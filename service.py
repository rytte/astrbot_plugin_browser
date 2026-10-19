"""Manage a resident headless Chromium and an optional headed Chromium."""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from astrbot.api import logger
from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
MAX_HTML_BYTES = 48 * 1024 * 1024
MAX_VIEWPORT_PIXELS = 16_777_216
MAX_SCREENSHOT_PIXELS = 32_000_000
MAX_TIMEOUT = 300
BROWSER_INSTALL_TIMEOUT = 300


def _system_browser_candidates() -> list[Path]:
    """Return common Chromium executable locations for the current platform."""
    candidates: list[Path] = []
    commands = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chrome",
        "msedge",
        "brave-browser",
        "vivaldi",
        "opera",
    )
    for command in commands:
        executable = shutil.which(command)
        if executable:
            candidates.append(Path(executable))

    system = platform.system()
    if system == "Windows":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        relative_paths = (
            "Google\\Chrome\\Application\\chrome.exe",
            "Microsoft\\Edge\\Application\\msedge.exe",
            "Chromium\\Application\\chrome.exe",
            "BraveSoftware\\Brave-Browser\\Application\\brave.exe",
            "Vivaldi\\Application\\vivaldi.exe",
            "Programs\\Opera\\opera.exe",
        )
        candidates.extend(
            Path(root) / relative
            for root in roots
            if root
            for relative in relative_paths
        )
    elif system == "Darwin":
        app_roots = (Path("/Applications"), Path.home() / "Applications")
        app_paths = (
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Chromium.app/Contents/MacOS/Chromium",
            "Brave Browser.app/Contents/MacOS/Brave Browser",
            "Vivaldi.app/Contents/MacOS/Vivaldi",
            "Opera.app/Contents/MacOS/Opera",
        )
        candidates.extend(
            root / relative for root in app_roots for relative in app_paths
        )
    else:
        candidates.extend(
            Path(path)
            for path in (
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
                "/usr/bin/brave-browser",
                "/usr/bin/vivaldi",
                "/usr/bin/opera",
                "/snap/bin/chromium",
            )
        )
    return candidates


def find_system_chromium() -> Path | None:
    """Find an existing Chromium-compatible browser without starting it."""
    seen: set[Path] = set()
    for candidate in _system_browser_candidates():
        try:
            candidate = candidate.expanduser()
        except RuntimeError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


class BrowserServiceError(RuntimeError):
    """A browser service failure that is safe to return to a plugin caller."""


