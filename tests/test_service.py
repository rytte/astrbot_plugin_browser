import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_browser import service as service_module
from astrbot_plugin_browser.service import BrowserService, BrowserServiceError
from playwright.async_api import Error as PlaywrightError


class FakeLocator:
    def __init__(self):
        self.bounding_box = AsyncMock(return_value={"width": 640, "height": 320})
        self.screenshot = AsyncMock(return_value=b"element-png")


class FakePage:
    def __init__(self):
        self.set_content = AsyncMock()
        self.screenshot = AsyncMock(return_value=b"page-png")
        self.pdf = AsyncMock(return_value=b"document-pdf")
        self.locator_instance = FakeLocator()
        self.locator = Mock(return_value=self.locator_instance)
        self.set_default_timeout = Mock()
        self.set_default_navigation_timeout = Mock()

    async def evaluate(self, script):
        if script == "document.fonts.ready":
            return None
        return {"width": 900, "height": 1200}


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.closed = False
        self._close_handlers = []
        self.page = FakePage()
        self.route = AsyncMock()
        self.new_page = AsyncMock(return_value=self.page)
        self.close = AsyncMock(side_effect=self._close)

    def on(self, event, handler):
        if event == "close":
            self._close_handlers.append(handler)

    async def _close(self):
        self.closed = True
        for handler in self._close_handlers:
            handler()


class FakeBrowser:
    def __init__(self):
        self.contexts_created = []
        self.close = AsyncMock(side_effect=self._close)
        self._connected = True
        self.new_context = AsyncMock(side_effect=self._new_context)

    def is_connected(self):
        return self._connected

    @property
    def contexts(self):
        return [context for context, _ in self.contexts_created if not context.closed]

    async def _close(self):
        self._connected = False
        for context in self.contexts:
            await context.close()

    async def _new_context(self, **kwargs):
        context = FakeContext(self)
        self.contexts_created.append((context, kwargs))
        return context


@pytest.fixture
def fake_runtime(monkeypatch):
    browser = FakeBrowser()
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            executable_path=sys.executable,
            launch=AsyncMock(return_value=browser),
        ),
        stop=AsyncMock(),
    )
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
    factory = Mock(return_value=manager)
    monkeypatch.setattr(service_module, "async_playwright", factory)
    return SimpleNamespace(
        browser=browser,
        playwright=playwright,
        manager=manager,
        factory=factory,
    )


@pytest.fixture
def service_clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        service_module, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    return clock


def test_find_system_chromium_uses_path(monkeypatch, tmp_path):
    executable = tmp_path / "chromium"
    executable.touch()
    monkeypatch.setattr(
        service_module.shutil,
        "which",
        lambda command: str(executable) if command == "chromium" else None,
    )

    assert service_module.find_system_chromium() == executable


async def test_auto_install_uses_current_python(monkeypatch):
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"installed", b"")),
    )
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(
        service_module.asyncio, "create_subprocess_exec", create_process
    )
    service = BrowserService()

    await service._install_playwright_chromium()

    create_process.assert_awaited_once_with(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=service_module.asyncio.subprocess.PIPE,
        stderr=service_module.asyncio.subprocess.PIPE,
    )
    process.communicate.assert_awaited_once()


async def test_auto_selects_detected_system_browser(
    fake_runtime, monkeypatch, tmp_path
):
    executable = tmp_path / "chrome"
    executable.touch()
    fake_runtime.playwright.chromium.executable_path = str(tmp_path / "missing")
    monkeypatch.setattr(service_module, "find_system_chromium", lambda: executable)
    service = BrowserService()

    await service.initialize()

    fake_runtime.playwright.chromium.launch.assert_awaited_once_with(
        headless=True,
        executable_path=str(executable),
    )
    await service.close()


async def test_missing_browsers_are_installed_automatically(fake_runtime, monkeypatch):
    fake_runtime.playwright.chromium.executable_path = "missing"
    monkeypatch.setattr(service_module, "find_system_chromium", lambda: None)
    install = AsyncMock()
    service = BrowserService()
    service._install_playwright_chromium = install

    await service.initialize()

    install.assert_awaited_once()
    fake_runtime.playwright.chromium.launch.assert_awaited_once_with(headless=True)
    await service.close()


