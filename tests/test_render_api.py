"""Verify the public rendering boundary with real Chromium objects."""

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_browser.agent_browser import AgentBrowser
from astrbot_plugin_browser.render_api import (
    BrowserServiceAPI,
    RenderLocator,
    RenderPage,
)
from astrbot_plugin_browser.service import (
    BrowserService,
    BrowserServiceError,
    find_system_chromium,
)
from PIL import Image
from playwright.async_api import Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


@pytest.mark.parametrize("operation", ["render_html", "render_pdf", "session"])
async def test_disabled_render_api_rejects_requests_to_ready_browser(operation):
    service = Mock(spec=BrowserService)
    service.ready = True
    service.render_html = AsyncMock()
    service.render_pdf = AsyncMock()
    api = BrowserServiceAPI(service, enabled=False)
    # Cached entry points must reject calls even when callers omit ready checks.
    call = getattr(api, operation)
    assert api.ready is False
    with pytest.raises(BrowserServiceError, match="共享渲染服务已关闭"):
        if operation == "session":
            async with call():
                pytest.fail("Disabled rendering must not create a session")
        else:
            await call("<p>blocked</p>")
    service.render_html.assert_not_awaited()
    service.render_pdf.assert_not_awaited()
    service.session.assert_not_called()


@pytest.fixture
async def render_service():
    async with async_playwright() as playwright:
        bundled = Path(playwright.chromium.executable_path)
    executable = (
        bundled if await asyncio.to_thread(bundled.is_file) else find_system_chromium()
    )
    if executable is None:
        pytest.skip("Install Playwright Chromium to run rendering API tests")
    service = BrowserService(browser_executable=str(executable))
    try:
        await service.initialize()
        yield service
    finally:
        await service.close()


async def test_public_render_api_cannot_reach_agent_credentials(
    render_service, tmp_path
):
    service = render_service
    api = BrowserServiceAPI(service)
    agent = AgentBrowser(service, data_dir=tmp_path, tool_level="full")
    owner = ("test:private", "alice")
    try:
        await agent.execute(owner, "network", operation="start")
        context = agent._sessions[owner].context
        await context.add_cookies(
            [
                {
                    "name": "login",
                    "value": "alice-token",
                    "url": "https://example.test",
                    "httpOnly": True,
                }
            ]
        )
        private_page = await context.new_page()
        await private_page.set_content(
            '<p>alice-private-data</p><input type="password" value="alice-password">'
        )
        assert {name for name in dir(api) if not name.startswith("_")} == {
            "ready",
            "render_html",
            "render_pdf",
            "session",
        }
        for name in ("operation", "_operation", "initialize", "close", "_service"):
            with pytest.raises(AttributeError):
                getattr(api, name)

        async with api.session(javascript_enabled=True) as page:
            assert isinstance(page, RenderPage) and not isinstance(page, Page)
            await page.set_content('<main id="content">public-render-data</main>')
            locator = page.locator("#content")
            assert isinstance(locator, RenderLocator) and not isinstance(
                locator, Locator
            )
            assert len(service._browser.contexts) == 2
            assert private_page.context.browser is service._browser
            assert {name for name in dir(page) if not name.startswith("_")} == {
                "url",
                "viewport_size",
                "set_content",
                "content",
                "title",
                "set_viewport_size",
                "emulate_media",
                "wait_for_load_state",
                "evaluate",
                "locator",
                "screenshot",
                "pdf",
            }
            assert {name for name in dir(locator) if not name.startswith("_")} == {
                "page",
                "first",
                "last",
                "nth",
                "all",
                "count",
                "locator",
                "filter",
                "inner_text",
                "text_content",
                "inner_html",
                "get_attribute",
                "is_visible",
                "is_hidden",
                "wait_for",
                "scroll_into_view_if_needed",
                "evaluate",
                "evaluate_all",
                "bounding_box",
                "screenshot",
            }
            assert locator.page is page
            for name in (
                "context",
                "browser",
                "request",
                "main_frame",
                "frames",
                "frame",
                "frame_locator",
                "evaluate_handle",
                "route",
                "unroute",
                "on",
                "once",
                "wait_for_event",
                "expect_request",
                "expose_function",
                "set_input_files",
                "goto",
                "close",
            ):
                with pytest.raises(AttributeError):
                    getattr(page, name)
            for name in (
                "element_handle",
                "element_handles",
                "evaluate_handle",
                "frame_locator",
                "content_frame",
                "set_input_files",
            ):
                with pytest.raises(AttributeError):
                    getattr(locator, name)
            assert (
                await page.evaluate("document.body.textContent") == "public-render-data"
            )
            assert (
                await locator.evaluate("node => node.textContent")
                == "public-render-data"
            )
            # Returning DOM objects by value does not return a Python Page/JSHandle.
            json.dumps(await page.evaluate("() => ({node: document.body})"))

        assert service._browser.contexts == [context]
        assert (await context.cookies())[0]["value"] == "alice-token"
        assert await private_page.locator("input").input_value() == "alice-password"
    finally:
        await agent.close()


