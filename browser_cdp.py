"""Connect to a user-owned local Chromium without owning its lifetime."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import suppress
from pathlib import Path

from playwright.async_api import Browser, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .browser_errors import AgentBrowserError
from .service import BrowserServiceError


def chrome_user_data_dir() -> Path:
    """Locate stable Chrome for the OS account running AstrBot, without scanning."""
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    config = (
        os.environ.get("CHROME_CONFIG_HOME")
        or os.environ.get("XDG_CONFIG_HOME")
        or str(Path.home() / ".config")
    )
    return Path(config) / "google-chrome"


def _discover_running_chrome(user_data_dir: Path | None) -> str:
    """Read Chrome's published endpoint; never launch or modify a user profile."""
    profile = user_data_dir if user_data_dir is not None else chrome_user_data_dir()
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


class LocalCDP:
    """Own only a Playwright connection, never the external browser process."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        user_data_dir: str = "",
        allow_existing_tabs: bool = False,
    ) -> None:
        for key, value in (
            ("enabled", enabled),
            ("allow_existing_tabs", allow_existing_tabs),
        ):
            if type(value) is not bool:
                raise BrowserServiceError(f"browser_use.cdp.{key} 必须是布尔值。")
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
                endpoint = await asyncio.to_thread(
                    _discover_running_chrome, self.user_data_dir
                )
            except (OSError, ValueError, KeyError) as exc:
                raise AgentBrowserError(
                    "CDP_DISCOVERY_FAILED",
                    "未找到当前 Chrome 的有效调试地址。请使用 Chrome 144+，在 chrome://inspect/#remote-debugging 启用远程调试；确认 AstrBot 与 Chrome 使用同一系统账户，或配置正确的 browser_use.cdp.user_data_dir（chrome://version 中个人资料路径的上一级目录）。",
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
                "无法连接当前 Chrome。请确认浏览器仍在运行、已在 chrome://inspect/#remote-debugging 启用远程调试，并在 Chrome 的连接授权窗口中点击允许；拒绝或超时后需显式重新请求连接。",
            ) from exc

    async def disconnect(self) -> None:
        runtime, self._runtime = self._runtime, None
        self.browser = None
        if runtime is not None:
            # Stopping this separate client detaches CDP; never send Browser.close.
            with suppress(PlaywrightError):
                await runtime.stop()
