"""Connect to a user-owned local Chromium without owning its lifetime."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import os
import re
import sys
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .browser_errors import AgentBrowserError
from .service import BrowserServiceError


def _local_endpoint(value: str) -> str:
    """Validate a loopback HTTP discovery URL or browser WebSocket URL."""
    if not isinstance(value, str):
        raise ValueError("地址必须是字符串")
    value = value.strip()
    if not value:
        return ""
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("地址不能包含空白或控制字符")
    implicit = "://" not in value
    if implicit:
        value = "http://" + value
    url = urlsplit(value)
    if url.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError("地址必须使用 http、https、ws 或 wss")
    if (
        url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError("地址不能包含凭据、查询参数或片段")
    host = url.hostname
    if host != "localhost" and (
        host is None or "%" in host or not ipaddress.ip_address(host).is_loopback
    ):
        raise ValueError("地址必须指向本机回环地址")
    if url.port == 0:
        raise ValueError("端口必须为 1～65535")
    if implicit:
        if url.path in {"", "/"}:
            # Internal marker: resolve a port from files, then HTTP if absent.
            return urlunsplit(("auto", url.netloc, "", "", ""))
        if url.path.startswith("/devtools/browser"):
            url = url._replace(scheme="ws")
            value = urlunsplit(url)
    if url.scheme in {"http", "https"}:
        if url.path not in {"", "/", "/json/version", "/json/version/"}:
            raise ValueError("HTTP 地址需填写浏览器调试根地址或 /json/version")
        return urlunsplit((url.scheme, url.netloc, "", "", ""))
    if url.path not in {
        "",
        "/",
        "/devtools/browser",
        "/devtools/browser/",
    } and not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", url.path):
        raise ValueError(
            "WebSocket 地址需填写主机和端口，或完整的 /devtools/browser/浏览器ID 路径"
        )
    return value


def _resolve_endpoint(endpoint: str, timeout: float) -> str:
    """Resolve local HTTP metadata without proxies or following redirects."""
    url = urlsplit(endpoint)
    if url.scheme in {"ws", "wss"}:
        return endpoint
    connection_type = (
        http.client.HTTPSConnection
        if url.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(url.hostname, url.port, timeout=timeout)
    try:
        connection.request(
            "GET", "/json/version", headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"Browser discovery returned HTTP {response.status}")
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("Browser discovery response is too large")
        metadata = json.loads(payload)
        websocket = _local_endpoint(metadata["webSocketDebuggerUrl"])
        target = urlsplit(websocket)
        if target.scheme not in {"ws", "wss"} or not re.fullmatch(
            r"/devtools/browser/[A-Za-z0-9_-]+", target.path
        ):
            raise ValueError(
                "Browser discovery did not return a full browser WebSocket URL"
            )
        return websocket
    finally:
        connection.close()


def browser_user_data_dirs() -> tuple[Path, ...]:
    """List standard profile roots for the OS account running AstrBot."""
    if sys.platform == "win32":
        root = Path(os.environ["LOCALAPPDATA"])
        return tuple(
            root / name
            for name in (
                "Google/Chrome/User Data",
                "Microsoft/Edge/User Data",
                "Chromium/User Data",
            )
        )
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
        return tuple(
            root / name for name in ("Google/Chrome", "Microsoft Edge", "Chromium")
        )
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    chrome_root = Path(os.environ.get("CHROME_CONFIG_HOME") or root)
    return (
        chrome_root / "google-chrome",
        root / "microsoft-edge",
        root / "chromium",
        Path.home() / "snap/chromium/common/chromium",
        Path.home() / ".var/app/org.chromium.Chromium/config/chromium",
        Path.home() / ".var/app/com.microsoft.Edge/config/microsoft-edge",
        Path.home() / ".var/app/com.google.Chrome/config/google-chrome",
    )


def _read_endpoint(profile: Path) -> str:
    """Read a browser's published endpoint without modifying its profile."""
    with (profile / "DevToolsActivePort").open("rb") as port_file:
        payload = port_file.read(4097)
    if len(payload) > 4096:
        raise ValueError("DevToolsActivePort is too large")
    lines = payload.decode("ascii").strip().splitlines()
    if (
        len(lines) != 2
        or not lines[0].isdecimal()
        or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", lines[1])
    ):
        raise ValueError("Invalid DevToolsActivePort")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid DevToolsActivePort port")
    return f"ws://127.0.0.1:{port}{lines[1]}"


