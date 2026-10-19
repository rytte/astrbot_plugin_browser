"""Optional session-scoped networking, emulation and JavaScript helpers."""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Response, Route

from .browser_errors import AgentBrowserError
from .service import DEFAULT_VIEWPORT

MAX_BODY = 64 * 1024
MAX_RECORDS = 200
RESOURCE_TYPES = {
    "document",
    "stylesheet",
    "image",
    "media",
    "font",
    "script",
    "texttrack",
    "xhr",
    "fetch",
    "eventsource",
    "websocket",
    "manifest",
    "other",
}
SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
}


def invalid(message: str) -> None:
    raise AgentBrowserError("INVALID_ARGUMENT", message)


def json_size(value: Any, limit: int) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise AgentBrowserError(
            "INVALID_ARGUMENT", "参数必须是有限数值组成的有效 JSON 数据。"
        ) from exc
    if len(encoded.encode("utf-8")) > limit:
        invalid(f"JSON 参数不能超过 {limit} 字节。")


def http_url(url: Any) -> bool:
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        return False
    try:
        parts = urlsplit(url)
        parts.port
        return bool(
            parts.scheme in {"http", "https"}
            and parts.hostname
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


def url_pattern(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2000
        or any(ord(c) < 32 for c in value)
    ):
        invalid("url_pattern 必须是 1～2000 字符的 URL 通配模式，* 匹配任意字符。")


def public_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key[:100]: "<redacted>" if key.lower() in SECRET_HEADERS else value[:1000]
        for key, value in list(headers.items())[:40]
    }


@dataclass
class NetworkEntry:
    id: str
    request: Request
    data: dict[str, Any]
    response: Response | None = None
    finished: bool = False


