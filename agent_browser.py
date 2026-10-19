"""Keep isolated online browser sessions for multi-turn agent tools."""

from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import time
import uuid
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    ElementHandle,
    JSHandle,
    Page,
)
from playwright.async_api import Error as PlaywrightError

from .browser_advanced import (
    SERIALIZE_SCRIPT,
    Interceptor,
    NetworkMonitor,
    environment,
    json_size,
)
from .browser_errors import AgentBrowserError
from .browser_storage import BrowserStorage
from .browser_tools import TOOL_LEVELS, actions_for_level
from .service import DEFAULT_VIEWPORT, BrowserService, BrowserServiceError

OBSERVE_SCRIPT = Path(__file__).with_name("observation.js").read_text(encoding="utf-8")
EXTRACT_SCRIPT = Path(__file__).with_name("extraction.js").read_text(encoding="utf-8")
MAX_TABS = 8
MAX_FRAMES = 16
STATE_SAVE_TIMEOUT = 5
ACTIVITY_SCRIPT = """(() => {
    let last = -Infinity;
    const activity = event => {
        const now = performance.now();
        if (!event.isTrusted || now - last < 1000) return;
        last = now;
        window.__astrbot_browser_activity().catch(() => {});
    };
    for (const name of ['pointerdown', 'keydown', 'input', 'wheel', 'touchstart', 'scroll'])
        addEventListener(name, activity, {capture: true, passive: true});
})();"""
Owner = tuple[str, str]
ACTION_PARAMETERS = {
    "mode": {"mode"},
    "open": {"url", "new_tab"},
    "observe": set(),
    "screenshot": set(),
    "click": {"observation_id", "ref", "x", "y", "button", "click_count"},
    "hover": {"observation_id", "ref", "x", "y"},
    "drag": {
        "observation_id",
        "source_ref",
        "target_ref",
        "start_x",
        "start_y",
        "end_x",
        "end_y",
    },
    "fill": {"observation_id", "ref", "text"},
    "press": {"observation_id", "key"},
    "select": {"observation_id", "ref", "value"},
    "scroll": {"observation_id", "direction", "pixels"},
    "wait": {"condition", "value", "state", "frame_index"},
    "upload": {"operation", "observation_id", "ref", "file_ids"},
    "download": {"operation", "file_id"},
    "extract": {"kind", "selector", "frame_index", "limit"},
    "dialog": {"operation", "dialog_type", "message", "prompt_text"},
    "state": {"operation"},
    "network": {"operation", "request_id", "url_pattern", "limit", "include_body"},
    "intercept": {"operation", "rule_id", "rule"},
    "emulate": {"operation", "settings"},
    "evaluate": {"observation_id", "expression", "argument", "frame_index"},
    "tabs": {"operation", "tab_id"},
    "close": set(),
}


@dataclass
class BrowserResult:
    """Return textual page state and, optionally, image bytes."""

    data: dict[str, Any]
    image: bytes | None = None
    file: Path | None = None


@dataclass
class AgentSession:
    """Own one user's live context, tabs and latest observation."""

    context: BrowserContext
    owner: Owner = ("", "")
    mode: str = "headless"
    closing: bool = False
    context_closed: bool = False
    disconnect_handler: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tabs: dict[str, Page] = field(default_factory=dict)
    active_tab: str = ""
    next_tab: int = 0
    last_used: float = field(default_factory=time.monotonic)
    auto_save: bool = True
    observation_id: str = ""
    observation_tab: str = ""
    refs: dict[str, tuple[JSHandle, int]] = field(default_factory=dict)
    snapshots: list[JSHandle] = field(default_factory=list)
    screenshot: bool = False
    coordinate_notice: str = ""
    notices: list[str] = field(default_factory=list)
    dialog_rule: dict[str, str] | None = None
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    network: NetworkMonitor = field(default_factory=NetworkMonitor)
    interceptor: Interceptor = field(default_factory=Interceptor)
    environment: dict[str, Any] = field(default_factory=lambda: environment({}))

    async def invalidate(self) -> None:
        """Disconnect observation guards and release remote DOM references."""
        self.observation_id = ""
        self.observation_tab = ""
        self.refs.clear()
        self.screenshot = False
        self.coordinate_notice = ""
        while self.snapshots:
            snapshot = self.snapshots[-1]
            with suppress(PlaywrightError):
                await snapshot.evaluate("state => state.observer.disconnect()")
            with suppress(PlaywrightError):
                await snapshot.dispose()
            self.snapshots.pop()