def _discover_running_browser(user_data_dir: Path | None) -> str:
    """Discover published endpoints without port scans or connection attempts."""
    if user_data_dir is not None:
        return _read_endpoint(user_data_dir)
    found = {}
    for profile in browser_user_data_dirs():
        try:
            found[profile] = _read_endpoint(profile)
        except (OSError, ValueError):
            continue
    if not found:
        raise ValueError("No valid DevToolsActivePort in standard browser directories")
    if len(found) > 1:
        raise AgentBrowserError(
            "CDP_MULTIPLE_BROWSERS",
            "发现多个浏览器的调试信息，请填写 browser_use.cdp.user_data_dir 选择目标目录，或填写 browser_use.cdp.endpoint 指定调试地址后重载插件。候选目录："
            + "、".join(str(profile) for profile in found),
        )
    return next(iter(found.values()))


def _complete_websocket(endpoint: str, user_data_dir: Path | None) -> str:
    """Fill only the browser path from metadata matching the requested port."""
    url = urlsplit(endpoint)
    port = url.port or (443 if url.scheme == "wss" else 80)
    profiles = (
        (user_data_dir,) if user_data_dir is not None else browser_user_data_dirs()
    )
    paths = set()
    for profile in profiles:
        try:
            discovered = urlsplit(_read_endpoint(profile))
        except (OSError, ValueError):
            continue
        if discovered.port == port:
            paths.add(discovered.path)
    if not paths:
        raise AgentBrowserError(
            "CDP_DISCOVERY_FAILED",
            f"未找到端口 {port} 对应的浏览器调试路径。请确认远程调试已开启；若浏览器使用非标准目录，请填写 browser_use.cdp.user_data_dir，或直接填写完整 WebSocket 地址。",
        )
    if len(paths) > 1:
        raise AgentBrowserError(
            "CDP_MULTIPLE_BROWSERS",
            f"端口 {port} 对应多个不同的浏览器调试路径。请填写 browser_use.cdp.user_data_dir 选择目标目录，或填写完整 WebSocket 地址。",
        )
    return urlunsplit((url.scheme, url.netloc, paths.pop(), "", ""))


def _resolve_implicit_endpoint(
    endpoint: str, user_data_dir: Path | None, timeout: float
) -> str:
    """Resolve a bare local address without connecting to another port."""
    url = urlsplit(endpoint)
    websocket = urlunsplit(("ws", url.netloc, "", "", ""))
    try:
        return _complete_websocket(websocket, user_data_dir)
    except AgentBrowserError as exc:
        if exc.code != "CDP_DISCOVERY_FAILED":
            raise
    http = urlunsplit(("http", url.netloc, "", "", ""))
    discovered = urlsplit(_resolve_endpoint(http, timeout))
    discovered_port = discovered.port or (443 if discovered.scheme == "wss" else 80)
    if discovered_port != (url.port or 80):
        raise ValueError("HTTP discovery returned a different debugging port")
    return urlunsplit((discovered.scheme, url.netloc, discovered.path, "", ""))