@pytest.mark.parametrize("javascript_enabled", [False, True])
async def test_documented_render_calls_return_data_and_images(
    render_service, javascript_enabled
):
    api = BrowserServiceAPI(render_service)
    async with api.session(
        viewport={"width": 900, "height": 720},
        javascript_enabled=javascript_enabled,
        timeout=45,
    ) as page:
        await page.set_content(
            "<style>body{margin:0}.markdown-body{width:640px;height:160px}</style>"
            '<main class="markdown-body">正文</main>',
            wait_until="load",
        )
        assert await page.evaluate("value => value", {"ok": [True, 1, None]}) == {
            "ok": [True, 1, None]
        }
        await page.evaluate("document.fonts.ready")
        article = page.locator(".markdown-body")
        box = await article.bounding_box()
        assert box == {"x": 0, "y": 0, "width": 640, "height": 160}
        assert await article.evaluate("(node, n) => node.scrollHeight + n", 2) == 162
        png = await article.screenshot(type="png", animations="disabled")
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        jpeg = await page.screenshot(type="jpeg", full_page=True, animations="disabled")
        assert jpeg.startswith(b"\xff\xd8\xff")
    assert not render_service._browser.contexts
    assert (await api.render_html("<p>PNG</p>")).startswith(b"\x89PNG\r\n\x1a\n")
    assert (await api.render_pdf("<p>PDF</p>")).startswith(b"%PDF-")


async def test_render_api_rejects_file_paths_and_native_object_arguments(
    render_service, tmp_path
):
    api = BrowserServiceAPI(render_service)
    image_path = tmp_path / "unexpected.png"
    async with api.session() as page:
        await page.set_content("<main>正文</main>")
        locator = page.locator("main")
        for target in (page, locator):
            with pytest.raises(TypeError, match="path"):
                await target.screenshot(path=str(image_path))
            with pytest.raises(BrowserServiceError, match="type"):
                await target.screenshot(type="unsupported")
            with pytest.raises(BrowserServiceError, match="animations"):
                await target.screenshot(animations="unsupported")
            with pytest.raises(BrowserServiceError, match="expression"):
                await target.evaluate(lambda: None)
        assert not image_path.exists()

        raw = render_service._browser.contexts[0].pages[0]
        handle = await raw.evaluate_handle("document.body")
        try:
            for target in (page, locator):
                with pytest.raises(BrowserServiceError, match="Playwright"):
                    await target.evaluate("value => value", {"handle": [handle]})
                with pytest.raises(BrowserServiceError, match="原生对象"):
                    await target.screenshot(mask=[raw.locator("main")])
            with pytest.raises(BrowserServiceError, match="原生对象"):
                page.locator("main", has=raw.locator("main"))
            with pytest.raises(BrowserServiceError, match="原生对象"):
                locator.locator(raw.locator("main"))
            with pytest.raises(TypeError, match="path"):
                await page.pdf(path=str(tmp_path / "unexpected.pdf"))
        finally:
            await handle.dispose()
        with pytest.raises(BrowserServiceError, match="html"):
            await page.set_content(123)
        with pytest.raises(BrowserServiceError, match="wait_until"):
            await page.set_content("<p>正文</p>", wait_until="unsupported")
        with pytest.raises(BrowserServiceError, match="full_page"):
            await page.screenshot(full_page="true")
        with pytest.raises(TypeError, match="timeout"):
            await page.evaluate("1", timeout=-1)
        with pytest.raises(TypeError, match="timeout"):
            await page.pdf(timeout=-1)
        with pytest.raises(BrowserServiceError, match="selector"):
            page.locator("")
        with pytest.raises(BrowserServiceError, match="evaluate 失败"):
            await page.evaluate("() => {throw new Error('test');}")