class AgentBrowser:
    """Manage bounded online contexts independently of execution capacity.

    Args:
        service: Shared browser runtime owned by the plugin.
        max_sessions: Maximum number of retained online contexts.
        idle_timeout: Seconds before an idle context is closed.
        action_timeout: Total seconds allowed for a tool call, including queueing.
        data_dir: Plugin-owned directory for login snapshots and temporary files.
        tool_level: Cumulative model tool level: basic, standard or full.
        headed_enabled: Allow explicit requests for the exclusive visible session.
    """

    def __init__(
        self,
        service: BrowserService,
        *,
        max_sessions: int = 4,
        idle_timeout: int = 600,
        action_timeout: int = 30,
        data_dir: Path | None = None,
        tool_level: str = "basic",
        headed_enabled: bool = True,
    ) -> None:
        for key, value, lower, upper in (
            ("browser_use.max_sessions", max_sessions, 1, 16),
            ("browser_use.idle_timeout", idle_timeout, 30, 3600),
            ("browser_use.action_timeout", action_timeout, 1, 120),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise BrowserServiceError(f"{key} 必须是 {lower}～{upper} 的整数。")
        if type(headed_enabled) is not bool:
            raise BrowserServiceError("browser_use.headed_enabled 必须是布尔值。")
        self.headed_enabled = headed_enabled
        self.service = service
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.action_timeout = action_timeout
        self.enabled_actions = actions_for_level(tool_level)
        self.tool_level = tool_level
        self.storage = BrowserStorage(data_dir)
        self._sessions: dict[Owner, AgentSession] = {}
        self._registry_lock = asyncio.Lock()
        self._owner_locks: dict[Owner, asyncio.Lock] = {}
        self._creating: set[Owner] = set()
        self._headed_owner: Owner | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._maintenance: set[asyncio.Task[Any]] = set()
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        """Start periodic idle-context collection on the current event loop."""
        if self._closed:
            raise BrowserServiceError("Agent 浏览器服务已停止。")
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(min(30, self.idle_timeout / 2))
            await self.reap_idle()

    async def reap_idle(self) -> None:
        """Close expired sessions without interrupting an operation in progress."""
        for owner, session in list(self._sessions.items()):
            lock = self._owner_lock(owner)
            if lock.locked() or session.lock.locked():
                continue
            async with lock:
                if (
                    self._sessions.get(owner) is session
                    and time.monotonic() - session.last_used >= self.idle_timeout
                ):
                    await self._drop(owner, session, save_state=True)

    def _owner_lock(self, owner: Owner) -> asyncio.Lock:
        return self._owner_locks.setdefault(owner, asyncio.Lock())

    def _schedule_external_close(self, session: AgentSession) -> None:
        if (
            self._closed
            or session.closing
            or self._sessions.get(session.owner) is not session
        ):
            return
        task = asyncio.create_task(self._external_close(session))
        self._maintenance.add(task)

        def finished(task: asyncio.Task[Any]) -> None:
            self._maintenance.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logger.warning(
                    "Failed to clean up a closed headed session",
                    exc_info=task.exception(),
                )

        task.add_done_callback(finished)

    async def _external_close(self, session: AgentSession) -> None:
        async with self._owner_lock(session.owner):
            if self._sessions.get(session.owner) is not session or session.closing:
                return
            if not session.context_closed and any(
                not p.is_closed() for p in session.tabs.values()
            ):
                return
            await self._drop(
                session.owner, session, save_state=not session.context_closed
            )

    async def _save_on_close(self, owner: Owner, session: AgentSession) -> bool | None:
        """Best-effort final snapshot; a cleared session must stay cleared."""
        try:
            async with self.storage.lock(owner):
                if not session.auto_save:
                    return None
                saved = await asyncio.wait_for(
                    session.context.storage_state(indexed_db=True),
                    timeout=STATE_SAVE_TIMEOUT,
                )
                self.storage.write_state(owner, saved)
            return True
        except Exception as exc:
            logger.warning(
                "Automatic browser login save failed (%s)", type(exc).__name__
            )
            return False

    async def _drop(
        self,
        owner: Owner,
        session: AgentSession,
        *,
        save_state: bool = False,
        release_headed: bool = True,
    ) -> bool | None:
        saved = None
        defer_close = False
        session.closing = True
        try:
            if save_state and self._sessions.get(owner) is session:
                saved = await self._save_on_close(owner, session)
        except asyncio.CancelledError:
            # Shutdown retries an interrupted final save after cancelling active calls.
            defer_close = self._closed
            raise
        finally:
            if not defer_close:
                if self._sessions.get(owner) is session:
                    del self._sessions[owner]
                try:
                    await asyncio.wait_for(session.context.close(), timeout=5)
                except (PlaywrightError, asyncio.TimeoutError):
                    logger.warning(
                        "Failed to close an agent browser context", exc_info=True
                    )
                finally:
                    session.refs.clear()
                    session.snapshots.clear()
                    if session.disconnect_handler is not None:
                        browser = session.context.browser
                        if browser is not None:
                            browser.remove_listener(
                                "disconnected", session.disconnect_handler
                            )
                    if session.mode == "headed" and release_headed:
                        try:
                            await self.service.close_headed(session.context.browser)
                        finally:
                            if self._headed_owner == owner:
                                self._headed_owner = None
        return saved

    async def _get_session(
        self, owner: Owner, browser: Browser, *, create: bool, mode: str = "headless"
    ) -> AgentSession:
        if self._closed:
            raise AgentBrowserError("SERVICE_CLOSED", "Agent 浏览器服务已停止。")
        session = self._sessions.get(owner)
        if session is not None:
            if session.context.browser is not browser:
                await self._drop(owner, session)
                raise AgentBrowserError(
                    "SESSION_LOST",
                    "浏览器已重启，原页面和登录态已失效，请重新打开网址。",
                )
            if time.monotonic() - session.last_used >= self.idle_timeout:
                await self._drop(owner, session, save_state=True)
                raise AgentBrowserError(
                    "SESSION_EXPIRED", "浏览器会话已空闲过期，请重新打开网址。"
                )
            return session
        if not create:
            raise AgentBrowserError(
                "NO_SESSION", "当前没有浏览器会话，请先调用 browser_open。"
            )
        async with self._registry_lock:
            if len(self._sessions) + len(self._creating) >= self.max_sessions:
                raise AgentBrowserError(
                    "SESSION_LIMIT",
                    "浏览器会话数量已达上限，请关闭不用的会话或稍后重试。",
                )
            self._creating.add(owner)
        try:
            session = await self._restore_session(owner, browser, mode=mode)
            self._sessions[owner] = session
            return session
        finally:
            self._creating.discard(owner)

    async def _restore_session(
        self,
        owner: Owner,
        browser: Browser,
        options: dict[str, Any] | None = None,
        *,
        mode: str = "headless",
    ) -> AgentSession:
        """Load a new session's own snapshot, with a clean fallback on invalid data."""
        saved = None
        notice = ""
        async with self.storage.lock(owner):
            try:
                saved = self.storage.read_state(owner)
            except (AgentBrowserError, OSError) as exc:
                if (
                    not isinstance(exc, AgentBrowserError)
                    or exc.code != "STATE_NOT_FOUND"
                ):
                    notice = "已保存的登录状态无法读取，本次会话从空白状态开始。"
                    logger.warning(
                        "Automatic browser login restore failed (%s)",
                        type(exc).__name__,
                    )
        try:
            session = await self._new_session(owner, browser, saved, options, mode=mode)
        except PlaywrightError:
            if saved is None:
                raise
            session = await self._new_session(
                owner, browser, options=options, mode=mode
            )
            notice = "已保存的登录状态无法载入，本次会话从空白状态开始。"
            logger.warning("Saved browser login state was rejected by Chromium")
        if notice:
            session.notices.append(notice)
        return session

    async def _new_session(
        self,
        owner: Owner,
        browser: Browser,
        storage_state: dict | None = None,
        options: dict[str, Any] | None = None,
        *,
        mode: str = "headless",
    ) -> AgentSession:
        options = environment({}) if options is None else options
        context = await browser.new_context(
            **options,
            java_script_enabled=True,
            service_workers="block",
            accept_downloads=True,
            storage_state=storage_state,
        )
        session = AgentSession(context, owner=owner, environment=options, mode=mode)
        try:
            context.set_default_timeout(self.action_timeout * 1000)
            context.set_default_navigation_timeout(self.action_timeout * 1000)
            await context.route("**/*", session.interceptor.route)
            if "network" in self.enabled_actions:
                context.on("request", session.network.request)
                context.on("response", session.network.response)
                context.on("requestfinished", session.network.finished)
                context.on(
                    "requestfailed",
                    lambda request: session.network.finished(request, failed=True),
                )
            context.on("page", lambda page: self._register_page(session, page))
            if mode == "headed":

                def context_closed() -> None:
                    session.context_closed = True
                    self._schedule_external_close(session)

                def disconnected() -> None:
                    session.context_closed = True
                    self._schedule_external_close(session)

                context.on("close", context_closed)
                session.disconnect_handler = disconnected
                browser.on("disconnected", disconnected)

                def activity(source: dict[str, Any]) -> None:
                    if self._sessions.get(owner) is session and not session.closing:
                        session.last_used = time.monotonic()

                await context.expose_binding("__astrbot_browser_activity", activity)
                await context.add_init_script(ACTIVITY_SCRIPT)
            if "geolocation" in options:
                await context.grant_permissions(["geolocation"])

            context.on("dialog", lambda dialog: self._handle_dialog(session, dialog))
            return session
        except BaseException:
            if session.disconnect_handler is not None:
                browser.remove_listener("disconnected", session.disconnect_handler)
            await context.close()
            raise

    async def _handle_dialog(self, session: AgentSession, dialog: Any) -> None:
        rule = session.dialog_rule
        matched = (
            bool(rule)
            and (not rule["dialog_type"] or rule["dialog_type"] == dialog.type)
            and (not rule["message"] or rule["message"] == dialog.message)
        )
        operation = rule["operation"] if matched else "dismiss"
        if matched:
            session.dialog_rule = None
        record = {
            "type": dialog.type,
            "message": dialog.message[:2000],
            "default_value": dialog.default_value[:1000],
            "operation": operation,
            "matched_rule": matched,
            "status": "handling",
        }
        session.dialogs.append(record)
        del session.dialogs[:-10]
        try:
            if operation == "accept":
                await dialog.accept(prompt_text=rule["prompt_text"])
            else:
                await dialog.dismiss()
            record["status"] = "handled"
        except PlaywrightError:
            record["status"] = "failed"
        session.notices.append(
            f"网页弹窗（{dialog.type}）已按 {operation} 处理：{dialog.message[:300]}"
        )
        del session.notices[:-5]

    def _start_download(self, session: AgentSession, download: Download) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._save_download(session, download))
        self._maintenance.add(task)
        task.add_done_callback(self._maintenance.discard)

    async def _save_download(self, session: AgentSession, download: Download) -> None:
        owner = session.owner
        item = None
        try:
            async with self.storage.lock(owner):
                pending = sum(
                    f.status == "downloading"
                    for f in self.storage.files.get(owner, {}).values()
                )
                if pending >= 3:
                    raise AgentBrowserError(
                        "FILE_LIMIT", "每个用户最多同时下载 3 个文件。"
                    )
                item = self.storage.reserve(
                    owner, download.suggested_filename, "download"
                )
            await asyncio.wait_for(download.save_as(item.path), timeout=120)
            async with self.storage.lock(owner):
                self.storage.finish(owner, item)
        except (
            PlaywrightError,
            OSError,
            AgentBrowserError,
            asyncio.TimeoutError,
            asyncio.CancelledError,
        ) as exc:
            with suppress(PlaywrightError, asyncio.TimeoutError):
                await asyncio.wait_for(download.cancel(), timeout=5)
            if item is not None:
                self.storage.fail(
                    item,
                    str(exc)
                    if isinstance(exc, AgentBrowserError)
                    else "下载失败、超时或会话已关闭。",
                )
            else:
                session.notices.append(
                    str(exc)
                    if isinstance(exc, AgentBrowserError)
                    else "无法接收下载文件，请检查文件区容量和磁盘权限。"
                )
                del session.notices[:-5]
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            # save_as copies the file; release Playwright's original temporary copy too.
            with suppress(PlaywrightError, asyncio.TimeoutError):
                await asyncio.wait_for(download.delete(), timeout=5)

    def _register_page(self, session: AgentSession, page: Page) -> None:
        if len([p for p in session.tabs.values() if not p.is_closed()]) >= MAX_TABS:
            session.notices.append(f"标签页超过 {MAX_TABS} 个，已关闭新增标签页。")
            del session.notices[:-5]
            task = asyncio.create_task(page.close())
            self._maintenance.add(task)

            def finished(task: asyncio.Task[Any]) -> None:
                self._maintenance.discard(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.warning("Failed to close an excess agent tab")

            task.add_done_callback(finished)
            return
        session.next_tab += 1
        tab_id = f"tab_{session.next_tab}"
        session.tabs[tab_id] = page
        page.on("download", lambda download: self._start_download(session, download))

        def page_closed() -> None:
            session.tabs.pop(tab_id, None)
            if session.mode == "headed":
                self._schedule_external_close(session)

        page.on("close", page_closed)
        if not session.active_tab:
            session.active_tab = tab_id

    @staticmethod
    def _page(session: AgentSession) -> Page:
        page = session.tabs.get(session.active_tab)
        if page is None or page.is_closed():
            raise AgentBrowserError(
                "PAGE_CLOSED",
                "当前标签页已关闭，请使用 browser_tabs 切换或重新打开网址。",
            )
        return page

    @staticmethod
    def _check_observation(session: AgentSession, observation_id: str) -> None:
        if (
            not observation_id
            or observation_id != session.observation_id
            or session.observation_tab != session.active_tab
        ):
            raise AgentBrowserError(
                "STALE_OBSERVATION", "观察编号已失效，请重新观察页面后再操作。"
            )

    async def _observe(self, session: AgentSession) -> BrowserResult:
        await session.invalidate()
        page = self._page(session)
        frames: list[dict[str, Any]] = []
        total_text = 0
        total_elements = 0
        element_budget = 48000
        for frame_index, frame in enumerate(page.frames[:MAX_FRAMES]):
            if frame.is_detached():
                continue
            snapshot = await frame.evaluate_handle(OBSERVE_SCRIPT)
            session.snapshots.append(snapshot)
            data_handle = await snapshot.get_property("data")
            try:
                data = await data_handle.json_value()
            finally:
                await data_handle.dispose()
            for key in ("viewport_text", "text"):
                remaining = max(0, 16000 - total_text)
                if len(data[key]) > remaining:
                    data[key] = data[key][:remaining]
                    data[key + "_truncated"] = True
                total_text += len(data[key])
            descriptions = data["elements"]
            remaining = max(0, 150 - total_elements)
            if len(descriptions) > remaining:
                data["elements_truncated"] = True
            data["elements"] = []
            for index, item in enumerate(descriptions[:remaining]):
                size = len(json.dumps(item, ensure_ascii=False))
                if size > element_budget:
                    data["elements_truncated"] = True
                    continue
                element_budget -= size
                total_elements += 1
                ref = f"e{total_elements}"
                item["ref"] = ref
                session.refs[ref] = (snapshot, index)
                data["elements"].append(item)
            frames.append(
                {
                    "frame_index": frame_index,
                    "url": frame.url[:2048],
                    "name": frame.name[:200],
                    **data,
                }
            )
        session.observation_id = "obs_" + uuid.uuid4().hex[:16]
        session.observation_tab = session.active_tab
        return BrowserResult(
            {
                "success": True,
                "observation_id": session.observation_id,
                "active_tab": session.active_tab,
                "url": page.url[:2048],
                "title": (await page.title())[:500],
                "viewport": page.viewport_size,
                "frames": frames,
                "frames_truncated": len(page.frames) > MAX_FRAMES,
                "tabs": [
                    {"id": key, "url": tab.url[:2048]}
                    for key, tab in session.tabs.items()
                    if not tab.is_closed()
                ],
                "notices": session.notices[-5:],
                "dialogs": session.dialogs[-10:],
                "interception_errors": session.interceptor.errors[-5:],
                "downloads": [
                    f.describe()
                    for f in self.storage.files.get(session.owner, {}).values()
                    if f.source == "download"
                ],
            }
        )

    def require_action(self, action: str) -> None:
        """Reject unavailable tools before accessing browser or attachment state."""
        if not isinstance(action, str) or action not in ACTION_PARAMETERS:
            raise AgentBrowserError("UNKNOWN_ACTION", "未知浏览器操作。")
        if action not in self.enabled_actions:
            required = next(
                level for level, actions in TOOL_LEVELS.items() if action in actions
            )
            raise AgentBrowserError(
                "TOOL_LEVEL_REQUIRED",
                f"browser_{action} 不在当前 {self.tool_level} 工具层级中，"
                f"请在 BrowserUse 配置组中将 tool_level 调整为 {required} 或更高层级，并重载插件。",
            )

    async def execute(
        self, owner: Owner, action: str, **arguments: Any
    ) -> BrowserResult:
        """Execute one bounded tool operation for a server-derived owner.

        Args:
            owner: Message source and sender identity, never model supplied.
            action: Operation implemented by the registered model tools.
            **arguments: Validated operation-specific parameters.

        Returns:
            Current page information and optional screenshot bytes.

        Raises:
            AgentBrowserError: The operation cannot be completed safely.
        """
        if self._closed:
            raise AgentBrowserError("SERVICE_CLOSED", "Agent 浏览器服务已停止。")
        self.validate_owner(owner)
        self.require_action(action)
        unknown = set(arguments) - ACTION_PARAMETERS[action]
        if unknown:
            raise AgentBrowserError(
                "INVALID_ARGUMENT", "未知工具参数：" + ", ".join(sorted(unknown))
            )
        for key, value in arguments.items():
            if key in {
                "new_tab",
                "include_body",
                "pixels",
                "click_count",
                "frame_index",
                "limit",
            }:
                valid = type(value) is (
                    bool if key in {"new_tab", "include_body"} else int
                )
            elif key in {"rule", "settings", "argument"}:
                valid = isinstance(value, dict)
            elif key == "file_ids":
                valid = (
                    isinstance(value, list)
                    and len(value) <= 10
                    and all(isinstance(item, str) for item in value)
                )
            elif key in {"x", "y", "start_x", "start_y", "end_x", "end_y"}:
                valid = type(value) in {int, float} and math.isfinite(value)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise AgentBrowserError("INVALID_ARGUMENT", f"{key} 的类型或数值无效。")
        if action == "evaluate":
            expression = arguments.get("expression", "")
            if not 1 <= len(expression) <= 20000:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT",
                    "expression 必须是 1～20000 字符的 JavaScript 表达式或函数。",
                )
            json_size(arguments.get("argument", {}), 32000)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            return await asyncio.wait_for(
                self._serialized_execute(owner, action, arguments),
                timeout=self.action_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise AgentBrowserError(
                "TIMEOUT",
                "JavaScript 执行超时，当前会话已关闭，请重新打开网站。操作可能已执行。"
                if action == "evaluate"
                else "浏览器工具超时；操作可能已执行，请重新观察确认结果，不要直接重复操作。",
            ) from exc
        except PlaywrightError as exc:
            raise AgentBrowserError(
                "BROWSER_ERROR",
                "浏览器操作失败，请重新观察页面后决定下一步。"
                + str(exc).split("\n", 1)[0][:250],
            ) from exc
        except OSError as exc:
            raise AgentBrowserError(
                "STORAGE_ERROR", "浏览器文件存储读写失败，请检查磁盘空间和目录权限。"
            ) from exc
        except BrowserServiceError as exc:
            if isinstance(exc, AgentBrowserError):
                raise
            raise AgentBrowserError("SERVICE_UNAVAILABLE", str(exc)) from exc
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def _serialized_execute(
        self, owner: Owner, action: str, args: dict[str, Any]
    ) -> BrowserResult:
        async with self._owner_lock(owner):
            if self._closed:
                raise AgentBrowserError("SERVICE_CLOSED", "Agent 浏览器服务已停止。")
            result = await self._execute(owner, action, args)
            session = self._sessions.get(owner)
            result.data.setdefault(
                "mode", session.mode if session is not None else "headless"
            )
            return result

    async def _mode(self, owner: Owner, target: str) -> BrowserResult:
        if target not in {"status", "headless", "headed"}:
            raise AgentBrowserError(
                "INVALID_ARGUMENT", "mode 为 status/headless/headed。"
            )
        previous = self._sessions.get(owner)
        if previous is not None and (
            previous.context_closed or not previous.context.browser.is_connected()
        ):
            await self._drop(owner, previous)
            previous = None
        if target == "status":
            return BrowserResult(
                {
                    "success": True,
                    "mode": previous.mode if previous else "headless",
                    "session_active": previous is not None,
                    "headed_enabled": self.headed_enabled,
                    "headed_in_use": self._headed_owner is not None,
                }
            )
        if target == "headed" and not self.headed_enabled:
            raise AgentBrowserError(
                "HEADED_DISABLED",
                "有头模式已关闭，请在 browser_use.headed_enabled 中开启并重载插件。",
            )
        if previous is not None and previous.mode == target:
            return BrowserResult(
                {
                    "success": True,
                    "mode": target,
                    "session_active": True,
                    "session_rebuilt": False,
                }
            )
        if previous is None and target == "headless":
            return BrowserResult(
                {
                    "success": True,
                    "mode": "headless",
                    "session_active": False,
                    "session_rebuilt": False,
                }
            )
        if target == "headed":
            async with self._registry_lock:
                if self._headed_owner is not None and self._headed_owner != owner:
                    raise AgentBrowserError(
                        "HEADED_BUSY",
                        "有头会话正在使用中，请稍后重试或继续使用无头模式。",
                    )
                if (
                    previous is None
                    and len(self._sessions) + len(self._creating) >= self.max_sessions
                ):
                    raise AgentBrowserError("SESSION_LIMIT", "浏览器会话数量已达上限。")
                self._headed_owner = owner
        try:
            async with self.service.operation(target) as browser:
                return await self._replace_session(owner, browser, mode=target)
        except BaseException:
            if target == "headed" and self._sessions.get(owner) is previous:
                try:
                    await self.service.close_headed()
                finally:
                    if self._headed_owner == owner:
                        self._headed_owner = None
            raise

    @staticmethod
    def validate_owner(owner: Owner) -> None:
        if (
            not isinstance(owner, tuple)
            or len(owner) != 2
            or any(not isinstance(item, str) or not item for item in owner)
        ):
            raise AgentBrowserError("INVALID_OWNER", "无法确定消息来源和发送者。")

    async def import_attachment(
        self, owner: Owner, path: Path, name: str, key: str
    ) -> None:
        self.validate_owner(owner)
        if self._closed:
            raise AgentBrowserError("SERVICE_CLOSED", "Agent 浏览器服务已停止。")
        await self.storage.import_file(owner, path, name, key)

    async def _execute(
        self, owner: Owner, action: str, args: dict[str, Any]
    ) -> BrowserResult:
        if action == "mode":
            return await self._mode(owner, args.get("mode", "status"))
        current = self._sessions.get(owner)
        mode = current.mode if current is not None else "headless"
        if (
            current is not None
            and mode == "headed"
            and (current.context_closed or not current.context.browser.is_connected())
        ):
            await self._drop(owner, current)
            raise AgentBrowserError(
                "SESSION_LOST",
                "有头窗口已关闭，原会话已失效；重新打开网址默认使用无头模式。",
            )
        if action == "close" and current is None:
            return BrowserResult(
                {"success": True, "session_closed": True, "state_saved": None}
            )
        if action == "emulate":
            operation, settings = (
                args.get("operation", "status"),
                args.get("settings", {}),
            )
            if operation not in {"status", "set", "reset"} or (
                settings and operation != "set"
            ):
                raise AgentBrowserError(
                    "INVALID_ARGUMENT",
                    "operation 为 status/set/reset，settings 仅用于 set。",
                )
            if operation in {"set", "reset"}:
                options = environment(settings if operation == "set" else {})
                async with self.service.operation(mode) as browser:
                    return await self._replace_session(owner, browser, options=options)
        if action == "download":
            return await self._files(owner, args)
        if action == "upload" and args.get("operation", "list") == "list":
            if args.get("observation_id") or args.get("ref") or args.get("file_ids"):
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "列出上传文件时不要传入页面目标或文件编号。"
                )
            return await self._files(owner, {"operation": "list"})
        if action == "state":
            operation = args.get("operation", "status")
            if operation not in {"status", "save", "restore", "clear"}:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "operation 为 status/save/restore/clear。"
                )
            if operation in {"status", "clear"}:
                async with self._registry_lock:
                    async with self.storage.lock(owner):
                        path = self.storage.state_path(owner)
                        if operation == "clear":
                            path.unlink(missing_ok=True)
                            session = self._sessions.get(owner)
                            if session is not None:
                                session.auto_save = False
                        return BrowserResult(
                            {
                                "success": True,
                                "saved": path.is_file(),
                                "live_session_unchanged": True,
                            }
                        )
            if operation == "restore":
                async with self.storage.lock(owner):
                    saved = self.storage.read_state(owner)
                async with self.service.operation(mode) as browser:
                    result = await self._replace_session(owner, browser, saved=saved)
                    result.data["restored"] = True
                    return result
        if action == "open":
            url = args.get("url")
            if not isinstance(url, str) or len(url) > 8192:
                raise AgentBrowserError("INVALID_URL", "网址必须是 HTTP/HTTPS 字符串。")
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not any(char.isspace() or ord(char) < 32 for char in url)
                )
                parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise AgentBrowserError(
                    "INVALID_URL", "只允许完整的 HTTP/HTTPS 网址，且不能包含账号密码。"
                )
            if type(args.get("new_tab", False)) is not bool:
                raise AgentBrowserError("INVALID_ARGUMENT", "new_tab 必须是布尔值。")
        if current is None and action not in {
            "open",
            "network",
            "intercept",
            "emulate",
        }:
            raise AgentBrowserError(
                "NO_SESSION", "当前没有浏览器会话，请先调用 browser_open。"
            )
        async with self.service.operation(mode) as browser:
            session = await self._get_session(
                owner,
                browser,
                create=action in {"open", "network", "intercept", "emulate"},
                mode=mode,
            )
            async with session.lock:
                if self._sessions.get(owner) is not session:
                    raise AgentBrowserError(
                        "SESSION_EXPIRED", "会话已关闭，请重新打开网址。"
                    )
                session.last_used = time.monotonic()
                try:
                    return await self._act(owner, session, action, args)
                except BaseException as exc:
                    if self._closed:
                        # Shutdown saves retained contexts after all calls have stopped.
                        pass
                    elif action == "evaluate" and isinstance(
                        exc, asyncio.CancelledError
                    ):
                        await self._drop(owner, session)
                    else:
                        try:
                            await asyncio.wait_for(session.invalidate(), timeout=1)
                        except asyncio.TimeoutError:
                            await self._drop(owner, session)
                    raise
                finally:
                    session.last_used = time.monotonic()

    async def _replace_session(
        self,
        owner: Owner,
        browser: Browser,
        *,
        saved: dict | None = None,
        options: dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> BrowserResult:
        """Commit a rebuilt context only after it is usable; retain the old one on failure."""
        previous = self._sessions.get(owner)
        mode = mode or (previous.mode if previous else "headless")
        async with self._registry_lock:
            if (
                previous is None
                and len(self._sessions) + len(self._creating) >= self.max_sessions
            ):
                raise AgentBrowserError("SESSION_LIMIT", "浏览器会话数量已达上限。")
            if previous is None:
                self._creating.add(owner)
        try:
            async with previous.lock if previous else asyncio.Lock():
                preserve_auto_save = saved is None
                if previous is not None:
                    if saved is None:
                        if (
                            previous.mode == mode
                            and previous.context.browser is not browser
                        ):
                            raise AgentBrowserError(
                                "SESSION_LOST",
                                "浏览器已重启，不能保留原会话；请重新打开网站。",
                            )
                        saved = await previous.context.storage_state(indexed_db=True)
                    if options is None:
                        options = previous.environment
                if previous is None and saved is None:
                    session = await self._restore_session(
                        owner, browser, options, mode=mode
                    )
                else:
                    session = await self._new_session(
                        owner, browser, saved, options, mode=mode
                    )
                try:
                    await session.context.new_page()
                    if previous is not None:
                        if preserve_auto_save:
                            session.auto_save = previous.auto_save
                        session.interceptor.rules = dict(previous.interceptor.rules)
                        session.network.active = previous.network.active
                        session.network.pattern = previous.network.pattern
                except BaseException:
                    await self._drop(owner, session, release_headed=False)
                    raise
                self._sessions[owner] = session
                if previous is not None:
                    await self._drop(
                        owner, previous, release_headed=previous.mode != mode
                    )
                return BrowserResult(
                    {
                        "success": True,
                        "mode": mode,
                        "environment": session.environment,
                        "session_rebuilt": True,
                        "message": "会话已重建，登录存储已载入；旧标签页和元素编号已失效，请调用 browser_open 打开网站。网络记录与弹窗规则已清空。",
                    }
                )
        finally:
            self._creating.discard(owner)

    async def _files(self, owner: Owner, args: dict[str, Any]) -> BrowserResult:
        operation, file_id = args.get("operation", "list"), args.get("file_id", "")
        if operation not in {"list", "wait", "send", "delete"}:
            raise AgentBrowserError(
                "INVALID_ARGUMENT", "operation 为 list/wait/send/delete。"
            )
        if operation == "list":
            if file_id:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "列出文件时不要传 file_id。"
                )
            async with self.storage.lock(owner):
                return BrowserResult(
                    {
                        "success": True,
                        "files": [
                            f.describe()
                            for f in self.storage.files.get(owner, {}).values()
                        ],
                    }
                )
        async with self.storage.lock(owner):
            item = self.storage.get(owner, file_id)
            if operation == "delete":
                self.storage.delete(owner, file_id)
                return BrowserResult({"success": True, "deleted": file_id})
        if operation == "wait":
            await item.done.wait()
        async with self.storage.lock(owner):
            item = self.storage.get(owner, file_id, ready=operation == "send")
            data = {"success": item.status != "failed", "file": item.describe()}
            if item.status == "failed":
                data["error"] = {"code": "DOWNLOAD_FAILED", "message": item.error}
            return BrowserResult(
                data,
                file=item.path if operation == "send" else None,
            )

    async def _evaluate(
        self, session: AgentSession, page: Page, args: dict[str, Any]
    ) -> BrowserResult:
        self._check_observation(session, args.get("observation_id", ""))
        for snapshot in session.snapshots:
            if not await snapshot.evaluate(
                "state => state.document === document.documentElement && state.url === location.href"
            ):
                raise AgentBrowserError(
                    "STALE_OBSERVATION", "网页已跳转，请重新观察后再运行脚本。"
                )
        frame = self._frame(page, args.get("frame_index", 0))
        await session.invalidate()
        handle = None
        try:
            handle = await frame.evaluate_handle(
                args["expression"], args.get("argument", {})
            )
            serialized = await handle.evaluate(SERIALIZE_SCRIPT)
        except PlaywrightError as exc:
            raise AgentBrowserError(
                "SCRIPT_ERROR", "页面脚本执行失败：" + str(exc).split("\n", 1)[0][:250]
            ) from exc
        finally:
            if handle is not None:
                with suppress(PlaywrightError, asyncio.TimeoutError):
                    await asyncio.wait_for(handle.dispose(), timeout=1)
        if serialized.get("error"):
            raise AgentBrowserError(
                "SCRIPT_RESULT_UNSUPPORTED",
                "脚本返回值不能转为 JSON，请返回普通数据，避免 DOM 节点、循环引用、BigInt 和非有限数值。",
            )
        result = await self._observe(session)
        result.data["evaluation"] = (
            {"result_preview": serialized["serialized"], "truncated": True}
            if serialized["truncated"]
            else {"result": json.loads(serialized["serialized"]), "truncated": False}
        )
        return result

    @staticmethod
    def _frame(page: Page, index: int):
        if not 0 <= index < min(len(page.frames), MAX_FRAMES):
            raise AgentBrowserError(
                "INVALID_FRAME", "frame_index 不存在，请使用最新观察中的 frame_index。"
            )
        return page.frames[index]

    async def _wait(
        self, session: AgentSession, page: Page, args: dict[str, Any]
    ) -> BrowserResult:
        condition, value = args.get("condition", "text"), args.get("value", "")
        state, index = args.get("state", ""), args.get("frame_index", 0)
        if (
            condition not in {"text", "selector", "url", "load"}
            or not 1 <= len(value) <= 2000
        ):
            raise AgentBrowserError(
                "INVALID_ARGUMENT",
                "condition 为 text/selector/url/load，value 为 1～2000 字符。",
            )
        frame = self._frame(page, index)
        if condition in {"text", "selector"}:
            state = state or "visible"
            allowed = (
                {"visible", "hidden"}
                if condition == "text"
                else {"visible", "hidden", "attached", "detached"}
            )
            if state not in allowed:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT",
                    "text 状态为 visible/hidden；selector 另支持 attached/detached。",
                )
            locator = (
                frame.get_by_text(value, exact=False)
                if condition == "text"
                else frame.locator("css=" + value)
            )
            if state in {"visible", "hidden"}:
                locator = locator.filter(visible=True)
            await session.invalidate()
            await locator.first.wait_for(state=state)
        else:
            if index != 0 or state:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "url/load 等待仅支持主页面，state 请留空。"
                )
            if condition == "load" and value not in {"domcontentloaded", "load"}:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "load 的 value 为 domcontentloaded 或 load。"
                )
            if condition == "url" and not value.startswith(("http://", "https://")):
                raise AgentBrowserError(
                    "INVALID_ARGUMENT",
                    "url 的 value 必须为 HTTP/HTTPS 网址，可使用 * 通配符。",
                )
            await session.invalidate()
            if condition == "load":
                await page.wait_for_load_state(value)
            else:
                await page.wait_for_url(value, wait_until="domcontentloaded")
        return await self._observe(session)

    async def _extract(self, page: Page, args: dict[str, Any]) -> BrowserResult:
        kind, selector = args.get("kind", "tables"), args.get("selector", "")
        limit, index = args.get("limit", 100), args.get("frame_index", 0)
        if (
            kind not in {"tables", "links", "lists", "text"}
            or len(selector) > 2000
            or not 1 <= limit <= 200
        ):
            raise AgentBrowserError(
                "INVALID_ARGUMENT",
                "kind 为 tables/links/lists/text，limit 为 1～200 的整数，selector 最多 2000 字符。",
            )
        frame = self._frame(page, index)
        extracted = await frame.evaluate(
            EXTRACT_SCRIPT, {"kind": kind, "selector": selector, "limit": limit}
        )
        if extracted.get("error"):
            raise AgentBrowserError(
                "INVALID_SELECTOR", "selector 不是有效的 CSS 选择器。"
            )
        return BrowserResult(
            {
                "success": True,
                "frame_index": index,
                "url": frame.url[:2048],
                **extracted,
            }
        )

    async def _upload(
        self, owner: Owner, session: AgentSession, args: dict[str, Any]
    ) -> BrowserResult:
        operation, file_ids = args.get("operation", "list"), args.get("file_ids", [])
        if (
            operation not in {"set", "clear"}
            or (operation == "set" and not file_ids)
            or (operation == "clear" and file_ids)
        ):
            raise AgentBrowserError(
                "INVALID_ARGUMENT",
                "operation 为 list/set/clear；set 需传 file_ids，clear 不传文件编号。",
            )
        self._check_observation(session, args.get("observation_id", ""))
        target = await self._resolve_element(session, args.get("ref", ""))
        try:
            info = await target.evaluate(
                "el => ({file: el.tagName === 'INPUT' && el.type === 'file', multiple: el.multiple})"
            )
            if not info["file"]:
                raise AgentBrowserError(
                    "INVALID_TARGET", "目标必须是观察中的文件输入框（type=file）。"
                )
            if len(file_ids) > 1 and not info["multiple"]:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "该文件输入框只接受一个文件。"
                )
            async with self.storage.lock(owner):
                files = [
                    self.storage.get(owner, file_id, ready=True) for file_id in file_ids
                ]
                await session.invalidate()
                # Buffers hide opaque server filenames from the website and preserve the supplied name.
                await target.set_input_files(
                    [
                        {
                            "name": f.name,
                            "mimeType": mimetypes.guess_type(f.name)[0]
                            or "application/octet-stream",
                            "buffer": f.path.read_bytes(),
                        }
                        for f in files
                    ]
                )
        finally:
            await target.dispose()
        return await self._observe(session)

    def _dialog(self, session: AgentSession, args: dict[str, Any]) -> BrowserResult:
        operation = args.get("operation", "list")
        dialog_type, message, prompt = (
            args.get("dialog_type", ""),
            args.get("message", ""),
            args.get("prompt_text", ""),
        )
        if (
            operation not in {"list", "accept", "dismiss", "clear"}
            or dialog_type not in {"", "alert", "confirm", "prompt", "beforeunload"}
            or len(message) > 2000
            or len(prompt) > 2000
        ):
            raise AgentBrowserError(
                "INVALID_ARGUMENT",
                "operation 为 list/accept/dismiss/clear；dialog_type 为 alert/confirm/prompt/beforeunload 或空，文字最多 2000 字符。",
            )
        if (operation in {"list", "clear"} and (dialog_type or message or prompt)) or (
            prompt and (operation != "accept" or dialog_type not in {"", "prompt"})
        ):
            raise AgentBrowserError(
                "INVALID_ARGUMENT",
                "查询/清除规则时不传匹配条件；prompt_text 仅用于接受 prompt。",
            )
        if operation == "clear":
            session.dialog_rule = None
        elif operation != "list":
            session.dialog_rule = {
                "operation": operation,
                "dialog_type": dialog_type,
                "message": message,
                "prompt_text": prompt,
            }
        return BrowserResult(
            {
                "success": True,
                "next_dialog": session.dialog_rule,
                "history": session.dialogs[-10:],
            }
        )

    async def _resolve_element(self, session: AgentSession, ref: str) -> ElementHandle:
        """Resolve an observed DOM node without switching to a replacement node."""
        reference = session.refs.get(ref)
        if reference is None:
            raise AgentBrowserError("INVALID_REF", "元素编号不存在，请重新观察页面。")
        snapshot, index = reference
        valid = await snapshot.evaluate(
            """(state, index) => state.elements[index]?.isConnected &&
            state.signature(state.elements[index]) === state.signatures[index]""",
            index,
        )
        if not valid:
            raise AgentBrowserError("STALE_ELEMENT", "目标元素已变化，请重新观察页面。")
        elements = await snapshot.get_property("elements")
        try:
            handle = await elements.get_property(str(index))
        finally:
            await elements.dispose()
        target = handle.as_element()
        if target is None:
            await handle.dispose()
            raise AgentBrowserError("STALE_ELEMENT", "目标元素已失效，请重新观察页面。")
        return target

    async def _check_coordinates(
        self, session: AgentSession, page: Page, points: tuple[tuple[float, float], ...]
    ) -> None:
        """Validate every coordinate against the same unchanged screenshot."""
        if not session.screenshot:
            raise AgentBrowserError(
                "COORDINATES_UNAVAILABLE"
                if session.coordinate_notice
                else "SCREENSHOT_REQUIRED",
                session.coordinate_notice or "坐标操作前请先获取当前视口截图。",
            )
        viewport = page.viewport_size or DEFAULT_VIEWPORT
        for x, y in points:
            if any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 <= value < viewport[axis]
                for value, axis in ((x, "width"), (y, "height"))
            ):
                raise AgentBrowserError(
                    "INVALID_COORDINATES", "坐标必须位于截图视口范围内。"
                )
        for snapshot in session.snapshots:
            valid = await snapshot.evaluate(
                """state => state.document === document.documentElement &&
                state.revision === 0 && state.x === scrollX && state.y === scrollY &&
                state.width === innerWidth && state.height === innerHeight &&
                state.visual_scale === (visualViewport?.scale || 1) &&
                state.visual_x === (visualViewport?.offsetLeft || 0) &&
                state.visual_y === (visualViewport?.offsetTop || 0) &&
                state.url === location.href"""
            )
            if not valid:
                raise AgentBrowserError(
                    "STALE_SCREENSHOT", "页面已变化，请重新截图后再操作坐标。"
                )

    async def _mouse_action(
        self, session: AgentSession, page: Page, action: str, args: dict[str, Any]
    ) -> None:
        """Perform clicks and hover with shared element and screenshot guards."""
        button, click_count = args.get("button", "left"), args.get("click_count", 1)
        if action == "click" and (
            button not in {"left", "right", "middle"} or click_count not in {1, 2}
        ):
            raise AgentBrowserError(
                "INVALID_ARGUMENT",
                "button 为 left/right/middle，click_count 为整数 1 或 2。",
            )
        ref, x, y = args.get("ref", ""), args.get("x", -1), args.get("y", -1)
        target = None
        try:
            if ref:
                if x != -1 or y != -1:
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT", "元素编号和坐标不能同时传入。"
                    )
                target = await self._resolve_element(session, ref)
            else:
                await self._check_coordinates(session, page, ((x, y),))
            await session.invalidate()
            if action == "hover":
                if target is not None:
                    await target.hover()
                else:
                    await page.mouse.move(x, y)
            elif target is not None:
                await target.click(button=button, click_count=click_count)
            else:
                await page.mouse.click(x, y, button=button, click_count=click_count)
        finally:
            if target is not None:
                with suppress(PlaywrightError):
                    await target.dispose()

    @staticmethod
    async def _drag_point(target: ElementHandle, page: Page) -> tuple[float, float]:
        """Use an element's center in main-frame CSS coordinates."""
        box = await target.bounding_box()
        if box is None:
            raise AgentBrowserError("STALE_ELEMENT", "拖拽元素不可见，请重新观察页面。")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        viewport = page.viewport_size or DEFAULT_VIEWPORT
        if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
            raise AgentBrowserError(
                "TARGET_OUTSIDE_VIEWPORT",
                "拖拽起点和终点需同时位于视口内，请滚动后重新观察，或使用截图坐标。",
            )
        return x, y

    async def _drag(
        self, session: AgentSession, page: Page, args: dict[str, Any]
    ) -> None:
        """Drag with real mouse events and release the button on failure or cancel."""
        source_ref, target_ref = args.get("source_ref", ""), args.get("target_ref", "")
        start = args.get("start_x", -1), args.get("start_y", -1)
        end = args.get("end_x", -1), args.get("end_y", -1)
        targets: list[ElementHandle] = []
        try:
            if source_ref or target_ref:
                if (
                    not source_ref
                    or not target_ref
                    or any(value != -1 for value in (*start, *end))
                ):
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT",
                        "拖拽需传入两个元素编号或四个截图坐标，不能混用。",
                    )
                targets.append(await self._resolve_element(session, source_ref))
                targets.append(await self._resolve_element(session, target_ref))
                await session.invalidate()
                for target in targets:
                    await self._drag_point(target, page)
                # Trial hover checks actionability, including occlusion, without
                # firing hover handlers. Check both endpoints before pressing.
                for target in reversed(targets):
                    await target.hover(trial=True)
                start = await self._drag_point(targets[0], page)
                end = await self._drag_point(targets[1], page)
            else:
                await self._check_coordinates(session, page, (start, end))
                await session.invalidate()
            await page.mouse.move(*start)
            try:
                await page.mouse.down()
                await page.mouse.move(*end, steps=20)
                # A second move over the endpoint delivers dragover for HTML5 DnD.
                await page.mouse.move(*end)
                await page.mouse.up()
            except BaseException:
                with suppress(PlaywrightError):
                    await page.mouse.up()
                raise
        finally:
            for target in targets:
                with suppress(PlaywrightError):
                    await target.dispose()

    async def _act(
        self, owner: Owner, session: AgentSession, action: str, args: dict[str, Any]
    ) -> BrowserResult:
        if action == "network":
            return BrowserResult(await session.network.operate(args))
        if action == "intercept":
            return BrowserResult(session.interceptor.operate(args))
        if action == "emulate":
            return BrowserResult({"success": True, "environment": session.environment})
        if action == "close":
            saved = await self._drop(owner, session, save_state=True)
            return BrowserResult(
                {
                    "success": True,
                    "session_closed": True,
                    "state_saved": saved,
                    "mode": session.mode,
                }
            )
        if action == "tabs":
            operation = args.get("operation", "list")
            tab_id = args.get("tab_id", "")
            if operation not in {"list", "switch", "close"}:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "operation 只能是 list、switch 或 close。"
                )
            if operation == "list":
                if tab_id:
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT", "列出标签页时不要传 tab_id。"
                    )
                return BrowserResult(
                    {
                        "success": True,
                        "active_tab": session.active_tab,
                        "tabs": [
                            {"id": key, "url": page.url}
                            for key, page in session.tabs.items()
                            if not page.is_closed()
                        ],
                    }
                )
            page = session.tabs.get(tab_id)
            if page is None or page.is_closed():
                raise AgentBrowserError("INVALID_TAB", "标签页不存在，请先列出标签页。")
            await session.invalidate()
            if operation == "switch":
                session.active_tab = tab_id
                await page.bring_to_front()
            else:
                remaining = [
                    key
                    for key, tab in session.tabs.items()
                    if key != tab_id and not tab.is_closed()
                ]
                if not remaining:
                    saved = await self._drop(owner, session, save_state=True)
                    return BrowserResult(
                        {"success": True, "session_closed": True, "state_saved": saved}
                    )
                await page.close()
                session.tabs.pop(tab_id, None)
                if session.active_tab == tab_id:
                    session.active_tab = remaining[0]
            return await self._observe(session)
        if action == "open":
            await session.invalidate()
            page = session.tabs.get(session.active_tab)
            if args.get("new_tab", False) or page is None or page.is_closed():
                if len(session.context.pages) >= MAX_TABS:
                    raise AgentBrowserError(
                        "TAB_LIMIT", f"每个会话最多打开 {MAX_TABS} 个标签页。"
                    )
                page = await session.context.new_page()
                session.active_tab = next(
                    key for key, item in session.tabs.items() if item is page
                )
            await page.goto(args["url"], wait_until="domcontentloaded")
            return await self._observe(session)
        if action == "dialog":
            return self._dialog(session, args)
        if action == "state":
            async with self.storage.lock(owner):
                saved = await session.context.storage_state(indexed_db=True)
                self.storage.write_state(owner, saved)
                session.auto_save = True
            return BrowserResult({"success": True, "saved": True})
        if action == "upload":
            return await self._upload(owner, session, args)
        page = self._page(session)
        if action == "evaluate":
            return await self._evaluate(session, page, args)
        if action == "extract":
            return await self._extract(page, args)
        if action in {"observe", "screenshot"}:
            result = await self._observe(session)
            if action == "screenshot":
                result.image = await page.screenshot(
                    type="png", full_page=False, scale="css", animations="disabled"
                )
                for snapshot in session.snapshots:
                    await snapshot.evaluate(
                        "state => { state.observer.takeRecords(); state.revision = 0; }"
                    )
                scale = await page.evaluate("visualViewport?.scale || 1")
                session.screenshot = abs(scale - 1) < 0.001
                result.data["coordinate_click_available"] = session.screenshot
                if not session.screenshot:
                    session.coordinate_notice = "当前页面存在移动端自动缩放，截图坐标与页面 CSS 坐标不同；请使用元素编号操作，或切换到未缩放的环境。"
                    result.data["coordinate_notice"] = session.coordinate_notice
            return result
        if action == "wait":
            return await self._wait(session, page, args)
        self._check_observation(session, args.get("observation_id", ""))
        if action in {"click", "hover", "drag"}:
            if action == "drag":
                await self._drag(session, page, args)
            else:
                await self._mouse_action(session, page, action, args)
            return await self._observe(session)
        if action in {"press", "scroll"}:
            for snapshot in session.snapshots:
                if not await snapshot.evaluate(
                    "state => state.document === document.documentElement && state.url === location.href"
                ):
                    raise AgentBrowserError(
                        "STALE_OBSERVATION", "网页已跳转，请重新观察后再操作。"
                    )
        target = None
        try:
            if action in {"fill", "select"}:
                target = await self._resolve_element(session, args.get("ref", ""))
            if action == "fill":
                text = args.get("text")
                if not isinstance(text, str) or len(text) > 100_000:
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT", "text 必须是最多 100000 字符的字符串。"
                    )
                await session.invalidate()
                await target.fill(text)
            elif action == "select":
                value = args.get("value")
                if not isinstance(value, str) or len(value) > 2000:
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT", "value 必须是最多 2000 字符的选项值。"
                    )
                await session.invalidate()
                await target.select_option(value=value)
            elif action == "press":
                key = args.get("key")
                if not isinstance(key, str) or not key or len(key) > 100:
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT",
                        "key 必须是按键名称，例如 Enter 或 Control+a。",
                    )
                await session.invalidate()
                await page.keyboard.press(key)
            elif action == "scroll":
                direction, pixels = (
                    args.get("direction", "down"),
                    args.get("pixels", 600),
                )
                if (
                    direction not in {"up", "down", "left", "right"}
                    or type(pixels) is not int
                    or not 1 <= pixels <= 5000
                ):
                    raise AgentBrowserError(
                        "INVALID_ARGUMENT",
                        "direction 为 up/down/left/right，pixels 为 1～5000 的整数。",
                    )
                await session.invalidate()
                dx = pixels * ({"left": -1, "right": 1}.get(direction, 0))
                dy = pixels * ({"up": -1, "down": 1}.get(direction, 0))
                await page.mouse.wheel(dx, dy)
                await page.evaluate(
                    "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
            else:
                raise AgentBrowserError("UNKNOWN_ACTION", f"未知浏览器操作：{action}")
        finally:
            if target is not None:
                with suppress(PlaywrightError):
                    await target.dispose()
        return await self._observe(session)

    async def close(self, *, pending_tasks: Iterable[asyncio.Task[Any]] = ()) -> None:
        """Stop active calls, save login state and release retained contexts."""
        self._closed = True
        current = asyncio.current_task()
        tasks = set(pending_tasks) | self._tasks | self._maintenance
        tasks.discard(current)
        if self._reaper is not None:
            tasks.add(self._reaper)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._registry_lock:
            for owner, session in list(self._sessions.items()):
                await self._drop(owner, session, save_state=True)
        self.storage.close()