class BrowserService:
    """Own shared Chromium runtimes and create isolated contexts for each task.

    Args:
        browser_executable: Optional absolute path to a Chromium-based browser.
        max_concurrent_sessions: Maximum number of concurrent browser operations.
        queue_timeout: Maximum time in seconds to wait for an execution slot.
        startup_timeout: Maximum time in seconds allowed for a browser launch.
    """

    def __init__(
        self,
        browser_executable: str = "",
        max_concurrent_sessions: int = 4,
        queue_timeout: int = 30,
        startup_timeout: int = 30,
    ) -> None:
        if not isinstance(browser_executable, str):
            raise BrowserServiceError("browser_executable 必须是字符串。")
        if browser_executable:
            if (
                browser_executable != browser_executable.strip()
                or any(char in browser_executable for char in "\r\n\0")
                or browser_executable[0] in "\"'“”‘’"
                or browser_executable[-1] in "\"'“”‘’"
            ):
                raise BrowserServiceError(
                    "browser_executable 请填写不带引号和首尾空白的绝对路径。"
                )
            path = Path(browser_executable)
            if not path.is_absolute() or not path.is_file():
                raise BrowserServiceError(
                    "browser_executable 必须指向已存在的 Chromium 系浏览器可执行文件。"
                )
        if (
            type(max_concurrent_sessions) is not int
            or not 1 <= max_concurrent_sessions <= 16
        ):
            raise BrowserServiceError("max_concurrent_sessions 必须是 1～16 的整数。")
        if type(queue_timeout) is not int or not 1 <= queue_timeout <= 300:
            raise BrowserServiceError("queue_timeout 必须是 1～300 的整数。")
        if type(startup_timeout) is not int or not 1 <= startup_timeout <= 120:
            raise BrowserServiceError("startup_timeout 必须是 1～120 的整数。")

        self.browser_executable = browser_executable
        self.max_concurrent_sessions = max_concurrent_sessions
        self.queue_timeout = queue_timeout
        self.startup_timeout = startup_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent_sessions)
        self._lifecycle_lock = asyncio.Lock()
        self._headed_lock = asyncio.Lock()
        self._runtime_lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._headed_browser: Browser | None = None
        self._launch_options: dict[str, Any] = {}
        self._launching = 0
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._closed = False

    @property
    def ready(self) -> bool:
        """Return whether the browser process is connected and accepting work."""
        return (
            not self._closing
            and not self._closed
            and self._browser is not None
            and self._browser.is_connected()
        )

    async def initialize(self) -> None:
        """Start Chromium and fail plugin loading if it cannot be launched.

        Raises:
            BrowserServiceError: The service is closed or browser startup fails.
        """
        if self._closing or self._closed:
            raise BrowserServiceError("浏览器服务已停止。")
        try:
            await asyncio.wait_for(
                self._ensure_browser(), timeout=self._browser_start_timeout
            )
        except asyncio.TimeoutError as exc:
            raise BrowserServiceError(
                f"浏览器启动或自动安装超过 {self._browser_start_timeout} 秒。"
            ) from exc

    @property
    def _browser_start_timeout(self) -> int:
        """Allow the first startup to include a one-time browser download."""
        if self.browser_executable:
            return self.startup_timeout
        return self.startup_timeout + BROWSER_INSTALL_TIMEOUT

    async def _install_playwright_chromium(self) -> None:
        """Install Playwright's bundled Chromium in the current Python environment."""
        logger.info("No Chromium executable found; installing Playwright Chromium")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BrowserServiceError(
                "无法自动安装 Playwright Chromium。请确认当前 Python 环境可执行 "
                "python -m playwright install chromium。"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=BROWSER_INSTALL_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
            raise BrowserServiceError(
                f"自动安装 Playwright Chromium 超过 {BROWSER_INSTALL_TIMEOUT} 秒。"
            ) from exc
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
            raise

        if process.returncode != 0:
            details = (stderr or stdout or b"").decode("utf-8", errors="replace")
            details = details.strip()[-1000:]
            suffix = f"\n安装输出：{details}" if details else ""
            raise BrowserServiceError(
                "自动安装 Playwright Chromium 失败，请检查网络和写入权限，或手动运行 "
                "python -m playwright install chromium。" + suffix
            )

    async def _runtime_options(self) -> dict[str, Any]:
        async with self._runtime_lock:
            if self._playwright is not None:
                return dict(self._launch_options)
            runtime = await async_playwright().start()
            options: dict[str, Any] = {}
            try:
                if self.browser_executable:
                    options["executable_path"] = self.browser_executable
                else:
                    bundled = Path(
                        getattr(runtime.chromium, "executable_path", "") or ""
                    )
                    if not bundled.is_file():
                        selected = find_system_chromium()
                        if selected is not None:
                            options["executable_path"] = str(selected)
                            logger.info(
                                "Using detected Chromium executable: %s", selected
                            )
                        else:
                            await self._install_playwright_chromium()
            except BaseException:
                await runtime.stop()
                raise
            self._playwright = runtime
            self._launch_options = options
            return dict(options)

    async def _ensure_browser(self, mode: str = "headless") -> Browser:
        if mode not in {"headless", "headed"}:
            raise BrowserServiceError("浏览器模式必须是 headless 或 headed。")
        if self._closing or self._closed:
            raise BrowserServiceError("浏览器服务正在停止。")
        lock = self._headed_lock if mode == "headed" else self._lifecycle_lock
        async with lock:
            if self._closing or self._closed:
                raise BrowserServiceError("浏览器服务正在停止。")
            browser = self._headed_browser if mode == "headed" else self._browser
            if browser is not None and browser.is_connected():
                return browser
            if mode == "headed":
                self._headed_browser = None
            else:
                self._browser = None
            self._launching += 1
            try:
                launch_options = await self._runtime_options()
                launch_options["headless"] = mode == "headless"
                try:
                    browser = await asyncio.wait_for(
                        self._playwright.chromium.launch(**launch_options),
                        timeout=self.startup_timeout,
                    )
                except asyncio.TimeoutError as exc:
                    raise BrowserServiceError(
                        f"浏览器启动超过 {self.startup_timeout} 秒。"
                    ) from exc
                if mode == "headed":
                    self._headed_browser = browser
                else:
                    self._browser = browser
                return browser
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, BrowserServiceError):
                    raise
                logger.exception("%s browser failed to start", mode)
                if mode == "headed":
                    raise BrowserServiceError(
                        "有头浏览器启动失败，请确认 AstrBot 所在主机具有可用的图形桌面和浏览器依赖。"
                    ) from exc
                if self.browser_executable or self._launch_options.get(
                    "executable_path"
                ):
                    raise BrowserServiceError(
                        "配置或自动发现的浏览器启动失败；不会自动切换到其他浏览器。"
                    ) from exc
                raise BrowserServiceError(
                    "Playwright Chromium 启动失败。请检查浏览器依赖，或运行 "
                    "python -m playwright install chromium。"
                ) from exc
            finally:
                self._launching -= 1
                async with self._runtime_lock:
                    if (
                        self._launching == 0
                        and self._browser is None
                        and self._headed_browser is None
                    ):
                        await self._stop_runtime()

    async def close_headed(self, browser: Browser | None = None) -> None:
        """Release only the optional headed process, retaining the shared driver."""
        async with self._headed_lock:
            if browser is not None and self._headed_browser is not browser:
                return
            current, self._headed_browser = self._headed_browser, None
            if current is not None:
                try:
                    await asyncio.wait_for(current.close(), timeout=5)
                except (PlaywrightError, asyncio.TimeoutError):
                    logger.warning("Failed to close headed Chromium", exc_info=True)

    async def _stop_runtime(self) -> None:
        browser, self._browser = self._browser, None
        headed, self._headed_browser = self._headed_browser, None
        playwright, self._playwright = self._playwright, None
        self._launch_options = {}
        for browser in (headed, browser):
            if browser is None:
                continue
            try:
                await browser.close()
            except Exception:
                logger.warning("Failed to close shared Chromium", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                logger.warning("Failed to stop the Playwright driver", exc_info=True)

    @staticmethod
    async def _abort_request(route: Any) -> None:
        await route.abort()

    @asynccontextmanager
    async def operation(self, mode: str = "headless") -> AsyncIterator[Browser]:
        """Reserve execution capacity without retaining it between agent turns.

        Args:
            mode: Select the resident headless or lazily started headed browser.

        Yields:
            The connected shared browser.

        Raises:
            BrowserServiceError: The service is stopped or capacity is unavailable.
        """
        if self._closing or self._closed:
            raise BrowserServiceError("浏览器服务不可用。")
        task = asyncio.current_task()
        registered = task is not None and task not in self._active_tasks
        if registered:
            self._active_tasks.add(task)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self.queue_timeout
                )
            except asyncio.TimeoutError as exc:
                raise BrowserServiceError("浏览器任务排队超时，请稍后重试。") from exc
            acquired = True
            if self._closing or self._closed:
                raise BrowserServiceError("浏览器服务不可用。")
            yield await asyncio.wait_for(
                self._ensure_browser(mode), timeout=self._browser_start_timeout
            )
        finally:
            if acquired:
                self._semaphore.release()
            if registered:
                self._active_tasks.discard(task)

    @staticmethod
    def _validate_viewport(viewport: dict[str, int] | None) -> dict[str, int]:
        value = dict(DEFAULT_VIEWPORT) if viewport is None else viewport
        if not isinstance(value, dict) or set(value) != {"width", "height"}:
            raise BrowserServiceError("viewport 必须只包含 width 和 height。")
        width, height = value["width"], value["height"]
        if (
            type(width) is not int
            or type(height) is not int
            or not 1 <= width <= 8192
            or not 1 <= height <= 8192
            or width * height > MAX_VIEWPORT_PIXELS
        ):
            raise BrowserServiceError(
                f"viewport 尺寸无效；单个视口最多 {MAX_VIEWPORT_PIXELS} 像素。"
            )
        return {"width": width, "height": height}

    @staticmethod
    def _validate_timeout(timeout: int | float) -> float:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0.1 <= timeout <= MAX_TIMEOUT
        ):
            raise BrowserServiceError(f"timeout 必须是 0.1～{MAX_TIMEOUT} 秒。")
        return float(timeout)

    @staticmethod
    def _validate_html(html: str) -> None:
        if not isinstance(html, str):
            raise BrowserServiceError("html 必须是字符串。")
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise BrowserServiceError("HTML 超过 48 MiB 的服务上限。")

    @asynccontextmanager
    async def session(
        self,
        *,
        viewport: dict[str, int] | None = None,
        javascript_enabled: bool = False,
        device_scale_factor: float = 1,
        timeout: int | float = 30,
    ) -> AsyncIterator[Page]:
        """Open an isolated offline page and close its context on every exit.

        Args:
            viewport: CSS viewport dimensions for this task.
            javascript_enabled: Enable page scripts for this task only.
            device_scale_factor: Device pixel ratio from 0.5 to 3.
            timeout: Default Playwright action timeout in seconds.

        Yields:
            A Playwright page backed by an isolated browser context.

        Raises:
            BrowserServiceError: The service is unavailable or the queue is full.
        """
        viewport = self._validate_viewport(viewport)
        timeout = self._validate_timeout(timeout)
        if type(javascript_enabled) is not bool:
            raise BrowserServiceError("javascript_enabled 必须是布尔值。")
        if (
            isinstance(device_scale_factor, bool)
            or not isinstance(device_scale_factor, int | float)
            or not 0.5 <= device_scale_factor <= 3
        ):
            raise BrowserServiceError("device_scale_factor 必须是 0.5～3。")
        if (
            viewport["width"] * viewport["height"] * device_scale_factor**2
            > MAX_VIEWPORT_PIXELS
        ):
            raise BrowserServiceError(
                f"视口乘设备像素比后最多允许 {MAX_VIEWPORT_PIXELS} 像素。"
            )
        if self._closing or self._closed:
            raise BrowserServiceError("浏览器服务不可用。")

        try:
            async with self.operation() as browser:
                context = None
                try:
                    context = await browser.new_context(
                        viewport=viewport,
                        device_scale_factor=float(device_scale_factor),
                        java_script_enabled=javascript_enabled,
                        service_workers="block",
                    )
                    await context.route("**/*", self._abort_request)
                    page = await context.new_page()
                    timeout_ms = int(timeout * 1000)
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)
                    yield page
                finally:
                    if context is not None:
                        try:
                            await asyncio.wait_for(context.close(), timeout=5)
                        except asyncio.CancelledError:
                            with suppress(Exception):
                                await asyncio.shield(context.close())
                            raise
                        except Exception:
                            logger.warning(
                                "Failed to close a browser context", exc_info=True
                            )
        except asyncio.TimeoutError as exc:
            raise BrowserServiceError("浏览器启动或创建页面超时。") from exc

    async def render_html(
        self,
        html: str,
        *,
        viewport: dict[str, int] | None = None,
        selector: str | None = None,
        full_page: bool = True,
        javascript_enabled: bool = False,
        timeout: int | float = 30,
        max_pixels: int | None = MAX_SCREENSHOT_PIXELS,
    ) -> bytes:
        """Render offline HTML as PNG bytes.

        Args:
            html: Complete HTML document with inline styles and assets.
            viewport: CSS viewport dimensions for this task.
            selector: Optional CSS selector whose element should be captured.
            full_page: Capture the full document when no selector is provided.
            javascript_enabled: Enable scripts in the isolated page if required.
            timeout: Maximum render duration in seconds, including queue wait.
            max_pixels: Maximum screenshot area; set to None to disable this area check.

        Returns:
            PNG image bytes.

        Raises:
            BrowserServiceError: Input is invalid, too large, or rendering fails.
        """
        self._validate_html(html)
        timeout = self._validate_timeout(timeout)
        if type(full_page) is not bool:
            raise BrowserServiceError("full_page 必须是布尔值。")
        if selector is not None and (not isinstance(selector, str) or not selector):
            raise BrowserServiceError("selector 必须是非空字符串或 None。")
        if max_pixels is not None and (type(max_pixels) is not int or max_pixels < 1):
            raise BrowserServiceError("max_pixels 必须是正整数或 None。")

        async def render() -> bytes:
            async with self.session(
                viewport=viewport,
                javascript_enabled=javascript_enabled,
                timeout=timeout,
            ) as page:
                await page.set_content(
                    html, wait_until="load", timeout=int(timeout * 1000)
                )
                await page.evaluate("document.fonts.ready")
                if selector:
                    target = page.locator(selector)
                    box = await target.bounding_box(timeout=int(timeout * 1000))
                    if box is None:
                        raise BrowserServiceError(f"截图目标不存在或不可见：{selector}")
                    pixels = box["width"] * box["height"]
                    if max_pixels is not None and pixels > max_pixels:
                        raise BrowserServiceError("截图目标超过像素面积上限。")
                    return await target.screenshot(
                        type="png",
                        animations="disabled",
                        timeout=int(timeout * 1000),
                    )

                if full_page and max_pixels is not None:
                    dimensions = await page.evaluate(
                        """() => ({
                            width: Math.max(
                                document.documentElement.scrollWidth,
                                document.body?.scrollWidth || 0
                            ),
                            height: Math.max(
                                document.documentElement.scrollHeight,
                                document.body?.scrollHeight || 0
                            )
                        })"""
                    )
                    if dimensions["width"] * dimensions["height"] > max_pixels:
                        raise BrowserServiceError("整页截图超过像素面积上限。")
                return await page.screenshot(
                    type="png",
                    full_page=full_page,
                    animations="disabled",
                    timeout=int(timeout * 1000),
                )

        try:
            return await asyncio.wait_for(render(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BrowserServiceError("HTML 截图超时。") from exc
        except BrowserServiceError:
            raise
        except PlaywrightError as exc:
            raise BrowserServiceError(
                "HTML 截图失败，请检查页面内容和浏览器日志。"
            ) from exc

    async def render_pdf(
        self,
        html: str,
        *,
        format: str = "A4",
        landscape: bool = False,
        print_background: bool = True,
        prefer_css_page_size: bool = True,
        margin: dict[str, str] | None = None,
        viewport: dict[str, int] | None = None,
        javascript_enabled: bool = False,
        timeout: int | float = 30,
    ) -> bytes:
        """Render offline HTML as PDF bytes using Chromium's print pipeline.

        Args:
            html: Complete HTML document with inline styles and assets.
            format: Chromium paper format such as A4, Letter, or Legal.
            landscape: Use landscape page orientation.
            print_background: Include CSS background colors and images.
            prefer_css_page_size: Respect CSS @page dimensions when present.
            margin: Optional CSS-unit margins keyed by top, right, bottom, left.
            viewport: CSS viewport dimensions for this task.
            javascript_enabled: Enable scripts in the isolated page if required.
            timeout: Maximum render duration in seconds, including queue wait.

        Returns:
            PDF document bytes.

        Raises:
            BrowserServiceError: Input is invalid or PDF rendering fails.
        """
        self._validate_html(html)
        timeout = self._validate_timeout(timeout)
        if not isinstance(format, str) or not format.strip():
            raise BrowserServiceError("format 必须是非空字符串。")
        if type(landscape) is not bool or type(print_background) is not bool:
            raise BrowserServiceError("landscape 和 print_background 必须是布尔值。")
        if type(prefer_css_page_size) is not bool:
            raise BrowserServiceError("prefer_css_page_size 必须是布尔值。")
        if margin is not None and (
            not isinstance(margin, dict)
            or set(margin) - {"top", "right", "bottom", "left"}
            or any(not isinstance(value, str) for value in margin.values())
        ):
            raise BrowserServiceError("margin 必须是以 CSS 单位表示的页边距字典。")

        async def render() -> bytes:
            async with self.session(
                viewport=viewport,
                javascript_enabled=javascript_enabled,
                timeout=timeout,
            ) as page:
                await page.set_content(
                    html, wait_until="load", timeout=int(timeout * 1000)
                )
                await page.evaluate("document.fonts.ready")
                options: dict[str, Any] = {
                    "format": format,
                    "landscape": landscape,
                    "print_background": print_background,
                    "prefer_css_page_size": prefer_css_page_size,
                }
                if margin is not None:
                    options["margin"] = margin
                return await page.pdf(**options)

        try:
            return await asyncio.wait_for(render(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BrowserServiceError("HTML 转 PDF 超时。") from exc
        except BrowserServiceError:
            raise
        except PlaywrightError as exc:
            raise BrowserServiceError(
                "HTML 转 PDF 失败，请检查页面内容和浏览器日志。"
            ) from exc

    async def close(self) -> None:
        """Cancel active sessions and release Chromium and the Playwright driver."""
        if self._closed or self._closing:
            return
        self._closing = True
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._active_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lifecycle_lock:
            async with self._headed_lock:
                await self._stop_runtime()
        self._closed = True
        self._closing = False