async def test_one_browser_serves_isolated_sessions_and_closes_contexts(fake_runtime):
    service = BrowserService()
    await service.initialize()

    async with service.session(viewport={"width": 900, "height": 720}):
        pass
    async with service.session(
        viewport={"width": 1200, "height": 1800}, javascript_enabled=True
    ):
        pass

    fake_runtime.playwright.chromium.launch.assert_awaited_once_with(headless=True)
    assert len(fake_runtime.browser.contexts_created) == 2
    contexts = fake_runtime.browser.contexts_created
    assert contexts[0][0] is not contexts[1][0]
    assert contexts[0][1]["java_script_enabled"] is False
    assert contexts[1][1]["java_script_enabled"] is True
    for context, _ in contexts:
        context.route.assert_awaited_once_with("**/*", service._abort_request)
        context.close.assert_awaited_once()

    await service.close()
    fake_runtime.browser.close.assert_awaited_once()
    fake_runtime.playwright.stop.assert_awaited_once()


async def test_session_blocks_requests_and_cleans_after_cancellation(fake_runtime):
    service = BrowserService()
    await service.initialize()
    entered = asyncio.Event()

    async def hold_session():
        async with service.session():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_session())
    await entered.wait()
    context = fake_runtime.browser.contexts_created[0][0]
    route_handler = context.route.await_args.args[1]
    route = SimpleNamespace(abort=AsyncMock())
    await route_handler(route)
    route.abort.assert_awaited_once()

    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    context.close.assert_awaited_once()
    fake_runtime.browser.close.assert_awaited_once()


async def test_session_queue_has_a_bounded_wait(fake_runtime):
    service = BrowserService(max_concurrent_sessions=1)
    service.queue_timeout = 0.01
    await service.initialize()

    async with service.session():
        with pytest.raises(BrowserServiceError, match="任务排队超时"):
            async with service.session():
                pass

    await service.close()


async def test_html_screenshot_and_pdf_share_the_managed_browser(fake_runtime):
    service = BrowserService()
    await service.initialize()

    png = await service.render_html(
        "<html><body><main id='card'>hello</main></body></html>",
        selector="#card",
        viewport={"width": 900, "height": 720},
    )
    pdf = await service.render_pdf(
        "<html><body>hello</body></html>",
        format="A4",
        margin={"top": "8mm"},
    )

    assert png == b"element-png"
    assert pdf == b"document-pdf"
    fake_runtime.playwright.chromium.launch.assert_awaited_once_with(headless=True)
    first_context = fake_runtime.browser.contexts_created[0][0]
    second_context = fake_runtime.browser.contexts_created[1][0]
    first_context.page.locator_instance.screenshot.assert_awaited_once_with(
        type="png", animations="disabled", timeout=30_000
    )
    second_context.page.pdf.assert_awaited_once_with(
        format="A4",
        landscape=False,
        print_background=True,
        prefer_css_page_size=True,
        margin={"top": "8mm"},
    )
    assert all(
        context.close.await_count == 1
        for context, _ in fake_runtime.browser.contexts_created
    )

    await service.close()


async def test_page_screenshot_rejects_excessive_full_page_area(fake_runtime):
    service = BrowserService()
    await service.initialize()

    with pytest.raises(BrowserServiceError, match="像素面积上限"):
        await service.render_html("<html></html>", max_pixels=100)

    context = fake_runtime.browser.contexts_created[0][0]
    context.page.screenshot.assert_not_awaited()
    context.close.assert_awaited_once()
    await service.close()


async def test_startup_failure_is_actionable_and_stops_playwright(fake_runtime):
    fake_runtime.playwright.chromium.launch.side_effect = PlaywrightError(
        "executable missing"
    )
    service = BrowserService()

    with pytest.raises(BrowserServiceError, match="playwright install chromium"):
        await service.initialize()

    fake_runtime.playwright.stop.assert_awaited_once()
    assert not service.ready


async def test_headed_browser_is_lazy_singleton_and_keeps_headless_runtime(
    fake_runtime,
):
    headed = FakeBrowser()
    fake_runtime.playwright.chromium.launch.side_effect = [fake_runtime.browser, headed]
    service = BrowserService()
    await service.initialize()
    assert service._headed_browser is None
    instances = await asyncio.gather(
        *(service._ensure_browser("headed") for _ in range(3))
    )
    assert all(browser is headed for browser in instances)
    assert fake_runtime.playwright.chromium.launch.await_count == 2
    assert fake_runtime.playwright.chromium.launch.await_args.kwargs == {
        "headless": False
    }
    await service.close_headed(headed)
    headed.close.assert_awaited_once()
    fake_runtime.browser.close.assert_not_awaited()
    fake_runtime.playwright.stop.assert_not_awaited()
    assert service.ready and service._headed_browser is None
    assert await service.render_html("<html>still running</html>") == b"page-png"
    await service.close()
    fake_runtime.playwright.stop.assert_awaited_once()