class NetworkMonitor:
    """Keep bounded metadata; obtain response bodies only on explicit detail calls."""

    def __init__(self) -> None:
        self.active = False
        self.pattern = "*"
        self.entries: OrderedDict[str, NetworkEntry] = OrderedDict()
        self.requests: dict[Request, str] = {}
        self.dropped = 0

    def request(self, request: Request) -> None:
        if not self.active or not fnmatchcase(request.url, self.pattern):
            return
        request_id = "req_" + uuid.uuid4().hex[:16]
        entry = NetworkEntry(
            request_id,
            request,
            {
                "request_id": request_id,
                "url": request.url[:8192],
                "method": request.method,
                "resource_type": request.resource_type,
                "started_at": time.time(),
                "status": None,
                "finished": False,
                "failure": "",
            },
        )
        self.entries[request_id] = entry
        self.requests[request] = request_id
        if len(self.entries) > MAX_RECORDS:
            _, removed = self.entries.popitem(last=False)
            self.requests.pop(removed.request, None)
            self.dropped += 1

    def response(self, response: Response) -> None:
        entry = self.entries.get(self.requests.get(response.request, ""))
        if entry is not None:
            entry.response = response
            entry.data["status"] = response.status
            entry.data["content_type"] = response.headers.get("content-type", "")[:200]

    def finished(self, request: Request, failed: bool = False) -> None:
        entry = self.entries.get(self.requests.get(request, ""))
        if entry is not None:
            entry.finished = True
            entry.data["finished"] = True
            entry.data["failure"] = (request.failure or "")[:500] if failed else ""

    async def operate(self, args: dict[str, Any]) -> dict[str, Any]:
        operation = args.get("operation", "list")
        request_id, pattern = args.get("request_id", ""), args.get("url_pattern", "")
        limit, include_body = args.get("limit", 50), args.get("include_body", False)
        if (
            operation not in {"start", "stop", "list", "detail", "clear"}
            or not 1 <= limit <= MAX_RECORDS
        ):
            invalid(
                "operation 为 start/stop/list/detail/clear，limit 为 1～200 的整数。"
            )
        if (
            (request_id and operation != "detail")
            or (include_body and operation != "detail")
            or (pattern and operation != "start")
        ):
            invalid("request_id/include_body 仅用于 detail；url_pattern 仅用于 start。")
        if operation == "start":
            url_pattern(pattern or "*")
            self.pattern, self.active = pattern or "*", True
        elif operation == "stop":
            self.active = False
        elif operation == "clear":
            self.entries.clear()
            self.requests.clear()
            self.dropped = 0
        elif operation == "detail":
            entry = self.entries.get(request_id)
            if entry is None:
                raise AgentBrowserError(
                    "REQUEST_NOT_FOUND", "当前会话的请求编号不存在，请先列出记录。"
                )
            detail = {
                **entry.data,
                "request_headers": public_headers(entry.request.headers),
            }
            response = entry.response
            if response is not None:
                detail["response_headers"] = public_headers(response.headers)
            if include_body:
                posted = entry.request.post_data
                if posted is not None:
                    encoded_post = posted.encode("utf-8")
                    detail["request_body"] = encoded_post[:MAX_BODY].decode(
                        "utf-8", errors="replace"
                    )
                    detail["request_body_truncated"] = len(encoded_post) > MAX_BODY
                mime = (
                    response.headers.get("content-type", "").lower() if response else ""
                )
                length = response.headers.get("content-length", "") if response else ""
                if not entry.finished:
                    detail["body_unavailable"] = "请求尚未完成，请稍后查询。"
                elif not response or entry.data["failure"]:
                    detail["body_unavailable"] = "请求失败或没有响应。"
                elif not (
                    mime.startswith("text/")
                    or any(
                        t in mime
                        for t in ("json", "xml", "javascript", "x-www-form-urlencoded")
                    )
                ):
                    detail["body_unavailable"] = "仅返回文本响应体，不读取二进制内容。"
                elif not length.isdigit() or int(length) > MAX_BODY:
                    detail["body_unavailable"] = (
                        "响应未声明 Content-Length，或声明大小超过 64 KiB；未读取响应体。"
                    )
                else:
                    body = await response.body()
                    detail["response_body"] = body[:MAX_BODY].decode(
                        "utf-8", errors="replace"
                    )
                    detail["response_body_truncated"] = len(body) > MAX_BODY
            return {"success": True, "request": detail}
        entries = list(self.entries.values())
        return {
            "success": True,
            "recording": self.active,
            "url_pattern": self.pattern,
            "requests": [dict(entry.data) for entry in entries[-limit:]],
            "truncated": len(entries) > limit,
            "dropped": self.dropped,
        }


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    json_size(rule, 96 * 1024)
    common = {"url_pattern", "action", "resource_types", "match_method"}
    action = rule.get("action")
    extra = {
        "abort": set(),
        "modify": {"headers", "url", "method", "post_data"},
        "fulfill": {"headers", "status", "content_type", "body"},
    }
    if (
        not isinstance(action, str)
        or action not in extra
        or set(rule) - common - extra[action]
    ):
        invalid("规则 action 为 abort/modify/fulfill，且不能包含未知或不适用的字段。")
    url_pattern(rule.get("url_pattern"))
    types = rule.get("resource_types", [])
    if not isinstance(types, list) or any(
        not isinstance(item, str) or item not in RESOURCE_TYPES for item in types
    ):
        invalid(
            "resource_types 必须是 Playwright 资源类型数组，例如 image、script、xhr、fetch。"
        )
    for key in ("match_method", "method"):
        if key in rule and (
            not isinstance(rule[key], str)
            or not re.fullmatch(r"[A-Z]{1,20}", rule[key])
        ):
            invalid(f"{key} 必须是大写 HTTP 方法名。")
    headers = rule.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 40:
        invalid("headers 必须是最多 40 项的对象。")
    normalized = {}
    for key, value in headers.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", key)
            or not isinstance(value, str)
            or len(value) > 4000
            or any(ord(c) < 32 and c != "\t" for c in value)
        ):
            invalid("请求头名称或值无效；不允许换行。")
        name = key.lower()
        if name in normalized:
            invalid("headers 不能重复指定同一个大小写无关的请求头。")
        if action == "modify" and name in {
            "cookie",
            "host",
            "content-length",
            "connection",
        }:
            invalid("不能通过 modify 覆盖 Cookie、Host、Content-Length 或 Connection。")
        normalized[name] = value
    if "url" in rule and not http_url(rule["url"]):
        invalid("修改后的 url 必须是完整 HTTP/HTTPS 网址，不能包含账号密码。")
    for key in ("body", "post_data"):
        if key in rule and (
            not isinstance(rule[key], str) or len(rule[key].encode("utf-8")) > MAX_BODY
        ):
            invalid(f"{key} 必须是最多 64 KiB 的文本。")
    if action == "modify" and not set(rule) & extra["modify"]:
        invalid("modify 至少需要 headers、url、method 或 post_data 之一。")
    if "status" in rule and (
        type(rule["status"]) is not int or not 200 <= rule["status"] <= 599
    ):
        invalid("模拟响应 status 必须是 200～599 的整数。")
    if "content_type" in rule and (
        not isinstance(rule["content_type"], str)
        or len(rule["content_type"]) > 200
        or any(ord(c) < 32 for c in rule["content_type"])
    ):
        invalid("content_type 必须是最多 200 字符的 MIME 类型，不能包含控制字符。")
    return {**rule, "headers": normalized}