async def test_restored_render_options_and_wrapped_locator_chains(render_service):
    api = BrowserServiceAPI(render_service)
    async with api.session(device_scale_factor=2) as page:
        await page.set_viewport_size({"width": 200, "height": 160})
        assert page.viewport_size == {"width": 200, "height": 160}
        copied = page.viewport_size
        copied["width"] = 1
        assert page.viewport_size["width"] == 200
        with pytest.raises(BrowserServiceError, match="设备像素比"):
            await page.set_viewport_size({"width": 4096, "height": 4096})
        await page.set_content(
            "<title>渲染测试</title><style>body{margin:0}"
            "main{width:100px;height:60px;background:red}</style>"
            '<main data-id="card"><span>公开</span><b>遮罩</b></main>'
            '<main style="display:none">隐藏</main>',
            timeout=5_000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_load_state("load", timeout=5_000)
        assert page.url == "about:blank"
        assert await page.title() == "渲染测试"
        assert 'data-id="card"' in await page.content()
        cards = page.locator("main", has_text=re.compile("公开|隐藏"))
        assert await cards.count() == 2
        assert await cards.last.is_hidden()
        assert await cards.first.is_visible()
        assert await cards.nth(0).inner_text(timeout=5_000) == "公开遮罩"
        assert await cards.first.get_attribute("data-id", timeout=5_000) == "card"
        assert "<span>公开</span>" in await cards.first.inner_html(timeout=5_000)
        assert await cards.last.text_content(timeout=5_000) == "隐藏"
        assert await cards.evaluate_all("nodes => nodes.length") == 2
        for item in await cards.all():
            assert isinstance(item, RenderLocator) and item.page is page
        secret = page.locator("b")
        card = cards.filter(has=secret, has_not_text="隐藏", visible=True)
        assert await card.count() == 1
        assert await cards.filter(has_not=secret).count() == 1
        assert await page.locator("main", has=secret).count() == 1
        assert await card.locator(secret).count() == 1
        assert await card.locator("span", has_text="公开").inner_text() == "公开"
        await card.wait_for(state="visible", timeout=5_000)
        await card.scroll_into_view_if_needed(timeout=5_000)
        assert (await card.bounding_box(timeout=5_000))["width"] == 100
        assert await card.evaluate("el => el.tagName", timeout=5_000) == "MAIN"
        await page.emulate_media(
            media="print", color_scheme="dark", reduced_motion="reduce"
        )
        assert await page.evaluate("matchMedia('print').matches")
        assert await page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches")
        for target in (page, card):
            png = await target.screenshot(
                timeout=5_000,
                omit_background=True,
                caret="hide",
                scale="css",
                mask=[secret],
                mask_color="#00ff00",
                style="main { background: transparent !important; }",
                animations="disabled",
            )
            with Image.open(BytesIO(png)) as image:
                assert image.mode == "RGBA"
                assert image.getpixel((99, 59))[3] == 0
                assert (0, 255, 0, 255) in {
                    color for _, color in image.getcolors(image.width * image.height)
                }
                assert image.width == (200 if target is page else 100)
            jpeg = await target.screenshot(type="jpeg", quality=70, timeout=5_000)
            assert jpeg.startswith(b"\xff\xd8\xff")
        png = await page.screenshot(
            clip={"x": 0, "y": 0, "width": 40, "height": 30}, scale="css"
        )
        with Image.open(BytesIO(png)) as image:
            assert image.size == (40, 30)
        pdf = await page.pdf(
            width="100mm",
            height="80mm",
            scale=0.8,
            print_background=True,
            display_header_footer=True,
            header_template="<span>页眉</span>",
            footer_template='<span class="pageNumber"></span>',
            page_ranges="1",
            margin={"top": "5mm"},
            tagged=True,
            outline=True,
        )
        assert pdf.startswith(b"%PDF-")


async def test_locator_arguments_cannot_cross_render_sessions(render_service):
    api = BrowserServiceAPI(render_service)
    async with api.session() as page, api.session() as other_page:
        await page.set_content("<main>当前会话</main>")
        await other_page.set_content("<main>另一个会话</main>")
        local = page.locator("main")
        foreign = other_page.locator("main")
        for target in (page, local):
            with pytest.raises(BrowserServiceError, match="其他渲染会话"):
                await target.screenshot(mask=[foreign])
            with pytest.raises(BrowserServiceError, match="其他渲染会话"):
                target.locator("main", has=foreign)
        with pytest.raises(BrowserServiceError, match="其他渲染会话"):
            local.filter(has_not=foreign)
        with pytest.raises(BrowserServiceError, match="其他渲染会话"):
            local.locator(foreign)


async def test_evaluate_accepts_by_value_data_beyond_json(render_service):
    api = BrowserServiceAPI(render_service)
    async with api.session() as page:
        await page.set_content("<main>正文</main>")
        value = {"at": datetime(2026, 10, 20, tzinfo=timezone.utc), "n": float("inf")}
        cyclic = []
        cyclic.append(cyclic)
        for target, expression in (
            (page, "v => [v.at.toISOString(), v.n === Infinity]"),
            (page.locator("main"), "(el, v) => [v.at.toISOString(), v.n === Infinity]"),
        ):
            assert await target.evaluate(expression, value) == [
                "2026-10-20T00:00:00.000Z",
                True,
            ]
        assert await page.evaluate("v => v[0] === v", cyclic)
        assert math.isnan(await page.evaluate("v => v", float("nan")))
        with pytest.raises(BrowserServiceError, match="字典键"):
            await page.evaluate("v => v", {1: "value"})


async def test_per_operation_timeouts_and_invalid_values(render_service):
    api = BrowserServiceAPI(render_service)
    async with api.session(timeout=5) as page:
        await page.set_content("<main>正文</main>", timeout=0)
        missing = page.locator("#missing")
        for action in (
            missing.wait_for,
            missing.bounding_box,
            missing.evaluate,
            missing.screenshot,
            missing.inner_text,
        ):
            args = ("el => el.tagName",) if action == missing.evaluate else ()
            with pytest.raises(BrowserServiceError) as error:
                await asyncio.wait_for(action(*args, timeout=20), timeout=2)
            assert isinstance(
                error.value.__cause__, (PlaywrightTimeoutError, asyncio.TimeoutError)
            )
        for timeout in (-1, True, float("inf"), float("nan"), "1000"):
            for target in (page, page.locator("main")):
                with pytest.raises(BrowserServiceError, match="timeout"):
                    await target.screenshot(timeout=timeout)

    async with api.session(timeout=0.1, javascript_enabled=True) as page:
        await page.set_content("<body></body>")
        await page.evaluate(
            "setTimeout(() => {document.body.innerHTML = '<main>延迟元素</main>';}, 350)"
        )
        png = await asyncio.wait_for(
            page.locator("main").screenshot(timeout=0), timeout=3
        )
        assert png.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("exit_mode", ["normal", "exception", "cancel"])
async def test_page_and_locator_expire_on_every_exit(render_service, exit_mode):
    api = BrowserServiceAPI(render_service)
    references = []
    entered = asyncio.Event()

    async def use_session():
        async with api.session() as page:
            await page.set_content("<main>正文</main>")
            locator = page.locator("main")
            references.extend(
                [
                    page,
                    locator,
                    locator.first,
                    locator.last,
                    locator.nth(0),
                    locator.filter(has_text="正文"),
                    locator.locator("span"),
                    *(await locator.all()),
                ]
            )
            entered.set()
            if exit_mode == "exception":
                raise ValueError("caller error")
            if exit_mode == "cancel":
                await asyncio.Event().wait()

    task = asyncio.create_task(use_session())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if exit_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_mode == "exception":
            with pytest.raises(ValueError, match="caller error"):
                await task
        else:
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert not render_service._browser.contexts
    page, *locators = references
    for target in references:
        with pytest.raises(BrowserServiceError, match="会话已结束"):
            await target.evaluate("1")
        with pytest.raises(BrowserServiceError, match="会话已结束"):
            await target.screenshot()
    with pytest.raises(BrowserServiceError, match="会话已结束"):
        page.locator("main")
    with pytest.raises(BrowserServiceError, match="会话已结束"):
        await page.set_content("<p>new</p>")
    for locator in locators:
        with pytest.raises(BrowserServiceError, match="会话已结束"):
            await locator.bounding_box()
        with pytest.raises(BrowserServiceError, match="会话已结束"):
            _ = locator.page
        with pytest.raises(BrowserServiceError, match="会话已结束"):
            _ = locator.first


async def test_page_scripts_cannot_fetch_external_resources(render_service):
    api = BrowserServiceAPI(render_service)
    requests = []

    async def handle(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/private"
    try:
        async with api.session(javascript_enabled=True) as page:
            await page.set_content(f'<img src="{url}"><main>离线渲染</main>')
            result = await page.evaluate(
                "async url => {try {await fetch(url); return 'sent';}"
                "catch {return 'blocked';}}",
                url,
            )
            assert result == "blocked"
            assert requests == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "markdown",
    [
        "# 中文标题\n\n**共享浏览器会话**",
        "$x^2$\n\n```mermaid\nflowchart LR; A-->B\n```",
    ],
)
async def test_existing_markdown_plugin_works_without_changes(render_service, markdown):
    module = pytest.importorskip("astrbot_plugin_markdown_render.renderer")
    api = BrowserServiceAPI(render_service)
    renderer = module.MarkdownRenderer(browser_service_resolver=lambda: api)
    await renderer.initialize()
    try:
        png = await renderer.render(markdown)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert not render_service._browser.contexts
    finally:
        await renderer.close()