async def test_headed_launch_failure_does_not_stop_headless(fake_runtime):
    fake_runtime.playwright.chromium.launch.side_effect = [
        fake_runtime.browser,
        PlaywrightError("no display"),
    ]
    service = BrowserService()
    await service.initialize()
    with pytest.raises(BrowserServiceError, match="有头浏览器启动失败"):
        async with service._operation("headed"):
            pass
    assert service.ready and service._headed_browser is None
    fake_runtime.playwright.stop.assert_not_awaited()
    assert await service.render_html("<html>ok</html>") == b"page-png"
    await service.close()


async def test_headed_launch_does_not_block_headless_operations(fake_runtime):
    service = BrowserService()
    await service.initialize()
    started, release = asyncio.Event(), asyncio.Event()
    headed = FakeBrowser()

    async def launch(**kwargs):
        started.set()
        await release.wait()
        return headed

    fake_runtime.playwright.chromium.launch.side_effect = launch
    task = asyncio.create_task(service._ensure_browser("headed"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert (
            await asyncio.wait_for(service.render_html("<html>ok</html>"), timeout=1)
            == b"page-png"
        )
    finally:
        release.set()
        await task
        await service.close()


async def test_headless_restart_preserves_headed_and_driver(fake_runtime):
    headed, replacement = FakeBrowser(), FakeBrowser()
    fake_runtime.playwright.chromium.launch.side_effect = [
        fake_runtime.browser,
        headed,
        replacement,
    ]
    service = BrowserService()
    await service.initialize()
    await service._ensure_browser("headed")
    fake_runtime.browser._connected = False
    assert await service._ensure_browser() is replacement
    assert service._headed_browser is headed
    headed.close.assert_not_awaited()
    fake_runtime.playwright.stop.assert_not_awaited()
    await service.close()


async def test_stale_headed_close_does_not_close_replacement(fake_runtime):
    first, second = FakeBrowser(), FakeBrowser()
    fake_runtime.playwright.chromium.launch.side_effect = [
        fake_runtime.browser,
        first,
        second,
    ]
    service = BrowserService()
    await service.initialize()
    await service._ensure_browser("headed")
    await service.close_headed(first)
    await service._ensure_browser("headed")
    await service.close_headed(first)
    assert service._headed_browser is second
    second.close.assert_not_awaited()
    await service.close()


async def test_idle_shutdown_is_disabled_by_default(fake_runtime, service_clock):
    service = BrowserService()
    await service.initialize()
    service_clock.now += 86400
    await service._close_if_idle()
    fake_runtime.browser.close.assert_not_awaited()
    assert service._idle_reaper is None
    await service.close()


async def test_idle_shutdown_preserves_readiness_and_wakes_renderers(
    fake_runtime, service_clock
):
    service = BrowserService(idle_shutdown_enabled=True)
    assert not service.ready
    await service.initialize()
    service_clock.now += 1199
    await service._close_if_idle()
    fake_runtime.browser.close.assert_not_awaited()
    service_clock.now += 1
    await service._close_if_idle()
    await service._close_if_idle()
    fake_runtime.browser.close.assert_awaited_once()
    assert service._browser is None and service.ready
    fake_runtime.playwright.stop.assert_not_awaited()

    replacement = FakeBrowser()
    fake_runtime.playwright.chromium.launch.return_value = replacement
    png, pdf = await asyncio.gather(
        service.render_html("<html>PNG</html>"),
        service.render_pdf("<html>PDF</html>"),
    )
    async with service.session() as page:
        assert page is replacement.contexts_created[-1][0].page
    assert png == b"page-png" and pdf == b"document-pdf"
    assert service._browser is replacement and service.ready
    assert fake_runtime.playwright.chromium.launch.await_count == 2
    assert len(replacement.contexts_created) == 3
    assert not replacement.contexts
    reaper = service._idle_reaper
    await service.close()
    assert reaper.done() and not service.ready
    replacement.close.assert_awaited_once()
    fake_runtime.playwright.stop.assert_awaited_once()


async def test_idle_shutdown_waits_for_active_render_and_resets_deadline(
    fake_runtime, service_clock
):
    service = BrowserService(idle_shutdown_enabled=True)
    await service.initialize()
    context = await fake_runtime.browser.new_context()
    started, release = asyncio.Event(), asyncio.Event()

    async def set_content(*args, **kwargs):
        started.set()
        await release.wait()

    context.page.set_content.side_effect = set_content
    fake_runtime.browser.new_context.return_value = context
    fake_runtime.browser.new_context.side_effect = None
    task = asyncio.create_task(service.render_html("<html>rendering</html>"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        service_clock.now += 2000
        await service._close_if_idle()
        fake_runtime.browser.close.assert_not_awaited()
        release.set()
        assert await task == b"page-png"
        service_clock.now += 1199
        await service._close_if_idle()
        fake_runtime.browser.close.assert_not_awaited()
        service_clock.now += 1
        await service._close_if_idle()
        fake_runtime.browser.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


async def test_queued_render_prevents_idle_shutdown_and_cancellation_releases_it(
    fake_runtime, service_clock
):
    fake_runtime.playwright.chromium.launch.side_effect = [
        fake_runtime.browser,
        FakeBrowser(),
    ]
    service = BrowserService(idle_shutdown_enabled=True, max_concurrent_sessions=1)
    await service.initialize()
    try:
        async with service._operation("headed"):
            task = asyncio.create_task(service.render_pdf("<html>queued</html>"))
            await asyncio.sleep(0)
            assert service._headless_operations == 1
            service_clock.now += 2000
            await service._close_if_idle()
            fake_runtime.browser.close.assert_not_awaited()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service._headless_operations == 0
            service_clock.now += 1200
            await service._close_if_idle()
            assert service._browser is None
    finally:
        await service.close()


async def test_retained_context_starts_idle_timer_only_when_closed(
    fake_runtime, service_clock
):
    service = BrowserService(idle_shutdown_enabled=True)
    await service.initialize()
    async with service._operation() as browser:
        context = await browser.new_context()
        service.track_context(context)
    service_clock.now += 5000
    await service._close_if_idle()
    fake_runtime.browser.close.assert_not_awaited()
    # No collector runs between the last busy check and this context's close.
    service_clock.now += 500
    await context.close()
    service_clock.now += 1199
    await service._close_if_idle()
    fake_runtime.browser.close.assert_not_awaited()
    service_clock.now += 1
    await service._close_if_idle()
    assert service._browser is None and service.ready
    await service.close()


async def test_idle_headless_shutdown_keeps_headed_browser_and_shared_driver(
    fake_runtime, service_clock
):
    headed = FakeBrowser()
    fake_runtime.playwright.chromium.launch.side_effect = [fake_runtime.browser, headed]
    service = BrowserService(idle_shutdown_enabled=True)
    await service.initialize()
    await service._ensure_browser("headed")
    await headed.new_context()
    service_clock.now += 1200
    await service._close_if_idle()
    assert service._browser is None and service._headed_browser is headed
    headed.close.assert_not_awaited()
    fake_runtime.playwright.stop.assert_not_awaited()
    assert service.ready
    await service.close()


async def test_new_render_during_idle_shutdown_waits_and_uses_replacement(
    fake_runtime, service_clock
):
    service = BrowserService(idle_shutdown_enabled=True)
    await service.initialize()
    started, release = asyncio.Event(), asyncio.Event()

    async def close_browser():
        started.set()
        await release.wait()
        await fake_runtime.browser._close()

    fake_runtime.browser.close.side_effect = close_browser
    replacement = FakeBrowser()
    fake_runtime.playwright.chromium.launch.return_value = replacement
    service_clock.now += 1200
    collector = asyncio.create_task(service._close_if_idle())
    task = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task = asyncio.create_task(service.render_html("<html>wake</html>"))
        await asyncio.sleep(0)
        assert not task.done() and service._headless_operations == 1
        release.set()
        await collector
        assert await task == b"page-png"
        assert service._browser is replacement
        assert not fake_runtime.browser.contexts_created
    finally:
        release.set()
        await asyncio.gather(
            collector, *([task] if task else []), return_exceptions=True
        )
        await service.close()


async def test_idle_close_failure_keeps_instance_and_retries_later(
    fake_runtime, service_clock
):
    service = BrowserService(idle_shutdown_enabled=True)
    await service.initialize()
    fake_runtime.browser.close.side_effect = PlaywrightError("close failed")
    service_clock.now += 1200
    await service._close_if_idle()
    assert service._browser is fake_runtime.browser and service.ready
    assert await service.render_html("<html>still running</html>") == b"page-png"
    fake_runtime.browser.close.side_effect = fake_runtime.browser._close
    service_clock.now += 1200
    await service._close_if_idle()
    assert service._browser is None
    await service.close()


async def test_idle_collector_runs_and_is_cancelled_at_shutdown(fake_runtime):
    service = BrowserService(idle_shutdown_enabled=True)
    service.idle_shutdown_timeout = 0.02
    closed = asyncio.Event()

    async def close_browser():
        await fake_runtime.browser._close()
        closed.set()

    fake_runtime.browser.close.side_effect = close_browser
    await service.initialize()
    reaper = service._idle_reaper
    try:
        await asyncio.wait_for(closed.wait(), timeout=2)
        # Let the collector publish its sleeping state after browser.close returns.
        await service._close_if_idle()
        assert service._browser is None and service.ready
    finally:
        await service.close()
    assert reaper.done() and service._idle_reaper is None


async def test_shutdown_during_idle_close_still_releases_browser_and_driver(
    fake_runtime,
):
    service = BrowserService(idle_shutdown_enabled=True)
    service.idle_shutdown_timeout = 0.02
    started = asyncio.Event()

    async def close_browser():
        if not started.is_set():
            started.set()
            await asyncio.Event().wait()
        await fake_runtime.browser._close()

    fake_runtime.browser.close.side_effect = close_browser
    await service.initialize()
    reaper = service._idle_reaper
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
    finally:
        await service.close()
    assert reaper.done() and not service.ready
    assert fake_runtime.browser.close.await_count == 2
    fake_runtime.playwright.stop.assert_awaited_once()


async def test_shutdown_during_initial_launch_does_not_leave_an_idle_collector(
    fake_runtime,
):
    service = BrowserService(idle_shutdown_enabled=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def launch(**kwargs):
        started.set()
        await release.wait()
        return fake_runtime.browser

    fake_runtime.playwright.chromium.launch.side_effect = launch
    initialization = asyncio.create_task(service.initialize())
    await asyncio.wait_for(started.wait(), timeout=2)
    shutdown = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert service._closing
    release.set()
    await asyncio.gather(initialization, shutdown)
    assert service._idle_reaper is None and not service.ready
    fake_runtime.browser.close.assert_awaited_once()
    fake_runtime.playwright.stop.assert_awaited_once()


async def test_failed_wake_can_be_retried_without_stopping_the_service(
    fake_runtime, service_clock
):
    service = BrowserService(idle_shutdown_enabled=True)
    await service.initialize()
    service_clock.now += 1200
    await service._close_if_idle()
    replacement = FakeBrowser()
    fake_runtime.playwright.chromium.launch.side_effect = [
        PlaywrightError("wake failed"),
        replacement,
    ]
    try:
        with pytest.raises(BrowserServiceError, match="启动失败"):
            await service.render_html("<html>failed wake</html>")
        assert service.ready and service._browser is None
        assert service._headless_operations == 0
        assert await service.render_pdf("<html>retry</html>") == b"document-pdf"
        assert service._browser is replacement
    finally:
        await service.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"idle_shutdown_enabled": 1},
        {"idle_shutdown_enabled": "true"},
        {"idle_shutdown_timeout": True},
        {"idle_shutdown_timeout": 29},
        {"idle_shutdown_timeout": 86401},
        {"idle_shutdown_timeout": "1200"},
    ],
)
def test_invalid_idle_shutdown_configuration(settings):
    with pytest.raises(BrowserServiceError, match="idle_shutdown"):
        BrowserService(**settings)


def test_invalid_viewports_and_limits_fail_before_startup():
    with pytest.raises(BrowserServiceError, match="viewport"):
        BrowserService._validate_viewport({"width": True, "height": 720})
    with pytest.raises(BrowserServiceError, match="像素"):
        BrowserService._validate_viewport({"width": 8192, "height": 8192})
    with pytest.raises(BrowserServiceError, match="max_concurrent_sessions"):
        BrowserService(max_concurrent_sessions=0)


async def test_device_scale_factor_is_included_in_viewport_limit(fake_runtime):
    service = BrowserService()
    with pytest.raises(BrowserServiceError, match="设备像素比"):
        async with service.session(
            viewport={"width": 3000, "height": 1500},
            device_scale_factor=2,
        ):
            pass
    fake_runtime.factory.assert_not_called()


@pytest.mark.skipif(
    not os.environ.get("ASTRBOT_BROWSER_EXECUTABLE"),
    reason="Set ASTRBOT_BROWSER_EXECUTABLE to run local browser smoke coverage",
)
async def test_real_browser_renders_png_and_pdf():
    executable = os.environ["ASTRBOT_BROWSER_EXECUTABLE"]
    service = BrowserService(browser_executable=executable)
    try:
        await service.initialize()
        png = await service.render_html(
            "<html><body><main id='card'>真实浏览器截图</main></body></html>",
            selector="#card",
        )
        pdf = await service.render_pdf(
            "<html><body><h1>真实浏览器 PDF</h1></body></html>"
        )
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert pdf.startswith(b"%PDF-")
        assert service.ready
    finally:
        await service.close()