class LocalCDP:
    """Own only a Playwright connection, never the external browser process."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        endpoint: str = "",
        user_data_dir: str = "",
        allow_existing_tabs: bool = False,
    ) -> None:
        for key, value in (
            ("enabled", enabled),
            ("allow_existing_tabs", allow_existing_tabs),
        ):
            if type(value) is not bool:
                raise BrowserServiceError(f"browser_use.cdp.{key} 必须是布尔值。")
        # A bad optional CDP address must not disable browsing or rendering.
        self.endpoint = endpoint
        if not isinstance(user_data_dir, str) or "\x00" in user_data_dir:
            raise BrowserServiceError(
                "browser_use.cdp.user_data_dir 必须是目录字符串。"
            )
        self.user_data_dir = Path(user_data_dir).expanduser() if user_data_dir else None
        if self.user_data_dir is not None and not self.user_data_dir.is_absolute():
            raise BrowserServiceError("browser_use.cdp.user_data_dir 必须是绝对路径。")
        self.enabled = enabled
        self.allow_existing_tabs = allow_existing_tabs
        self.browser: Browser | None = None
        self._runtime: Playwright | None = None

    async def connect(self, timeout: float) -> Browser:
        if not self.enabled:
            raise AgentBrowserError("CDP_DISABLED", "本地 CDP 控制已关闭。")
        if self.browser is not None and self.browser.is_connected():
            return self.browser
        await self.disconnect()
        runtime = None
        try:
            try:
                configured_endpoint = _local_endpoint(self.endpoint)
            except ValueError as exc:
                raise AgentBrowserError(
                    "CDP_INVALID_ENDPOINT",
                    f"本地浏览器调试地址无效：{exc}。请修改 browser_use.cdp.endpoint 后保存并重载插件，或清空地址恢复自动发现。",
                ) from exc
            if configured_endpoint:
                url = urlsplit(configured_endpoint)
                if url.scheme == "auto":
                    endpoint = await asyncio.to_thread(
                        _resolve_implicit_endpoint,
                        configured_endpoint,
                        self.user_data_dir,
                        timeout,
                    )
                elif url.scheme in {"ws", "wss"} and url.path in {
                    "",
                    "/",
                    "/devtools/browser",
                    "/devtools/browser/",
                }:
                    endpoint = await asyncio.to_thread(
                        _complete_websocket, configured_endpoint, self.user_data_dir
                    )
                else:
                    endpoint = await asyncio.to_thread(
                        _resolve_endpoint, configured_endpoint, timeout
                    )
            else:
                try:
                    endpoint = await asyncio.to_thread(
                        _discover_running_browser, self.user_data_dir
                    )
                except (OSError, ValueError, KeyError) as exc:
                    raise AgentBrowserError(
                        "CDP_DISCOVERY_FAILED",
                        "未找到 Chrome、Edge 或 Chromium 的有效调试地址。请在支持当前会话调试授权的浏览器中启用远程调试（Chrome/Chromium：chrome://inspect/#remote-debugging；Edge：edge://inspect/#remote-debugging）；确认 AstrBot 与浏览器使用同一系统账户，或配置正确的 browser_use.cdp.user_data_dir（浏览器版本页面中个人资料路径的上一级目录），也可填写 browser_use.cdp.endpoint 指定本地调试地址。",
                    ) from exc
            runtime = await async_playwright().start()
            browser = await runtime.chromium.connect_over_cdp(
                endpoint, timeout=timeout * 1000
            )
            if not browser.contexts:
                raise ValueError("No default browser context")
            self._runtime, self.browser = runtime, browser
            return browser
        except BaseException as exc:
            if runtime is not None:
                with suppress(PlaywrightError):
                    await runtime.stop()
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AgentBrowserError):
                raise
            raise AgentBrowserError(
                "CDP_CONNECTION_FAILED",
                "无法连接本地浏览器。请检查配置的调试地址（如已填写）、浏览器进程及远程调试开关，并在浏览器弹出连接授权窗口时点击允许；拒绝或超时后需显式重新请求连接。"
                + (
                    "省略协议时会先读取匹配端口的调试文件，没有匹配再尝试同地址的 HTTP /json/version；请确认用户数据目录可读取，或填写正确的用户数据根目录、完整 WebSocket 地址。"
                    if urlsplit(configured_endpoint).scheme == "auto"
                    else "HTTP 调试地址需提供 /json/version 接口；若当前会话调试不提供该接口，可改填 ws://主机:端口 自动读取浏览器路径，或填写完整 WebSocket 地址。"
                    if urlsplit(configured_endpoint).scheme in {"http", "https"}
                    else ""
                ),
            ) from exc

    async def disconnect(self) -> None:
        runtime, self._runtime = self._runtime, None
        self.browser = None
        if runtime is not None:
            # Stopping this separate client detaches CDP; never send Browser.close.
            with suppress(PlaywrightError):
                await runtime.stop()
