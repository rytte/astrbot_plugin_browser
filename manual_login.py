"""Keep administrator login pages separate from model-controlled sessions."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from .agent_browser import STATE_SAVE_TIMEOUT, AgentBrowser, Owner
from .browser_advanced import http_url
from .browser_errors import AgentBrowserError

WIDTH, HEIGHT = 1100, 720
IDLE_SECONDS = 30
MAX_SECONDS = 600
OFFER_SECONDS = 120
KEYS = {
    "Enter",
    "Tab",
    "Shift+Tab",
    "Escape",
    "Backspace",
    "Delete",
    "Home",
    "End",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "ControlOrMeta+A",
}


@dataclass
class LoginSession:
    owner: Owner
    username: str
    context: BrowserContext
    token: str = field(default_factory=secrets.token_urlsafe)
    started: float = field(default_factory=time.monotonic)
    heartbeat: float = field(default_factory=time.monotonic)
    last_frame: float = 0
    page: Page | None = None
    tabs: dict[str, Page] = field(default_factory=dict)
    frame_id: str = ""
    frame_url: str = ""
    navigation: asyncio.Task | None = None


class ManualLogin:
    """Give one Dashboard client a temporary lease on an isolated Context."""

    def __init__(self, agent: AgentBrowser) -> None:
        self.agent = agent
        self.lock = asyncio.Lock()
        self.offer: tuple[str, Owner, float] | None = None
        self.session: LoginSession | None = None
        self.closed = False
        self.reaper: asyncio.Task | None = None

    async def request(self, owner: Owner) -> None:
        """Register a target using the identity derived from a chat event."""
        self.agent.validate_owner(owner)
        async with self.lock:
            if self.closed:
                raise AgentBrowserError("LOGIN_CLOSED", "人工登录已停止。")
            if self.session is not None:
                raise AgentBrowserError("LOGIN_BUSY", "已有人工登录在进行。")
            if (
                self.offer
                and self.offer[1] != owner
                and self.offer[2] > time.monotonic()
            ):
                raise AgentBrowserError("LOGIN_BUSY", "已有聊天会话正在等待人工登录。")
            self.offer = (
                secrets.token_urlsafe(),
                owner,
                time.monotonic() + OFFER_SECONDS,
            )

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(3)
            async with self.lock:
                session = self.session
                if session is not None and (
                    time.monotonic()
                    >= min(
                        session.heartbeat + IDLE_SECONDS, session.started + MAX_SECONDS
                    )
                    or not session.context.browser.is_connected()
                ):
                    await self._finish()

    async def close(self) -> None:
        """Discard unfinished login data before stopping the shared browser."""
        self.closed = True
        if self.reaper is not None:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        async with self.lock:
            self.offer = None
            await self._finish()

    async def _finish(self, *, save: bool = False) -> None:
        session = self.session
        if session is None:
            return
        async with self.agent._owner_lock(session.owner):
            if save:
                state = await asyncio.wait_for(
                    session.context.storage_state(indexed_db=True), STATE_SAVE_TIMEOUT
                )
                async with self.agent.storage.lock(session.owner):
                    self.agent.storage.write_state(session.owner, state)
            self.session = None
            try:
                if session.navigation is not None:
                    session.navigation.cancel()
                    await asyncio.gather(session.navigation, return_exceptions=True)
                with suppress(PlaywrightError, asyncio.TimeoutError):
                    await asyncio.wait_for(session.context.close(), 5)
            finally:
                self.agent.manual_login_owner = None

    async def _start(self, username: str, body: dict) -> dict:
        offer = self.offer
        url = body.get("url")
        if not offer or offer[2] <= time.monotonic() or body.get("offer") != offer[0]:
            raise AgentBrowserError("LOGIN_OFFER", "申请已失效，请在目标聊天重新申请。")
        if self.session is not None:
            raise AgentBrowserError("LOGIN_BUSY", "已有人工登录在进行。")
        if (
            not isinstance(url, str)
            or len(url) > 2048
            or not http_url(url)
            or urlsplit(url).scheme != "https"
        ):
            raise AgentBrowserError("LOGIN_URL", "请输入完整的 HTTPS 登录地址。")
        owner = offer[1]
        context = None
        async with self.agent._owner_lock(owner):
            if self.closed:
                raise AgentBrowserError("LOGIN_CLOSED", "人工登录已停止。")
            self.agent.manual_login_owner = owner
            try:
                previous = self.agent._sessions.get(owner)
                if previous is not None:
                    previous.network.active = False
                    previous.network.entries.clear()
                    previous.network.requests.clear()
                    await self.agent._drop(owner, previous, save_state=True)
                state = None
                async with self.agent.storage.lock(owner):
                    try:
                        state = self.agent.storage.read_state(owner)
                    except AgentBrowserError as exc:
                        if exc.code != "STATE_NOT_FOUND":
                            raise
                async with self.agent.service._operation() as browser:
                    context = await browser.new_context(
                        viewport={"width": WIDTH, "height": HEIGHT},
                        storage_state=state,
                        accept_downloads=False,
                    )
                    self.agent.service.track_context(context)
                    context.set_default_timeout(4000)
                    page = await context.new_page()
                session = LoginSession(owner, username, context, page=page)
                # The popup or login site can stay busy while the administrator interacts.
                session.navigation = asyncio.create_task(self._navigate(page, url))
                self.session = session
                self.offer = None
                if self.reaper is None:
                    self.reaper = asyncio.create_task(self._reap())
                return {"token": session.token}
            finally:
                if self.session is None:
                    self.agent.manual_login_owner = None
                    if context is not None:
                        with suppress(PlaywrightError, asyncio.TimeoutError):
                            await asyncio.wait_for(context.close(), 5)

    @staticmethod
    async def _navigate(page: Page, url: str) -> None:
        with suppress(PlaywrightError):
            await page.goto(url, wait_until="commit", timeout=15_000)

    async def operate(self, username: str, body: dict) -> dict:
        """Authorize and serialize one action from the Dashboard controller."""
        async with self.lock:
            if self.closed:
                raise AgentBrowserError("LOGIN_CLOSED", "人工登录已停止。")
            session = self.session
            now = time.monotonic()
            if session is not None and (
                now
                >= min(session.heartbeat + IDLE_SECONDS, session.started + MAX_SECONDS)
                or not session.context.browser.is_connected()
            ):
                await self._finish()
                session = None
            action = body.get("action")
            if action == "status":
                offer = self.offer
                return {
                    "busy": session is not None,
                    "offer": {
                        "id": offer[0],
                        "origin": offer[1][0],
                        "sender": offer[1][1],
                    }
                    if offer and offer[2] > now
                    else None,
                }
            if action == "start":
                return await self._start(username, body)
            token = body.get("token")
            if (
                session is None
                or session.username != username
                or not isinstance(token, str)
                or not token.isascii()
                or not secrets.compare_digest(token, session.token)
            ):
                raise AgentBrowserError("LOGIN_SESSION", "接管已结束或无权访问。")
            if action in ("save", "cancel"):
                if action == "save" and (
                    session.page is None
                    or session.page.is_closed()
                    or not session.frame_id
                    or session.page.url != session.frame_url
                    or not session.frame_url.startswith("https://")
                ):
                    raise AgentBrowserError(
                        "LOGIN_PAGE", "等登录页面加载完成后再保存。"
                    )
                await self._finish(save=action == "save")
                return {"saved": action == "save"}
            if action == "frame":
                if now - session.last_frame < 0.25:
                    return {"skipped": True}
                result = await self._frame(session)
                session.last_frame = now
                session.heartbeat = time.monotonic()
                return result
            return await self._input(session, body)

    async def _frame(self, session: LoginSession) -> dict:
        pages = session.context.pages
        old_pages = set(session.tabs.values())
        session.tabs = {
            key: page for key, page in session.tabs.items() if page in pages
        }
        for page in pages:
            if page not in old_pages:
                session.tabs[secrets.token_urlsafe(12)] = page
                session.page = page
                session.frame_id = ""
        page = session.page
        if page not in pages:
            if not pages:
                await self._finish()
                raise AgentBrowserError("LOGIN_SESSION", "登录页面已关闭。")
            page = session.page = pages[-1]
            session.frame_id = ""
        url = page.url
        screenshot = await page.screenshot(type="jpeg", quality=65, timeout=4000)
        if page.url != url:
            session.frame_id = ""
            return {"skipped": True}
        if not session.frame_id or session.frame_url != url:
            session.frame_id = secrets.token_urlsafe(12)
            session.frame_url = url
        return {
            "frame": session.frame_id,
            "jpeg": base64.b64encode(screenshot).decode("ascii"),
            "selected": next(key for key, tab in session.tabs.items() if tab is page),
            "tabs": [
                {
                    "id": key,
                    "site": f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"
                    if parts.hostname
                    else "空白页",
                }
                for key, tab in session.tabs.items()
                for parts in [urlsplit(tab.url)]
            ],
        }

    async def _input(self, session: LoginSession, body: dict) -> dict:
        action = body.get("action")
        page = session.page
        if action == "tab":
            tab = body.get("tab")
            if (
                isinstance(tab, str)
                and tab in session.tabs
                and not session.tabs[tab].is_closed()
            ):
                session.page = session.tabs[tab]
                session.frame_id = ""
                return {}
        elif (
            page is None
            or page.is_closed()
            or not session.frame_id
            or body.get("frame") != session.frame_id
            or page.url != session.frame_url
            or any(tab not in session.tabs.values() for tab in session.context.pages)
        ):
            raise AgentBrowserError("LOGIN_FRAME", "页面已变化，请等待画面刷新后重试。")
        elif not session.frame_url.startswith("https://"):
            raise AgentBrowserError("LOGIN_URL", "只能在 HTTPS 页面中输入或点击。")
        elif action == "text":
            value = body.get("text")
            if (
                isinstance(value, str)
                and 0 < len(value) <= 1024
                and value.isprintable()
            ):
                await page.keyboard.insert_text(value)
                return {}
        elif action == "key":
            key = body.get("key")
            if isinstance(key, str) and key in KEYS:
                await page.keyboard.press(key)
                return {}
        elif action in ("click", "scroll"):
            x, y = body.get("x"), body.get("y")
            if type(x) is int and type(y) is int and 0 <= x < WIDTH and 0 <= y < HEIGHT:
                if action == "click":
                    await page.mouse.click(x, y)
                    return {}
                dx, dy = body.get("dx"), body.get("dy")
                if (
                    type(dx) is int
                    and type(dy) is int
                    and max(abs(dx), abs(dy)) <= 1200
                ):
                    await page.mouse.move(x, y)
                    await page.mouse.wheel(dx, dy)
                    return {}
        raise AgentBrowserError("LOGIN_INPUT", "无效的浏览器操作。")