class Interceptor:
    def __init__(self) -> None:
        self.rules: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []

    @staticmethod
    def describe(rule: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: public_headers(value) if key == "headers" else value
            for key, value in rule.items()
        }
        for key in ("body", "post_data"):
            if key in public and len(public[key]) > 1000:
                public[key] = public[key][:1000]
                public[key + "_truncated"] = True
        return public

    def operate(self, args: dict[str, Any]) -> dict[str, Any]:
        operation, rule_id, rule = (
            args.get("operation", "list"),
            args.get("rule_id", ""),
            args.get("rule", {}),
        )
        if operation not in {"list", "add", "remove", "clear"}:
            invalid("operation 为 list/add/remove/clear。")
        if (rule and operation != "add") or (rule_id and operation != "remove"):
            invalid("rule 仅用于 add，rule_id 仅用于 remove。")
        if operation == "add":
            validated = validate_rule(rule)
            if len(self.rules) >= 20:
                raise AgentBrowserError(
                    "RULE_LIMIT", "每个会话最多 20 条拦截规则，请先移除不用的规则。"
                )
            rule_id = "rule_" + uuid.uuid4().hex[:16]
            self.rules[rule_id] = validated
        elif operation == "remove":
            if rule_id not in self.rules:
                raise AgentBrowserError("RULE_NOT_FOUND", "当前会话的规则编号不存在。")
            del self.rules[rule_id]
        elif operation == "clear":
            self.rules.clear()
            self.errors.clear()
        return {
            "success": True,
            "rule_id": rule_id,
            "rules": [
                {
                    "rule_id": key,
                    **self.describe(value),
                }
                for key, value in self.rules.items()
            ],
            "errors": self.errors[-5:],
        }

    async def route(self, route: Route) -> None:
        try:
            await self._route(route)
        except PlaywrightError as exc:
            self.errors.append(
                "请求拦截执行失败，已尝试中止请求：" + str(exc).split("\n", 1)[0][:250]
            )
            del self.errors[:-5]
            with suppress(PlaywrightError):
                await route.abort()

    async def _route(self, route: Route) -> None:
        request = route.request
        if urlsplit(request.url).scheme not in {"http", "https"}:
            await route.abort()
            return
        for rule in tuple(self.rules.values()):
            if not fnmatchcase(request.url, rule["url_pattern"]):
                continue
            if (
                rule.get("resource_types")
                and request.resource_type not in rule["resource_types"]
            ):
                continue
            if rule.get("match_method") and request.method != rule["match_method"]:
                continue
            if rule["action"] == "abort":
                await route.abort("blockedbyclient")
            elif rule["action"] == "fulfill":
                await route.fulfill(
                    status=rule.get("status", 200),
                    headers=rule["headers"],
                    content_type=rule.get("content_type", "application/json"),
                    body=rule.get("body", ""),
                )
            else:
                changes = {
                    key: rule[key]
                    for key in ("url", "method", "post_data")
                    if key in rule
                }
                if rule["headers"]:
                    changes["headers"] = {**request.headers, **rule["headers"]}
                await route.continue_(**changes)
            return
        await route.continue_()


