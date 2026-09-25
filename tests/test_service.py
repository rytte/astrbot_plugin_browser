import asyncio
import os
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
    def __init__(self):
        self.page = FakePage()
        self.route = AsyncMock()
        self.new_page = AsyncMock(return_value=self.page)
        self.close = AsyncMock()


class FakeBrowser:
    def __init__(self):
        self.contexts_created = []
        self.close = AsyncMock()
        self._connected = True
        self.new_context = AsyncMock(side_effect=self._new_context)

    def is_connected(self):
        return self._connected

    async def _new_context(self, **kwargs):
        context = FakeContext()
        self.contexts_created.append((context, kwargs))
        return context


@pytest.fixture
def fake_runtime(monkeypatch):
    browser = FakeBrowser()
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
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
        with pytest.raises(BrowserServiceError, match="队列已满"):
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
