"""Keep isolated online browser sessions for multi-turn agent tools."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger
from playwright.async_api import Browser, BrowserContext, JSHandle, Page, Route
from playwright.async_api import Error as PlaywrightError

from .service import DEFAULT_VIEWPORT, BrowserService, BrowserServiceError

OBSERVE_SCRIPT = Path(__file__).with_name("observation.js").read_text(encoding="utf-8")
MAX_TABS = 8
MAX_FRAMES = 16
Owner = tuple[str, str]
ACTION_PARAMETERS = {
    "open": {"url", "new_tab"},
    "observe": set(),
    "screenshot": set(),
    "click": {"observation_id", "ref", "x", "y"},
    "fill": {"observation_id", "ref", "text"},
    "press": {"observation_id", "key"},
    "select": {"observation_id", "ref", "value"},
    "scroll": {"observation_id", "direction", "pixels"},
    "wait": {"text"},
    "tabs": {"operation", "tab_id"},
    "close": set(),
}


class AgentBrowserError(BrowserServiceError):
    """An actionable error with a stable code for the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BrowserResult:
    """Return textual page state and, optionally, image bytes."""

    data: dict[str, Any]
    image: bytes | None = None


@dataclass
class AgentSession:
    """Own one user's live context, tabs and latest observation."""

    context: BrowserContext
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tabs: dict[str, Page] = field(default_factory=dict)
    active_tab: str = ""
    next_tab: int = 0
    last_used: float = field(default_factory=time.monotonic)
    observation_id: str = ""
    observation_tab: str = ""
    refs: dict[str, tuple[JSHandle, int]] = field(default_factory=dict)
    snapshots: list[JSHandle] = field(default_factory=list)
    screenshot: bool = False
    notices: list[str] = field(default_factory=list)

    async def invalidate(self) -> None:
        """Disconnect observation guards and release remote DOM references."""
        self.observation_id = ""
        self.observation_tab = ""
        self.refs.clear()
        self.screenshot = False
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
    """

    def __init__(
        self,
        service: BrowserService,
        *,
        max_sessions: int = 4,
        idle_timeout: int = 600,
        action_timeout: int = 30,
    ) -> None:
        for key, value, lower, upper in (
            ("browser_use.max_sessions", max_sessions, 1, 16),
            ("browser_use.idle_timeout", idle_timeout, 30, 3600),
            ("browser_use.action_timeout", action_timeout, 1, 120),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise BrowserServiceError(f"{key} 必须是 {lower}～{upper} 的整数。")
        self.service = service
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.action_timeout = action_timeout
        self._sessions: dict[Owner, AgentSession] = {}
        self._registry_lock = asyncio.Lock()
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
        async with self._registry_lock:
            expired = [
                owner
                for owner, session in self._sessions.items()
                if not session.lock.locked()
                and time.monotonic() - session.last_used >= self.idle_timeout
            ]
            for owner in expired:
                await self._drop(owner, self._sessions[owner])

    async def _drop(self, owner: Owner, session: AgentSession) -> None:
        if self._sessions.get(owner) is session:
            del self._sessions[owner]
        try:
            await asyncio.wait_for(session.context.close(), timeout=5)
        except (PlaywrightError, asyncio.TimeoutError):
            logger.warning("Failed to close an agent browser context", exc_info=True)
        finally:
            session.refs.clear()
            session.snapshots.clear()

    @staticmethod
    async def _route(route: Route) -> None:
        if urlsplit(route.request.url).scheme in {"http", "https"}:
            await route.continue_()
        else:
            await route.abort()

    async def _get_session(
        self, owner: Owner, browser: Browser, *, create: bool
    ) -> AgentSession:
        async with self._registry_lock:
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
                if (
                    not session.lock.locked()
                    and time.monotonic() - session.last_used >= self.idle_timeout
                ):
                    await self._drop(owner, session)
                    raise AgentBrowserError(
                        "SESSION_EXPIRED", "浏览器会话已空闲过期，请重新打开网址。"
                    )
                return session
            if not create:
                raise AgentBrowserError(
                    "NO_SESSION", "当前没有浏览器会话，请先调用 browser_open。"
                )
            if len(self._sessions) >= self.max_sessions:
                raise AgentBrowserError(
                    "SESSION_LIMIT",
                    "浏览器会话数量已达上限，请关闭不用的会话或稍后重试。",
                )
            context = await browser.new_context(
                viewport=dict(DEFAULT_VIEWPORT),
                device_scale_factor=1,
                java_script_enabled=True,
                service_workers="block",
                accept_downloads=False,
            )
            session = AgentSession(context)
            try:
                context.set_default_timeout(self.action_timeout * 1000)
                context.set_default_navigation_timeout(self.action_timeout * 1000)
                await context.route("**/*", self._route)
                context.on("page", lambda page: self._register_page(session, page))

                async def dismiss_dialog(dialog: Any) -> None:
                    session.notices.append(
                        f"已关闭网页弹窗（{dialog.type}）：{dialog.message[:300]}"
                    )
                    del session.notices[:-5]
                    await dialog.dismiss()

                context.on("dialog", dismiss_dialog)
                self._sessions[owner] = session
                return session
            except BaseException:
                await context.close()
                raise

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
        page.on("close", lambda: session.tabs.pop(tab_id, None))
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
        for frame in page.frames[:MAX_FRAMES]:
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
            frames.append({"url": frame.url[:2048], "name": frame.name[:200], **data})
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
            }
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
        if (
            not isinstance(owner, tuple)
            or len(owner) != 2
            or any(not isinstance(item, str) or not item for item in owner)
        ):
            raise AgentBrowserError("INVALID_OWNER", "无法确定消息来源和发送者。")
        if not isinstance(action, str) or action not in ACTION_PARAMETERS:
            raise AgentBrowserError("UNKNOWN_ACTION", "未知浏览器操作。")
        unknown = set(arguments) - ACTION_PARAMETERS[action]
        if unknown:
            raise AgentBrowserError(
                "INVALID_ARGUMENT", "未知工具参数：" + ", ".join(sorted(unknown))
            )
        for key, value in arguments.items():
            if key in {"new_tab", "pixels"}:
                valid = type(value) is (bool if key == "new_tab" else int)
            elif key in {"x", "y"}:
                valid = type(value) in {int, float} and math.isfinite(value)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise AgentBrowserError("INVALID_ARGUMENT", f"{key} 的类型或数值无效。")
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            return await asyncio.wait_for(
                self._execute(owner, action, arguments), timeout=self.action_timeout
            )
        except asyncio.TimeoutError as exc:
            raise AgentBrowserError(
                "TIMEOUT",
                "浏览器工具超时；操作可能已执行，请重新观察确认结果，不要直接重复操作。",
            ) from exc
        except PlaywrightError as exc:
            raise AgentBrowserError(
                "BROWSER_ERROR",
                "浏览器操作失败，请重新观察页面后决定下一步。"
                + str(exc).split("\n", 1)[0][:250],
            ) from exc
        except BrowserServiceError as exc:
            if isinstance(exc, AgentBrowserError):
                raise
            raise AgentBrowserError("SERVICE_UNAVAILABLE", str(exc)) from exc
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def _execute(
        self, owner: Owner, action: str, args: dict[str, Any]
    ) -> BrowserResult:
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
        async with self.service.operation() as browser:
            session = await self._get_session(owner, browser, create=action == "open")
            async with session.lock:
                if self._sessions.get(owner) is not session:
                    raise AgentBrowserError(
                        "SESSION_EXPIRED", "会话已关闭，请重新打开网址。"
                    )
                session.last_used = time.monotonic()
                try:
                    return await self._act(owner, session, action, args)
                except BaseException:
                    await session.invalidate()
                    raise
                finally:
                    session.last_used = time.monotonic()

    async def _act(
        self, owner: Owner, session: AgentSession, action: str, args: dict[str, Any]
    ) -> BrowserResult:
        if action == "close":
            await self._drop(owner, session)
            return BrowserResult({"success": True, "session_closed": True})
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
                await page.close()
                session.tabs.pop(tab_id, None)
                remaining = [
                    key for key, tab in session.tabs.items() if not tab.is_closed()
                ]
                if not remaining:
                    await self._drop(owner, session)
                    return BrowserResult({"success": True, "session_closed": True})
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
        page = self._page(session)
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
                session.screenshot = True
                result.data["coordinate_click_available"] = True
            return result
        if action == "wait":
            text = args.get("text")
            if not isinstance(text, str) or not text or len(text) > 2000:
                raise AgentBrowserError(
                    "INVALID_ARGUMENT", "text 必须是 1～2000 字符的待出现文字。"
                )
            await session.invalidate()
            await (
                page.get_by_text(text, exact=False)
                .filter(visible=True)
                .first.wait_for(state="visible")
            )
            return await self._observe(session)
        self._check_observation(session, args.get("observation_id", ""))
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
            if action in {"click", "fill", "select"}:
                ref = args.get("ref", "")
                x, y = args.get("x", -1), args.get("y", -1)
                coordinates = action == "click" and not ref
                if coordinates:
                    if not session.screenshot:
                        raise AgentBrowserError(
                            "SCREENSHOT_REQUIRED", "坐标点击前请先获取当前视口截图。"
                        )
                    viewport = page.viewport_size or DEFAULT_VIEWPORT
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
                            state.url === location.href"""
                        )
                        if not valid:
                            raise AgentBrowserError(
                                "STALE_SCREENSHOT",
                                "页面已变化，请重新截图后再点击坐标。",
                            )
                else:
                    if action == "click" and (x != -1 or y != -1):
                        raise AgentBrowserError(
                            "INVALID_ARGUMENT", "元素编号和坐标不能同时传入。"
                        )
                    reference = session.refs.get(ref)
                    if reference is None:
                        raise AgentBrowserError(
                            "INVALID_REF", "元素编号不存在，请重新观察页面。"
                        )
                    snapshot, index = reference
                    valid = await snapshot.evaluate(
                        """(state, index) => state.elements[index]?.isConnected &&
                        state.signature(state.elements[index]) === state.signatures[index]""",
                        index,
                    )
                    if not valid:
                        raise AgentBrowserError(
                            "STALE_ELEMENT", "目标元素已变化，请重新观察页面。"
                        )
                    elements = await snapshot.get_property("elements")
                    try:
                        handle = await elements.get_property(str(index))
                        target = handle.as_element()
                    finally:
                        await elements.dispose()
                    if target is None:
                        raise AgentBrowserError(
                            "STALE_ELEMENT", "目标元素已失效，请重新观察页面。"
                        )
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
            elif action == "click":
                await session.invalidate()
                if target is not None:
                    await target.click()
                else:
                    await page.mouse.click(x, y)
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

    async def close(self) -> None:
        """Cancel active calls and release the reaper and retained contexts."""
        self._closed = True
        current = asyncio.current_task()
        tasks = [
            task for task in self._tasks | self._maintenance if task is not current
        ]
        if self._reaper is not None:
            tasks.append(self._reaper)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._registry_lock:
            for owner, session in list(self._sessions.items()):
                await self._drop(owner, session)