def environment(settings: dict[str, Any]) -> dict[str, Any]:
    json_size(settings, 16000)
    allowed = {
        "viewport",
        "device_scale_factor",
        "is_mobile",
        "has_touch",
        "user_agent",
        "locale",
        "timezone_id",
        "geolocation",
        "color_scheme",
        "reduced_motion",
        "offline",
    }
    if set(settings) - allowed:
        invalid("未知环境配置：" + ", ".join(sorted(set(settings) - allowed)))
    options = {"viewport": dict(DEFAULT_VIEWPORT), "device_scale_factor": 1, **settings}
    viewport = options["viewport"]
    if (
        not isinstance(viewport, dict)
        or set(viewport) != {"width", "height"}
        or any(type(v) is not int or not 1 <= v <= 8192 for v in viewport.values())
        or viewport["width"] * viewport["height"] > 16_777_216
    ):
        invalid(
            "viewport 必须包含 1～8192 的整数 width/height，面积最多 16777216 像素。"
        )
    for key in ("device_scale_factor",):
        value = options[key]
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not 0.5 <= value <= 3
        ):
            invalid(f"{key} 必须是 0.5～3 的有限数值。")
    for key in ("is_mobile", "has_touch", "offline"):
        if key in options and type(options[key]) is not bool:
            invalid(f"{key} 必须是布尔值。")
    for key, maximum in (("user_agent", 1000), ("locale", 50), ("timezone_id", 100)):
        if key in options and (
            not isinstance(options[key], str)
            or not 1 <= len(options[key]) <= maximum
            or any(ord(c) < 32 for c in options[key])
        ):
            invalid(f"{key} 必须是 1～{maximum} 字符的字符串，不能包含控制字符。")
    if "locale" in options and not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", options["locale"]
    ):
        invalid("locale 必须是语言标签，例如 zh-CN 或 en-US。")
    for key, values in (
        ("color_scheme", {"light", "dark", "no-preference"}),
        ("reduced_motion", {"reduce", "no-preference"}),
    ):
        if key in options and (
            not isinstance(options[key], str) or options[key] not in values
        ):
            invalid(f"{key} 无效，可选值：" + ", ".join(sorted(values)))
    if "geolocation" in options:
        geo = options["geolocation"]
        if (
            not isinstance(geo, dict)
            or not {"latitude", "longitude"} <= set(geo)
            or set(geo) - {"latitude", "longitude", "accuracy"}
        ):
            invalid("geolocation 需要 latitude/longitude，可选 accuracy。")
        for key, low, high in (
            ("latitude", -90, 90),
            ("longitude", -180, 180),
            ("accuracy", 0, 100000),
        ):
            value = geo.get(key, 0)
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                invalid(f"geolocation.{key} 必须是 {low}～{high} 的有限数值。")
    return options


SERIALIZE_SCRIPT = """value => {
  try {
    if (value === undefined) value = null;
    const serialized = JSON.stringify(value, (key, item) => {
      if (typeof item === 'bigint' || typeof item === 'function' || typeof item === 'symbol' ||
          item === undefined || (typeof item === 'number' && !Number.isFinite(item)) ||
          item instanceof Node || item === window)
        throw new Error('Return JSON values, not DOM nodes, functions or non-finite numbers');
      return item;
    });
    return {serialized: serialized.slice(0, 24000), truncated: serialized.length > 24000};
  } catch (_) { return {error: 'SCRIPT_RESULT_UNSUPPORTED'}; }
}"""
