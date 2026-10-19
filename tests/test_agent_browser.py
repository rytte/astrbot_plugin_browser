"""Exercise agent operations against a real local Chromium and HTTP site."""

import asyncio
import json
import os
import struct
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from astrbot_plugin_browser import service as service_module
from astrbot_plugin_browser.agent_browser import (
    AgentBrowser,
    AgentBrowserError,
    AgentSession,
)
from astrbot_plugin_browser.service import (
    BrowserService,
    BrowserServiceError,
    find_system_chromium,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

OWNER = ("test:group:one", "alice")
OTHER = ("test:group:one", "bob")

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>浏览器测试</title>
<style>body{margin:0} input,button,select,a{display:block;margin:10px}</style></head>
<body><label>搜索商品<input id="query"></label>
<button id="search" onclick="result.textContent='搜索结果：'+query.value">搜索</button>
<select aria-label="排序方式" onchange="result.textContent='排序：'+this.value">
<option value="default">综合</option><option value="price">价格</option></select>
<p id="result">尚未搜索</p>
<a href="/detail" target="_blank">查看详情</a><div id="shadow"></div>
<iframe src="/frame" title="子页面"></iframe><div style="height:2000px">长页面</div>
<script>
query.addEventListener('keydown', event => {
  if(event.key === 'Enter') result.textContent='回车：'+query.value;
});
shadow.attachShadow({mode:'open'}).innerHTML='<button>Shadow 按钮</button>';
</script></body></html>"""

MOUSE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>鼠标测试</title>
<style>
body{margin:0;padding:16px} button{margin:8px;width:140px;height:32px}
#menu{position:relative;width:180px;height:40px;background:#ddd}
#submenu{display:none;position:absolute;top:28px;left:0}
#menu:hover #submenu{display:block}
#source,#drop{display:inline-block;width:140px;height:70px;background:#ddd;vertical-align:top}
#slider{display:block;width:300px;margin:20px 0} canvas{display:block;background:#eee}
</style></head><body>
<div id="menu" role="button" aria-label="更多操作">菜单
<button id="submenu" onclick="statusText.textContent='菜单已点击'">隐藏菜单项</button></div>
<button id="double" ondblclick="statusText.textContent='双击完成'">双击目标</button>
<button id="context" oncontextmenu="event.preventDefault();statusText.textContent='右键完成'">右键目标</button>
<div id="source" draggable="true" ondragstart="event.dataTransfer.setData('text/plain','项目')">拖动项目</div>
<div id="drop" ondragover="event.preventDefault()"
 ondrop="event.preventDefault();statusText.textContent='拖放完成：'+event.dataTransfer.getData('text/plain')">拖放区域</div>
<input id="slider" type="range" aria-label="音量" min="0" max="100" value="0"
 oninput="statusText.textContent='音量：'+this.value">
<canvas id="canvas" width="250" height="80"
 onmousedown="window.drawing=true" onmousemove="if(window.drawing&&event.buttons)statusText.textContent='正在绘制'"
 onmouseup="window.drawing=false;statusText.textContent='画布拖动完成'"></canvas>
<p id="statusText">等待鼠标操作</p>
<script>
window.mouseDowns=0;window.mouseButtons=0;
document.addEventListener('mousedown',event=>{window.mouseDowns++;window.mouseButtons=event.buttons});
document.addEventListener('mousemove',event=>{window.mouseButtons=event.buttons});
document.addEventListener('mouseup',event=>{window.mouseButtons=event.buttons});
</script></body></html>"""

FEATURES_HTML = """<!doctype html><meta charset="utf-8"><title>基础能力测试</title>
<input id="upload" type="file" multiple style="display:none" aria-label="上传文件"
 onchange="result.textContent=Array.from(this.files).map(f=>f.name+':'+f.size).join(',')">
<p id="result">未操作</p>
<a href="/download" download>下载报告</a>
<button onclick="result.textContent=confirm('确认操作')?'已确认':'已取消'">确认弹窗</button>
<button onclick="result.textContent=String(prompt('输入昵称','默认'))">输入弹窗</button>
<button onclick="alert('通知');result.textContent='通知结束'">通知弹窗</button>
<table id="products"><caption>商品表</caption><tr><th>名称</th><th>价格</th></tr>
<tr><td>键盘</td><td>129</td></tr><tr><td>鼠标</td><td>59</td></tr></table>
<ul><li>第一项</li><li>第二项</li></ul><a href="/detail">详情</a>
<a href="/hidden" style="display:none">隐藏链接</a>
<div id="host"></div><iframe src="/frame"></iframe>
<script>host.attachShadow({mode:'open'}).innerHTML='<a href="/shadow">影子链接</a>';</script>
"""

ADVANCED_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>高级能力测试</title>
<button onclick="fetch('/api/data').then(r=>r.json()).then(data=>result.textContent=data.source).catch(()=>result.textContent='请求失败')">请求数据</button>
<p id="result">未请求</p><iframe src="/frame"></iframe>
"""


@pytest.fixture
async def site():
    requests = []
    connections = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            path = urlsplit(header.split(b" ")[1].decode()).path
            requests.append(path)
            content_type = "text/html; charset=utf-8"
            if path == "/detail":
                body = "<html><title>商品详情</title><body>价格 129 元</body></html>"
            elif path == "/frame":
                body = "<html><body><button>子页面按钮</button></body></html>"
            elif path == "/mouse":
                body = MOUSE_HTML
            elif path == "/features":
                body = FEATURES_HTML
            elif path == "/advanced":
                body = ADVANCED_HTML
            elif path.startswith("/api/"):
                request_headers = {}
                for line in header.decode("iso-8859-1").split("\r\n")[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        request_headers[key.lower()] = value.strip()
                size = int(request_headers.get("content-length", "0"))
                posted = (await reader.readexactly(size)).decode() if size else ""
                body = json.dumps(
                    {
                        "source": "real",
                        "method": header.split(b" ")[0].decode(),
                        "custom_header": request_headers.get("x-browser-test", ""),
                        "body": posted,
                    }
                )
                content_type = "application/json"
            elif path == "/download":
                writer.write(
                    b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename="report.txt"\r\nContent-Length: 12\r\nConnection: close\r\n\r\nhello agent!'
                )
                await writer.drain()
                return
            else:
                body = HTML
            payload = body.encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n".encode()
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            connections.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        server.close()
        # Python 3.12+ waits for accepted sockets too, including idle browser sockets.
        for task in list(connections):
            task.cancel()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)
        await server.wait_closed()


@pytest.fixture
async def browser_agent(tmp_path, request):
    async with async_playwright() as playwright:
        bundled = Path(playwright.chromium.executable_path)
    executable = bundled if bundled.is_file() else find_system_chromium()
    if executable is None:
        pytest.skip("Install Playwright Chromium to run browser integration tests")
    service = BrowserService(browser_executable=str(executable))
    agent = AgentBrowser(
        service, data_dir=tmp_path, tool_level=getattr(request, "param", "full")
    )
    await service.initialize()
    agent.start()
    try:
        yield agent
    finally:
        await agent.close()
        await service.close()


@pytest.fixture
async def mouse_page(browser_agent, site):
    await browser_agent.execute(OWNER, "open", url=site[0] + "/mouse")
    return browser_agent._sessions[OWNER].tabs["tab_1"]


async def test_idle_shutdown_protects_agent_context_and_restores_login_after_render_wake(
    browser_agent, site, monkeypatch
):
    agent = browser_agent
    service = agent.service
    service.idle_shutdown_enabled = True
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        service_module, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    await agent.execute(OWNER, "open", url=site[0])
    original_browser = service._browser
    context = agent._sessions[OWNER].context
    await context.add_cookies([{"name": "login", "value": "saved", "url": site[0]}])
    export = AsyncMock(wraps=context.storage_state)
    monkeypatch.setattr(context, "storage_state", export)

    clock.now += 5000
    await service._close_if_idle()
    assert service._browser is original_browser
    png = await service.render_html("<html><body>共享渲染</body></html>")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    export.assert_not_awaited()
    assert service._browser is original_browser

    await agent.execute(OWNER, "close")
    export.assert_awaited_once()
    clock.now += 1199
    await service._close_if_idle()
    assert service._browser is original_browser
    clock.now += 1
    await service._close_if_idle()
    assert service._browser is None and service.ready
    assert not original_browser.is_connected()

    pdf = await service.render_pdf("<html><body>自动唤醒</body></html>")
    assert pdf.startswith(b"%PDF-")
    assert service._browser is not original_browser
    await agent.execute(OWNER, "open", url=site[0])
    cookies = await agent._sessions[OWNER].context.cookies()
    assert any(
        cookie["name"] == "login" and cookie["value"] == "saved" for cookie in cookies
    )


def element(result, name):
    return next(
        item["ref"]
        for frame in result.data["frames"]
        for item in frame["elements"]
        if item["name"] == name
    )


def text(result):
    return "\n".join(frame["text"] for frame in result.data["frames"])


@pytest.mark.parametrize("browser_agent", ["basic", "standard", "full"], indirect=True)
async def test_search_fill_press_select_and_observation(browser_agent, site):
    agent = browser_agent
    result = await agent.execute(OWNER, "open", url=site[0])
    assert result.data["title"] == "浏览器测试"
    result = await agent.execute(
        OWNER,
        "fill",
        observation_id=result.data["observation_id"],
        ref=element(result, "搜索商品"),
        text="无线键盘",
    )
    result = await agent.execute(
        OWNER, "press", observation_id=result.data["observation_id"], key="Enter"
    )
    assert "回车：无线键盘" in text(result)
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "搜索"),
    )
    assert "搜索结果：无线键盘" in text(result)
    result = await agent.execute(
        OWNER,
        "select",
        observation_id=result.data["observation_id"],
        ref=element(result, "排序方式"),
        value="price",
    )
    assert "排序：price" in text(result)
    assert "价格" in str(result.data)


@pytest.mark.parametrize("coordinates", [False, True])
async def test_hover_reveals_menu(browser_agent, mouse_page, coordinates):
    agent, page = browser_agent, mouse_page
    result = await agent.execute(OWNER, "screenshot" if coordinates else "observe")
    if coordinates:
        box = await page.locator("#menu").bounding_box()
        target = {"x": box["x"] + 10, "y": box["y"] + 10}
    else:
        target = {"ref": element(result, "更多操作")}
    result = await agent.execute(
        OWNER, "hover", observation_id=result.data["observation_id"], **target
    )
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "隐藏菜单项"),
    )
    assert "菜单已点击" in text(result)


@pytest.mark.parametrize("coordinates", [False, True])
@pytest.mark.parametrize(
    ("selector", "name", "options", "expected"),
    [
        ("#double", "双击目标", {"click_count": 2}, "双击完成"),
        ("#context", "右键目标", {"button": "right"}, "右键完成"),
    ],
)
async def test_double_and_right_click(
    browser_agent, mouse_page, coordinates, selector, name, options, expected
):
    agent, page = browser_agent, mouse_page
    result = await agent.execute(OWNER, "screenshot" if coordinates else "observe")
    if coordinates:
        box = await page.locator(selector).bounding_box()
        target = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
    else:
        target = {"ref": element(result, name)}
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        **target,
        **options,
    )
    assert expected in text(result)


async def test_drag_elements_triggers_native_html_drop(browser_agent, mouse_page):
    agent = browser_agent
    result = await agent.execute(OWNER, "observe")
    source = next(
        item
        for item in result.data["frames"][0]["elements"]
        if item["name"] == "拖动项目"
    )
    assert source["draggable"] is True
    old_id = result.data["observation_id"]
    result = await agent.execute(
        OWNER,
        "drag",
        observation_id=old_id,
        source_ref=source["ref"],
        target_ref=element(result, "拖放区域"),
    )
    assert "拖放完成：项目" in text(result)
    assert result.data["observation_id"] != old_id


@pytest.mark.parametrize("selector", ["#slider", "#canvas"])
async def test_coordinate_drag_operates_slider_and_canvas(
    browser_agent, mouse_page, selector
):
    agent, page = browser_agent, mouse_page
    shot = await agent.execute(OWNER, "screenshot")
    box = await page.locator(selector).bounding_box()
    result = await agent.execute(
        OWNER,
        "drag",
        observation_id=shot.data["observation_id"],
        start_x=box["x"] + 8,
        start_y=box["y"] + box["height"] / 2,
        end_x=box["x"] + box["width"] - 10,
        end_y=box["y"] + box["height"] / 2,
    )
    if selector == "#slider":
        assert int(await page.locator("#slider").input_value()) > 80
        assert "音量：" in text(result)
    else:
        assert "画布拖动完成" in text(result)
    assert await page.evaluate("window.mouseButtons") == 0


@pytest.mark.parametrize("action", ["hover", "drag"])
async def test_new_mouse_actions_require_current_screenshot(
    browser_agent, mouse_page, action
):
    agent, page = browser_agent, mouse_page
    arguments = (
        {"x": 20, "y": 20}
        if action == "hover"
        else {"start_x": 20, "start_y": 20, "end_x": 40, "end_y": 40}
    )
    result = await agent.execute(OWNER, "observe")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER, action, observation_id=result.data["observation_id"], **arguments
        )
    assert error.value.code == "SCREENSHOT_REQUIRED"
    shot = await agent.execute(OWNER, "screenshot")
    await page.evaluate("statusText.textContent='网页已更新'")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER, action, observation_id=shot.data["observation_id"], **arguments
        )
    assert error.value.code == "STALE_SCREENSHOT"
    assert await page.evaluate("window.mouseDowns") == 0


async def test_hidden_file_upload_clear_and_owner_isolation(
    browser_agent, site, tmp_path
):
    agent = browser_agent
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    await agent.import_attachment(OWNER, source, "问候.txt", "attachment-1")
    listing = await agent.execute(OWNER, "upload", operation="list")
    file_id = listing.data["files"][0]["file_id"]
    assert (await agent.execute(OTHER, "upload", operation="list")).data["files"] == []
    other_origin = ("test:group:two", OWNER[1])
    assert (await agent.execute(other_origin, "download")).data["files"] == []
    for stranger in (OTHER, other_origin):
        with pytest.raises(AgentBrowserError) as error:
            await agent.execute(stranger, "download", operation="send", file_id=file_id)
        assert error.value.code == "FILE_NOT_FOUND"
    result = await agent.execute(OWNER, "open", url=site[0] + "/features")
    result = await agent.execute(
        OWNER,
        "upload",
        operation="set",
        observation_id=result.data["observation_id"],
        ref=element(result, "上传文件"),
        file_ids=[file_id],
    )
    assert "问候.txt:5" in text(result)
    page = agent._sessions[OWNER].tabs["tab_1"]
    assert (
        await page.locator("#upload").evaluate("async el => await el.files[0].text()")
        == "hello"
    )
    result = await agent.execute(
        OWNER,
        "upload",
        operation="clear",
        observation_id=result.data["observation_id"],
        ref=element(result, "上传文件"),
    )
    assert await page.locator("#upload").evaluate("el => el.files.length") == 0
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OWNER, "download", operation="send", file_id="../../secret")
    assert error.value.code == "FILE_NOT_FOUND"


async def test_browser_download_can_be_reused_and_survives_session_close(
    browser_agent, site
):
    agent = browser_agent
    result = await agent.execute(OWNER, "open", url=site[0] + "/features")
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "下载报告"),
    )
    listing = await agent.execute(OWNER, "download", operation="list")
    assert len(listing.data["files"]) == 1
    file_id = listing.data["files"][0]["file_id"]
    ready = await agent.execute(OWNER, "download", operation="wait", file_id=file_id)
    assert ready.data["file"]["status"] == "ready"
    assert ready.data["file"]["name"] == "report.txt"
    result = await agent.execute(
        OWNER,
        "upload",
        operation="set",
        observation_id=result.data["observation_id"],
        ref=element(result, "上传文件"),
        file_ids=[file_id],
    )
    assert "report.txt:12" in text(result)
    await agent.execute(OWNER, "close")
    sent = await agent.execute(OWNER, "download", operation="send", file_id=file_id)
    assert sent.file.read_bytes() == b"hello agent!"
    await agent.execute(OWNER, "download", operation="delete", file_id=file_id)
    assert not sent.file.exists()
    assert (await agent.execute(OWNER, "download")).data["files"] == []


async def test_dialog_rule_is_one_shot_and_prompt_input_works(browser_agent, site):
    agent = browser_agent
    result = await agent.execute(OWNER, "open", url=site[0] + "/features")
    await agent.execute(
        OWNER, "dialog", operation="accept", dialog_type="confirm", message="确认操作"
    )
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "确认弹窗"),
    )
    assert "已确认" in text(result)
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "确认弹窗"),
    )
    assert "已取消" in text(result)
    await agent.execute(
        OWNER,
        "dialog",
        operation="accept",
        dialog_type="prompt",
        message="输入昵称",
        prompt_text="小明",
    )
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "输入弹窗"),
    )
    assert "小明" in text(result)
    history = await agent.execute(OWNER, "dialog")
    assert history.data["next_dialog"] is None
    assert [item["matched_rule"] for item in history.data["history"]] == [
        True,
        False,
        True,
    ]
    assert all(item["status"] == "handled" for item in history.data["history"])
    await agent.execute(
        OWNER, "dialog", operation="accept", dialog_type="confirm", message="确认操作"
    )
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "通知弹窗"),
    )
    assert "通知结束" in text(result)
    assert (await agent.execute(OWNER, "dialog")).data["next_dialog"] is not None
    await agent.execute(OWNER, "dialog", operation="clear")
    assert (await agent.execute(OWNER, "dialog")).data["next_dialog"] is None


async def test_extract_tables_links_lists_text_shadow_and_iframe(browser_agent, site):
    agent = browser_agent
    opened = await agent.execute(OWNER, "open", url=site[0] + "/features")
    tables = await agent.execute(OWNER, "extract", kind="tables", selector="#products")
    assert tables.data["items"] == [
        {
            "caption": "商品表",
            "rows": [["名称", "价格"], ["键盘", "129"], ["鼠标", "59"]],
        }
    ]
    limited = await agent.execute(OWNER, "extract", kind="tables", limit=1)
    assert len(limited.data["items"][0]["rows"]) == 1
    assert limited.data["truncated"]
    links = await agent.execute(OWNER, "extract", kind="links")
    assert {item["text"] for item in links.data["items"]} == {
        "下载报告",
        "详情",
        "影子链接",
    }
    assert (
        next(item for item in links.data["items"] if item["text"] == "影子链接")["url"]
        == site[0] + "/shadow"
    )
    exact = await agent.execute(OWNER, "extract", kind="links", limit=3)
    assert not exact.data["truncated"]
    shadow_text = await agent.execute(OWNER, "extract", kind="text")
    assert "影子链接" in " ".join(shadow_text.data["items"])
    lists = await agent.execute(OWNER, "extract", kind="lists")
    assert lists.data["items"] == [{"ordered": False, "items": ["第一项", "第二项"]}]
    extracted = await agent.execute(OWNER, "extract", kind="text", selector="#result")
    assert extracted.data["items"] == ["未操作"]
    assert agent._sessions[OWNER].observation_id == opened.data["observation_id"]
    await agent.execute(
        OWNER, "wait", condition="selector", value="button", frame_index=1
    )
    iframe = await agent.execute(OWNER, "extract", kind="text", frame_index=1)
    assert iframe.data["items"] == ["子页面按钮"]
    for args, code in [
        ({"selector": "["}, "INVALID_SELECTOR"),
        ({"frame_index": -1}, "INVALID_FRAME"),
        ({"frame_index": 99}, "INVALID_FRAME"),
    ]:
        with pytest.raises(AgentBrowserError) as error:
            await agent.execute(OWNER, "extract", **args)
        assert error.value.code == code

    page = agent._sessions[OWNER].tabs["tab_1"]
    await page.locator("#result").evaluate("el => el.textContent='x'.repeat(25000)")
    capped = await agent.execute(OWNER, "extract", kind="text", selector="#result")
    assert len(capped.data["items"][0]) == 24000
    assert capped.data["truncated"]


async def test_precise_wait_hidden_detached_url_load_and_frame(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0] + "/features")
    page = agent._sessions[OWNER].tabs["tab_1"]
    await agent.execute(
        OWNER, "wait", condition="selector", value="#upload", state="attached"
    )
    await agent.execute(
        OWNER, "wait", condition="selector", value="#upload", state="hidden"
    )
    await page.evaluate("setTimeout(() => result.style.display='none', 30)")
    await agent.execute(OWNER, "wait", condition="text", value="未操作", state="hidden")
    await page.evaluate("setTimeout(() => result.remove(), 30)")
    await agent.execute(
        OWNER, "wait", condition="selector", value="#result", state="detached"
    )
    await agent.execute(
        OWNER, "wait", condition="selector", value="button", frame_index=1
    )
    await page.evaluate("setTimeout(() => location.href='/detail', 30)")
    result = await agent.execute(
        OWNER, "wait", condition="url", value=site[0] + "/det*"
    )
    assert "价格 129 元" in text(result)
    await agent.execute(OWNER, "wait", condition="load", value="load")


async def test_explicit_login_state_save_and_restore_preserves_storage(
    browser_agent, site
):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    session = agent._sessions[OWNER]
    await session.context.add_cookies(
        [{"name": "login", "value": "alice-secret", "url": site[0]}]
    )
    page = session.tabs["tab_1"]
    await page.evaluate(
        "localStorage.setItem('account','alice');sessionStorage.setItem('session-only','ephemeral')"
    )
    await page.evaluate("""() => new Promise((resolve,reject) => {
      const request=indexedDB.open('auth',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('tokens');
      request.onerror=()=>reject(request.error);
      request.onsuccess=()=>{const db=request.result;const tx=db.transaction('tokens','readwrite');
        tx.objectStore('tokens').put('indexed-secret','token');tx.oncomplete=()=>{db.close();resolve()};};
    })""")
    assert not (await agent.execute(OWNER, "state")).data["saved"]
    assert not list(agent.storage.root().rglob("*.json"))
    saved = await agent.execute(OWNER, "state", operation="save")
    assert saved.data == {"success": True, "saved": True, "mode": "headless"}
    assert not (await agent.execute(OTHER, "state")).data["saved"]
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OTHER, "state", operation="restore")
    assert error.value.code == "STATE_NOT_FOUND"
    await agent.execute(OWNER, "close")
    await agent.close()
    replacement = AgentBrowser(
        agent.service, data_dir=agent.storage.data_dir, tool_level="standard"
    )
    try:
        await replacement.execute(OWNER, "state", operation="restore")
        await replacement.execute(OWNER, "open", url=site[0])
        fresh = replacement._sessions[OWNER]
        assert (await fresh.context.cookies())[0]["value"] == "alice-secret"
        page = fresh.tabs["tab_1"]
        assert await page.evaluate("localStorage.getItem('account')") == "alice"
        assert await page.evaluate("sessionStorage.getItem('session-only')") is None
        assert (
            await page.evaluate("""() => new Promise(resolve=>{const r=indexedDB.open('auth');
          r.onsuccess=()=>{const db=r.result;const get=db.transaction('tokens').objectStore('tokens').get('token');
          get.onsuccess=()=>{resolve(get.result);db.close()}};})""")
            == "indexed-secret"
        )
        await replacement.execute(OWNER, "state", operation="clear")
        assert not (await replacement.execute(OWNER, "state")).data["saved"]
        assert (await fresh.context.cookies())[0]["value"] == "alice-secret"
    finally:
        await replacement.close()


@pytest.mark.parametrize("browser_agent", ["basic"], indirect=True)
@pytest.mark.parametrize("ending", ["close", "last_tab", "idle", "expired", "shutdown"])
async def test_session_end_saves_once_and_new_session_restores_login(
    browser_agent, site, monkeypatch, ending
):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    session = agent._sessions[OWNER]
    await session.context.add_cookies(
        [{"name": "login", "value": "alice-secret", "url": site[0]}]
    )
    page = session.tabs["tab_1"]
    await page.evaluate(
        "localStorage.setItem('account','alice');sessionStorage.setItem('temporary','only-this-tab')"
    )
    await page.evaluate("""() => new Promise((resolve,reject) => {
      const request=indexedDB.open('auth',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('tokens');
      request.onerror=()=>reject(request.error);
      request.onsuccess=()=>{const db=request.result;const tx=db.transaction('tokens','readwrite');
        tx.objectStore('tokens').put('indexed-secret','token');tx.oncomplete=()=>{db.close();resolve()};};
    })""")
    export = AsyncMock(wraps=session.context.storage_state)
    monkeypatch.setattr(session.context, "storage_state", export)
    await agent.execute(OWNER, "observe")
    await agent.execute(OWNER, "open", url=site[0])
    assert export.await_count == 0
    assert not agent.storage.state_path(OWNER).exists()

    if ending == "close":
        assert (await agent.execute(OWNER, "close")).data["state_saved"] is True
    elif ending == "last_tab":
        result = await agent.execute(OWNER, "tabs", operation="close", tab_id="tab_1")
        assert result.data["state_saved"] is True
    elif ending == "idle":
        session.last_used -= 1000
        await agent.reap_idle()
    elif ending == "expired":
        session.last_used -= 1000
        with pytest.raises(AgentBrowserError, match="空闲过期"):
            await agent.execute(OWNER, "observe")
    else:
        await agent.close()

    assert OWNER not in agent._sessions
    assert page.is_closed()
    assert agent.storage.read_state(OWNER)["cookies"][0]["value"] == "alice-secret"
    await agent.close()
    export.assert_awaited_once_with(indexed_db=True)

    replacement = AgentBrowser(agent.service, data_dir=agent.storage.data_dir)
    try:
        await replacement.execute(OTHER, "open", url=site[0])
        other = replacement._sessions[OTHER]
        assert await other.context.cookies() == []
        assert (
            await other.tabs["tab_1"].evaluate("localStorage.getItem('account')")
            is None
        )
        # Opening is enough to restore, including at the basic tool level.
        await replacement.execute(OWNER, "open", url=site[0])
        fresh = replacement._sessions[OWNER]
        assert (await fresh.context.cookies())[0]["value"] == "alice-secret"
        restored = fresh.tabs["tab_1"]
        assert await restored.evaluate("localStorage.getItem('account')") == "alice"
        assert await restored.evaluate("sessionStorage.getItem('temporary')") is None
        assert (
            await restored.evaluate("""() => new Promise(resolve=>{
          const r=indexedDB.open('auth');r.onsuccess=()=>{const db=r.result;
            const get=db.transaction('tokens').objectStore('tokens').get('token');
            get.onsuccess=()=>{resolve(get.result);db.close()};};
        })""")
            == "indexed-secret"
        )
    finally:
        await replacement.close()


async def test_clear_survives_environment_rebuild_and_close(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    await agent._sessions[OWNER].context.add_cookies(
        [{"name": "login", "value": "alice", "url": site[0]}]
    )
    await agent.execute(OWNER, "state", operation="save")
    await agent.execute(OWNER, "state", operation="clear")
    await agent.execute(OWNER, "emulate", operation="set", settings={"locale": "zh-CN"})
    await agent.execute(OWNER, "open", url=site[0])
    assert (await agent._sessions[OWNER].context.cookies())[0]["value"] == "alice"
    assert (await agent.execute(OWNER, "close")).data["state_saved"] is None
    assert not agent.storage.state_path(OWNER).exists()

    await agent.execute(OWNER, "open", url=site[0])
    assert await agent._sessions[OWNER].context.cookies() == []
    assert (await agent.execute(OWNER, "close")).data["state_saved"] is True


async def test_explicit_save_reenables_auto_save_after_clear(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    await agent.execute(OWNER, "state", operation="clear")
    await agent.execute(OWNER, "state", operation="save")
    await agent._sessions[OWNER].context.add_cookies(
        [{"name": "login", "value": "latest", "url": site[0]}]
    )
    assert (await agent.execute(OWNER, "close")).data["state_saved"] is True
    assert agent.storage.read_state(OWNER)["cookies"][0]["value"] == "latest"


async def test_starting_with_environment_settings_also_restores_login(
    browser_agent, site
):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    await agent._sessions[OWNER].context.add_cookies(
        [{"name": "login", "value": "alice", "url": site[0]}]
    )
    await agent.execute(OWNER, "close")
    snapshot = agent.storage.state_path(OWNER).read_bytes()
    await agent.execute(OWNER, "emulate", operation="set", settings={"locale": "zh-CN"})
    assert (await agent._sessions[OWNER].context.cookies())[0]["value"] == "alice"
    assert agent._sessions[OWNER].environment["locale"] == "zh-CN"
    assert agent.storage.state_path(OWNER).read_bytes() == snapshot


@pytest.mark.parametrize("snapshot", ["{broken", '{"cookies":[{}],"origins":[]}'])
async def test_bad_auto_restore_starts_clean_and_reports_notice(
    browser_agent, site, snapshot
):
    agent = browser_agent
    agent.storage.state_path(OWNER).write_text(snapshot, encoding="utf-8")
    result = await agent.execute(OWNER, "open", url=site[0])
    assert result.data["success"]
    assert any("登录状态无法" in notice for notice in result.data["notices"])
    assert await agent._sessions[OWNER].context.cookies() == []
    assert agent.storage.state_path(OWNER).read_text(encoding="utf-8") == snapshot


async def test_auto_save_failure_closes_session_and_keeps_previous_snapshot(
    browser_agent, site, monkeypatch
):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    await agent.execute(OWNER, "state", operation="save")
    previous = agent.storage.state_path(OWNER).read_bytes()
    page = agent._sessions[OWNER].tabs["tab_1"]

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(agent.storage, "write_state", fail_write)
    result = await agent.execute(OWNER, "close")
    assert result.data["success"] and result.data["state_saved"] is False
    assert OWNER not in agent._sessions
    assert page.is_closed()
    assert agent.storage.state_path(OWNER).read_bytes() == previous


async def test_restore_replaces_tabs_invalidates_refs_and_preserves_on_corrupt_state(
    browser_agent, site
):
    agent = browser_agent
    old = await agent.execute(OWNER, "open", url=site[0])
    await agent._sessions[OWNER].context.add_cookies(
        [{"name": "login", "value": "saved", "url": site[0]}]
    )
    await agent.execute(OWNER, "state", operation="save")
    snapshot = agent.storage.state_path(OWNER).read_bytes()
    previous = agent._sessions[OWNER]
    await previous.context.add_cookies(
        [{"name": "login", "value": "unsaved", "url": site[0]}]
    )
    await agent.execute(OWNER, "state", operation="restore")
    assert agent.storage.state_path(OWNER).read_bytes() == snapshot
    assert (await agent._sessions[OWNER].context.cookies())[0]["value"] == "saved"
    assert all(page.is_closed() for page in previous.context.pages)
    assert agent._sessions[OWNER] is not previous
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER, "click", observation_id=old.data["observation_id"], ref="e1"
        )
    assert error.value.code == "STALE_OBSERVATION"
    current = agent._sessions[OWNER]
    agent.storage.state_path(OWNER).write_text("{}", encoding="utf-8")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OWNER, "state", operation="restore")
    assert error.value.code == "INVALID_STATE"
    assert agent._sessions[OWNER] is current
    assert not current.tabs["tab_1"].is_closed()


async def test_drag_rejects_invalid_endpoint_before_mouse_down(
    browser_agent, mouse_page
):
    agent, page = browser_agent, mouse_page
    shot = await agent.execute(OWNER, "screenshot")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER,
            "drag",
            observation_id=shot.data["observation_id"],
            start_x=20,
            start_y=20,
            end_x=1280,
            end_y=40,
        )
    assert error.value.code == "INVALID_COORDINATES"
    result = await agent.execute(OWNER, "observe")
    await page.evaluate("drop.outerHTML='<div id=drop ondrop=\"\">拖放区域</div>'")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER,
            "drag",
            observation_id=result.data["observation_id"],
            source_ref=element(result, "拖动项目"),
            target_ref=element(result, "拖放区域"),
        )
    assert error.value.code == "STALE_ELEMENT"
    assert await page.evaluate("window.mouseDowns") == 0


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        ("click", {"button": "invalid"}),
        ("click", {"click_count": 3}),
        ("hover", {"ref": "e1", "x": 20, "y": 20}),
        ("drag", {"source_ref": "e1"}),
        ("drag", {"source_ref": "e1", "target_ref": "e2", "start_x": 20}),
    ],
)
async def test_invalid_mouse_modes_do_not_press_button(
    browser_agent, mouse_page, action, arguments
):
    result = await browser_agent.execute(OWNER, "observe")
    with pytest.raises(AgentBrowserError) as error:
        await browser_agent.execute(
            OWNER, action, observation_id=result.data["observation_id"], **arguments
        )
    assert error.value.code == "INVALID_ARGUMENT"
    assert await mouse_page.evaluate("window.mouseDowns") == 0


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("phase", ["move", "release"])
async def test_drag_releases_mouse_on_timeout_and_cancellation(
    browser_agent, mouse_page, monkeypatch, cancel, phase
):
    agent, page = browser_agent, mouse_page
    shot = await agent.execute(OWNER, "screenshot")
    box = await page.locator("#canvas").bounding_box()
    entered = asyncio.Event()
    original_move = page.mouse.move
    original_up = page.mouse.up

    async def block_drag_move(x, y, **kwargs):
        if phase == "move" and kwargs.get("steps"):
            entered.set()
            await asyncio.Event().wait()
        await original_move(x, y, **kwargs)

    async def block_first_release():
        if (
            phase == "release"
            and not entered.is_set()
            and not asyncio.current_task().cancelling()
        ):
            entered.set()
            await asyncio.Event().wait()
        await original_up()

    monkeypatch.setattr(page.mouse, "move", block_drag_move)
    monkeypatch.setattr(page.mouse, "up", block_first_release)
    if not cancel:
        agent.action_timeout = 2 if phase == "release" else 0.3
    task = asyncio.create_task(
        agent.execute(
            OWNER,
            "drag",
            observation_id=shot.data["observation_id"],
            start_x=box["x"] + 10,
            start_y=box["y"] + 20,
            end_x=box["x"] + 100,
            end_y=box["y"] + 20,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    else:
        with pytest.raises(AgentBrowserError) as error:
            await asyncio.wait_for(task, timeout=5)
        assert error.value.code == "TIMEOUT"
    agent.action_timeout = 30
    assert await page.evaluate("window.mouseDowns") == 1
    assert await page.evaluate("window.mouseButtons") == 0
    assert not agent._sessions[OWNER].lock.locked()
    assert agent.service._semaphore._value == agent.service.max_concurrent_sessions
    assert (await agent.execute(OWNER, "observe")).data["success"]


async def test_mouse_hover_resolves_iframe_and_shadow_targets(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    await agent.execute(OWNER, "wait", condition="text", value="尚未搜索")
    page = agent._sessions[OWNER].tabs["tab_1"]
    await page.evaluate("""() => {
        shadow.shadowRoot.querySelector('button').addEventListener('mouseenter', () => {
            result.textContent = 'Shadow 悬停成功';
        });
    }""")
    frame = next(frame for frame in page.frames if frame.url.endswith("/frame"))
    await frame.locator("button").evaluate("""element => {
        element.addEventListener('mouseenter', () => element.textContent = 'iframe 悬停成功');
    }""")
    result = await agent.execute(OWNER, "observe")
    result = await agent.execute(
        OWNER,
        "hover",
        observation_id=result.data["observation_id"],
        ref=element(result, "Shadow 按钮"),
    )
    assert "Shadow 悬停成功" in text(result)
    result = await agent.execute(
        OWNER,
        "hover",
        observation_id=result.data["observation_id"],
        ref=element(result, "子页面按钮"),
    )
    assert "iframe 悬停成功" in text(result)


async def test_drag_requires_both_elements_in_viewport(browser_agent, mouse_page):
    agent, page = browser_agent, mouse_page
    await page.evaluate("drop.style.marginTop='2000px'")
    result = await agent.execute(OWNER, "observe")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER,
            "drag",
            observation_id=result.data["observation_id"],
            source_ref=element(result, "拖动项目"),
            target_ref=element(result, "拖放区域"),
        )
    assert error.value.code == "TARGET_OUTSIDE_VIEWPORT"
    assert await page.evaluate("window.mouseDowns") == 0


async def test_shadow_dom_and_iframe_elements(browser_agent, site):
    result = await browser_agent.execute(OWNER, "open", url=site[0])
    await browser_agent.execute(OWNER, "wait", condition="text", value="尚未搜索")
    result = await browser_agent.execute(OWNER, "observe")
    assert element(result, "Shadow 按钮")
    assert element(result, "子页面按钮")
    result = await browser_agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "子页面按钮"),
    )
    assert result.data["success"]


async def test_stale_observations_and_replaced_elements_are_rejected(
    browser_agent, site
):
    agent = browser_agent
    old = await agent.execute(OWNER, "open", url=site[0])
    await agent.execute(OWNER, "observe")
    with pytest.raises(AgentBrowserError, match="观察编号已失效"):
        await agent.execute(
            OWNER,
            "click",
            observation_id=old.data["observation_id"],
            ref=element(old, "搜索"),
        )
    result = await agent.execute(OWNER, "observe")
    page = agent._sessions[OWNER].tabs["tab_1"]
    await page.evaluate("search.outerHTML = '<button id=search>搜索</button>'")
    with pytest.raises(AgentBrowserError, match="目标元素已变化"):
        await agent.execute(
            OWNER,
            "click",
            observation_id=result.data["observation_id"],
            ref=element(result, "搜索"),
        )


async def test_screenshot_coordinate_click_and_stale_guard(browser_agent, site):
    agent = browser_agent
    result = await agent.execute(OWNER, "open", url=site[0])
    with pytest.raises(AgentBrowserError, match="先获取当前视口截图"):
        await agent.execute(
            OWNER, "click", observation_id=result.data["observation_id"], x=20, y=20
        )
    shot = await agent.execute(OWNER, "screenshot")
    assert shot.image.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", shot.image[16:24]) == (1280, 720)
    page = agent._sessions[OWNER].tabs["tab_1"]
    await page.evaluate("result.textContent = '页面变化'")
    with pytest.raises(AgentBrowserError, match="页面已变化"):
        await agent.execute(
            OWNER, "click", observation_id=shot.data["observation_id"], x=20, y=20
        )
    shot = await agent.execute(OWNER, "screenshot")
    box = await page.locator("#search").bounding_box()
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=shot.data["observation_id"],
        x=box["x"] + box["width"] / 2,
        y=box["y"] + box["height"] / 2,
    )
    assert "搜索结果：" in text(result)


async def test_tabs_popup_switch_and_last_tab_close(browser_agent, site):
    agent = browser_agent
    result = await agent.execute(OWNER, "open", url=site[0])
    async with agent._sessions[OWNER].context.expect_page():
        result = await agent.execute(
            OWNER,
            "click",
            observation_id=result.data["observation_id"],
            ref=element(result, "查看详情"),
        )
    tabs = await agent.execute(OWNER, "tabs", operation="list")
    assert len(tabs.data["tabs"]) == 2
    assert tabs.data["active_tab"] == "tab_1"
    result = await agent.execute(OWNER, "tabs", operation="switch", tab_id="tab_2")
    result = await agent.execute(OWNER, "wait", condition="text", value="价格 129 元")
    assert "价格 129 元" in text(result)
    result = await agent.execute(OWNER, "tabs", operation="close", tab_id="tab_2")
    assert result.data["active_tab"] == "tab_1"
    result = await agent.execute(OWNER, "tabs", operation="close", tab_id="tab_1")
    assert result.data["session_closed"]
    assert OWNER not in agent._sessions


async def test_session_storage_and_cookie_isolation(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    first = agent._sessions[OWNER]
    await first.context.add_cookies(
        [{"name": "account", "value": "alice", "url": site[0]}]
    )
    await first.tabs["tab_1"].evaluate("localStorage.setItem('account', 'alice')")
    await agent.execute(OWNER, "open", url=site[0] + "/detail")
    assert agent._sessions[OWNER] is first
    assert (
        await first.tabs["tab_1"].evaluate("localStorage.getItem('account')") == "alice"
    )
    other = await agent.execute(OTHER, "open", url=site[0])
    second = agent._sessions[OTHER]
    assert second is not first
    assert await second.context.cookies() == []
    assert (
        await second.tabs["tab_1"].evaluate("localStorage.getItem('account')") is None
    )
    with pytest.raises(AgentBrowserError, match="观察编号已失效"):
        await agent.execute(
            OWNER,
            "click",
            observation_id=other.data["observation_id"],
            ref=element(other, "搜索"),
        )


async def test_retained_sessions_do_not_hold_execution_capacity(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    assert agent.service._semaphore._value == agent.service.max_concurrent_sessions
    agent.max_sessions = 1
    with pytest.raises(AgentBrowserError, match="会话数量已达上限"):
        await agent.execute(OTHER, "open", url=site[0])
    await agent.execute(OWNER, "close")
    await agent.execute(OTHER, "open", url=site[0])


async def test_idle_reaping_and_explicit_expiration(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    session = agent._sessions[OWNER]
    session.last_used -= 1000
    with pytest.raises(AgentBrowserError, match="空闲过期"):
        await agent.execute(OWNER, "observe")
    await agent.execute(OWNER, "open", url=site[0])
    agent._sessions[OWNER].last_used -= 1000
    await agent.reap_idle()
    assert OWNER not in agent._sessions
    assert session.context not in agent.service._browser.contexts


async def test_timeout_and_cancellation_release_locks_and_capacity(
    browser_agent, site, monkeypatch
):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    agent.action_timeout = 0.1
    with pytest.raises(AgentBrowserError, match="工具超时"):
        await agent.execute(OWNER, "wait", condition="text", value="不会出现的文字")
    agent.action_timeout = 30
    await agent.execute(OWNER, "observe")
    entered = asyncio.Event()
    original_act = agent._act

    async def track_act(*args):
        entered.set()
        return await original_act(*args)

    monkeypatch.setattr(agent, "_act", track_act)
    task = asyncio.create_task(
        agent.execute(OWNER, "wait", condition="text", value="不会出现的文字")
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not agent._sessions[OWNER].lock.locked()
    assert agent.service._semaphore._value == agent.service.max_concurrent_sessions
    assert not agent.service._active_tasks
    await agent.execute(OWNER, "observe")


@pytest.mark.parametrize("action", ["wait", "evaluate"])
async def test_shutdown_cancels_active_calls_saves_login_and_closes_contexts(
    browser_agent, site, monkeypatch, action
):
    agent = browser_agent
    opened = await agent.execute(OWNER, "open", url=site[0])
    context = agent._sessions[OWNER].context
    await context.add_cookies(
        [{"name": "login", "value": "saved-on-shutdown", "url": site[0]}]
    )
    entered = asyncio.Event()
    original_act = agent._act

    async def track_act(*args):
        entered.set()
        return await original_act(*args)

    monkeypatch.setattr(agent, "_act", track_act)
    arguments = (
        {"condition": "text", "value": "不会出现的文字"}
        if action == "wait"
        else {
            "observation_id": opened.data["observation_id"],
            "expression": "() => new Promise(()=>{})",
        }
    )
    task = asyncio.create_task(
        asyncio.wait_for(agent.execute(OWNER, action, **arguments), timeout=30)
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    await agent.close(pending_tasks=[task])
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not agent._sessions
    assert context not in agent.service._browser.contexts
    assert agent._reaper.done()
    assert agent.storage.read_state(OWNER)["cookies"][0]["value"] == "saved-on-shutdown"


async def test_browser_restart_reports_lost_session(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    await agent.service._browser.close()
    with pytest.raises(AgentBrowserError, match="浏览器已重启"):
        await agent.execute(OWNER, "observe")
    result = await agent.execute(OWNER, "open", url=site[0])
    assert result.data["success"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hello",
        "https://user:pass@example.com",
        "https://example.com:bad",
    ],
)
async def test_invalid_navigation_fails_before_context_creation(browser_agent, url):
    with pytest.raises(AgentBrowserError, match="HTTP/HTTPS"):
        await browser_agent.execute(OWNER, "open", url=url)
    assert not browser_agent._sessions


async def test_existing_renderer_stays_offline(browser_agent, site):
    png = await browser_agent.service.render_html(
        f'<html><body><img src="{site[0]}/image">离线</body></html>'
    )
    assert png.startswith(b"\x89PNG")
    assert site[1] == []


async def test_scroll_invalidates_old_coordinates_and_wait_returns_new_state(
    browser_agent, site
):
    agent = browser_agent
    result = await agent.execute(OWNER, "open", url=site[0])
    result = await agent.execute(
        OWNER,
        "scroll",
        observation_id=result.data["observation_id"],
        direction="down",
        pixels=600,
    )
    page = agent._sessions[OWNER].tabs["tab_1"]
    assert await page.evaluate("scrollY") > 0
    await page.evaluate("setTimeout(() => result.textContent='异步结果', 50)")
    result = await agent.execute(OWNER, "wait", condition="text", value="异步结果")
    assert "异步结果" in text(result)


@pytest.mark.parametrize(
    "kwargs", [{"max_sessions": 0}, {"idle_timeout": True}, {"action_timeout": 999}]
)
def test_invalid_agent_configuration(kwargs):
    with pytest.raises(BrowserServiceError, match="必须是"):
        AgentBrowser(BrowserService(), **kwargs)


async def test_scroll_exposes_later_elements_on_long_pages(browser_agent, site):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    page = agent._sessions[OWNER].tabs["tab_1"]
    await page.set_content(
        "<html><body>"
        + "".join(
            f'<button style="display:block;height:50px">条目 {index}</button>'
            for index in range(200)
        )
        + "</body></html>"
    )
    result = await agent.execute(OWNER, "observe")
    assert result.data["frames"][0]["elements_truncated"]
    await page.evaluate("scrollTo(0, document.body.scrollHeight)")
    result = await agent.execute(OWNER, "observe")
    assert element(result, "条目 199")
    assert "条目 199" in result.data["frames"][0]["viewport_text"]


@pytest.mark.parametrize(
    "action,args",
    [
        ("open", {"url": "https://example.com", "legacy_option": True}),
        ("click", {"ref": ["e1"]}),
        ("tabs", {"operation": []}),
        ("scroll", {"pixels": True}),
        ("click", {"x": float("nan")}),
        ("click", {"click_count": True}),
        ("click", {"click_count": 1.5}),
        ("click", {"button": []}),
        ("drag", {"end_y": float("inf")}),
        ("drag", {"start_x": True}),
    ],
)
async def test_invalid_arguments_fail_before_browser_access(action, args):
    agent = AgentBrowser(BrowserService(), tool_level="standard")
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OWNER, action, **args)
    assert error.value.code == "INVALID_ARGUMENT"
    assert agent.service._browser is None


async def test_cancelled_invalidation_can_finish_releasing_remote_handles():
    snapshot = SimpleNamespace(
        evaluate=AsyncMock(side_effect=[asyncio.CancelledError(), None]),
        dispose=AsyncMock(),
    )
    session = AgentSession(context=None)
    session.snapshots.append(snapshot)
    with pytest.raises(asyncio.CancelledError):
        await session.invalidate()
    await session.invalidate()
    snapshot.dispose.assert_awaited_once()
    assert not session.snapshots


async def test_wait_ignores_hidden_matches(browser_agent, site):
    await browser_agent.execute(OWNER, "open", url=site[0])
    page = browser_agent._sessions[OWNER].tabs["tab_1"]
    await page.set_content('<p style="display:none">完成</p><p>完成</p>')
    result = await browser_agent.execute(OWNER, "wait", condition="text", value="完成")
    assert "完成" in text(result)


async def test_shadow_dom_changes_invalidate_coordinate_screenshot(browser_agent, site):
    await browser_agent.execute(OWNER, "open", url=site[0])
    shot = await browser_agent.execute(OWNER, "screenshot")
    page = browser_agent._sessions[OWNER].tabs["tab_1"]
    await page.evaluate(
        "shadow.shadowRoot.querySelector('button').textContent = '已更新'"
    )
    with pytest.raises(AgentBrowserError) as error:
        await browser_agent.execute(
            OWNER, "click", observation_id=shot.data["observation_id"], x=20, y=20
        )
    assert error.value.code == "STALE_SCREENSHOT"


@pytest.fixture
async def advanced_agent(browser_agent):
    return browser_agent


async def test_advanced_network_records_response_body_stop_clear_and_isolation(
    advanced_agent, site
):
    agent = advanced_agent
    await agent.execute(OWNER, "network", operation="start", url_pattern="*/api/*")
    opened = await agent.execute(OWNER, "open", url=site[0] + "/advanced")
    page = agent._sessions[OWNER].tabs["tab_1"]
    assert (await agent.execute(OWNER, "network")).data["requests"] == []
    await agent.execute(
        OWNER,
        "click",
        observation_id=opened.data["observation_id"],
        ref=element(opened, "请求数据"),
    )
    await page.get_by_text("real", exact=True).wait_for()
    records = (await agent.execute(OWNER, "network")).data["requests"]
    assert len(records) == 1
    assert records[0]["status"] == 200 and records[0]["finished"]
    request_id = records[0]["request_id"]
    detail = await agent.execute(
        OWNER, "network", operation="detail", request_id=request_id, include_body=True
    )
    assert json.loads(detail.data["request"]["response_body"])["source"] == "real"
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OTHER, "network", operation="detail", request_id=request_id)
    assert error.value.code == "REQUEST_NOT_FOUND"
    await agent.execute(OWNER, "network", operation="stop")
    await page.evaluate("fetch('/api/second')")
    assert len((await agent.execute(OWNER, "network")).data["requests"]) == 1
    await agent.execute(OWNER, "network", operation="clear")
    assert (await agent.execute(OWNER, "network")).data["requests"] == []


async def test_advanced_intercept_mock_modify_abort_order_and_isolation(
    advanced_agent, site
):
    agent = advanced_agent
    await agent.execute(OWNER, "network", operation="start", url_pattern="*/api/*")
    first = await agent.execute(
        OWNER,
        "intercept",
        operation="add",
        rule={
            "url_pattern": "*/api/data",
            "action": "fulfill",
            "body": '{"source":"mock"}',
        },
    )
    await agent.execute(
        OWNER,
        "intercept",
        operation="add",
        rule={"url_pattern": "*/api/data", "action": "abort"},
    )
    await agent.execute(OWNER, "open", url=site[0] + "/advanced")
    page = agent._sessions[OWNER].tabs["tab_1"]
    assert await page.evaluate("fetch('/api/data').then(r=>r.json())") == {
        "source": "mock"
    }
    assert "/api/data" not in site[1]
    await agent.execute(OTHER, "open", url=site[0] + "/advanced")
    other = agent._sessions[OTHER].tabs["tab_1"]
    assert (await other.evaluate("fetch('/api/data').then(r=>r.json())"))[
        "source"
    ] == "real"
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OTHER, "intercept", operation="remove", rule_id=first.data["rule_id"]
        )
    assert error.value.code == "RULE_NOT_FOUND"
    await agent.execute(
        OWNER, "intercept", operation="remove", rule_id=first.data["rule_id"]
    )
    assert await page.evaluate("fetch('/api/data').then(()=>false).catch(()=>true)")
    failed = (await agent.execute(OWNER, "network")).data["requests"][-1]
    assert failed["finished"] and failed["failure"]
    await agent.execute(
        OWNER,
        "intercept",
        operation="add",
        rule={
            "url_pattern": "*/api/original",
            "action": "modify",
            "url": site[0] + "/api/echo",
            "method": "POST",
            "headers": {"X-Browser-Test": "yes"},
            "post_data": "hello",
        },
    )
    modified = await page.evaluate("fetch('/api/original').then(r=>r.json())")
    assert modified == {
        "source": "real",
        "method": "POST",
        "custom_header": "yes",
        "body": "hello",
    }
    assert "/api/echo" in site[1] and "/api/original" not in site[1]
    await agent.execute(OWNER, "intercept", operation="clear")
    assert (await page.evaluate("fetch('/api/data').then(r=>r.json())"))[
        "source"
    ] == "real"


async def test_advanced_environment_rebuild_preserves_login_and_applies_settings(
    advanced_agent, site
):
    agent = advanced_agent
    old = await agent.execute(OWNER, "open", url=site[0] + "/advanced")
    previous = agent._sessions[OWNER]
    old_page = previous.tabs["tab_1"]
    await old_page.evaluate("localStorage.setItem('account','alice')")
    await previous.context.add_cookies(
        [{"name": "login", "value": "alice", "url": site[0]}]
    )
    await agent.execute(OWNER, "network", operation="start")
    rule = await agent.execute(
        OWNER,
        "intercept",
        operation="add",
        rule={"url_pattern": "*/blocked", "action": "abort"},
    )
    changed = await agent.execute(
        OWNER,
        "emulate",
        operation="set",
        settings={
            "viewport": {"width": 390, "height": 844},
            "device_scale_factor": 2,
            "is_mobile": True,
            "has_touch": True,
            "user_agent": "BrowserUseMobileTest",
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "color_scheme": "dark",
            "reduced_motion": "reduce",
            "geolocation": {"latitude": 31.2, "longitude": 121.5},
        },
    )
    assert changed.data["session_rebuilt"]
    assert old_page.is_closed()
    assert not agent.storage.state_path(OWNER).is_file()
    await agent.execute(OWNER, "open", url=site[0] + "/advanced")
    page = agent._sessions[OWNER].tabs["tab_1"]
    actual = await page.evaluate("""() => ({width:innerWidth, ratio:devicePixelRatio, touch:navigator.maxTouchPoints,
        lang:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
        dark:matchMedia('(prefers-color-scheme:dark)').matches,
        reduced:matchMedia('(prefers-reduced-motion:reduce)').matches,
        ua:navigator.userAgent, account:localStorage.getItem('account')})""")
    assert actual == {
        "width": 390,
        "ratio": 2,
        "touch": 1,
        "lang": "zh-CN",
        "timezone": "Asia/Shanghai",
        "dark": True,
        "reduced": True,
        "ua": "BrowserUseMobileTest",
        "account": "alice",
    }
    position = await page.evaluate(
        "() => new Promise((resolve,reject) => navigator.geolocation.getCurrentPosition(p=>resolve([p.coords.latitude,p.coords.longitude]),reject))"
    )
    assert position == [31.2, 121.5]
    assert (await agent._sessions[OWNER].context.cookies())[0]["value"] == "alice"
    assert (await agent.execute(OWNER, "intercept")).data["rules"][0][
        "rule_id"
    ] == rule.data["rule_id"]
    shot = await agent.execute(OWNER, "screenshot")
    assert struct.unpack(">II", shot.image[16:24]) == (390, 844)
    await agent.execute(
        OWNER, "click", observation_id=shot.data["observation_id"], x=30, y=18
    )
    await page.get_by_text("real", exact=True).wait_for()
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER, "click", observation_id=old.data["observation_id"], ref="e1"
        )
    assert error.value.code == "STALE_OBSERVATION"
    await agent.execute(OWNER, "emulate", operation="reset")
    assert (await agent.execute(OWNER, "emulate")).data["environment"] == {
        "viewport": {"width": 1280, "height": 720},
        "device_scale_factor": 1,
    }


async def test_advanced_invalid_environment_keeps_current_context(advanced_agent, site):
    agent = advanced_agent
    await agent.execute(OWNER, "open", url=site[0] + "/advanced")
    previous = agent._sessions[OWNER]
    with pytest.raises(AgentBrowserError):
        await agent.execute(
            OWNER,
            "emulate",
            operation="set",
            settings={"timezone_id": "Invalid/Timezone"},
        )
    assert agent._sessions[OWNER] is previous
    assert not previous.tabs["tab_1"].is_closed()


async def test_advanced_javascript_arguments_async_iframe_and_bounded_result(
    advanced_agent, site
):
    agent = advanced_agent
    opened = await agent.execute(OWNER, "open", url=site[0] + "/advanced")
    result = await agent.execute(
        OWNER,
        "evaluate",
        observation_id=opened.data["observation_id"],
        expression="async arg => {await Promise.resolve();document.querySelector('#result').textContent=arg.value;return {value:arg.value,title:document.title}}",
        argument={"value": "脚本完成"},
    )
    assert result.data["evaluation"]["result"] == {
        "value": "脚本完成",
        "title": "高级能力测试",
    }
    assert "脚本完成" in text(result)
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER,
            "evaluate",
            observation_id=opened.data["observation_id"],
            expression="document.title",
        )
    assert error.value.code == "STALE_OBSERVATION"
    observed = await agent.execute(
        OWNER, "wait", condition="selector", value="button", frame_index=1
    )
    iframe = await agent.execute(
        OWNER,
        "evaluate",
        observation_id=observed.data["observation_id"],
        expression="() => document.querySelector('button').textContent",
        frame_index=1,
    )
    assert iframe.data["evaluation"]["result"] == "子页面按钮"
    oversized = await agent.execute(
        OWNER,
        "evaluate",
        observation_id=iframe.data["observation_id"],
        expression="'x'.repeat(30000)",
    )
    assert oversized.data["evaluation"]["truncated"]
    assert len(oversized.data["evaluation"]["result_preview"]) == 24000


@pytest.mark.parametrize(
    "expression,code",
    [
        ("document.body", "SCRIPT_RESULT_UNSUPPORTED"),
        ("(() => {const a={};a.self=a;return a})()", "SCRIPT_RESULT_UNSUPPORTED"),
        ("(() => {throw new Error('test failure')})()", "SCRIPT_ERROR"),
    ],
)
async def test_advanced_javascript_invalid_returns_are_clear(
    advanced_agent, site, expression, code
):
    opened = await advanced_agent.execute(OWNER, "open", url=site[0])
    with pytest.raises(AgentBrowserError) as error:
        await advanced_agent.execute(
            OWNER,
            "evaluate",
            observation_id=opened.data["observation_id"],
            expression=expression,
        )
    assert error.value.code == code
    assert (await advanced_agent.execute(OWNER, "observe")).data["success"]


@pytest.mark.parametrize(
    "expression", ["() => new Promise(()=>{})", "() => {while(true){}}"]
)
async def test_advanced_javascript_timeout_releases_stuck_page(
    advanced_agent, site, expression
):
    agent = advanced_agent
    opened = await agent.execute(OWNER, "open", url=site[0])
    agent.action_timeout = 0.3
    with pytest.raises(AgentBrowserError) as error:
        await asyncio.wait_for(
            agent.execute(
                OWNER,
                "evaluate",
                observation_id=opened.data["observation_id"],
                expression=expression,
            ),
            timeout=8,
        )
    assert error.value.code == "TIMEOUT"
    assert OWNER not in agent._sessions
    agent.action_timeout = 5
    assert (await agent.execute(OWNER, "open", url=site[0])).data["success"]


async def test_advanced_scaled_mobile_page_uses_elements_instead_of_mismatched_coordinates(
    advanced_agent, site
):
    agent = advanced_agent
    await agent.execute(
        OWNER,
        "emulate",
        operation="set",
        settings={
            "viewport": {"width": 390, "height": 844},
            "is_mobile": True,
            "has_touch": True,
        },
    )
    await agent.execute(OWNER, "open", url=site[0])
    shot = await agent.execute(OWNER, "screenshot")
    assert shot.data["coordinate_click_available"] is False
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(
            OWNER, "click", observation_id=shot.data["observation_id"], x=30, y=18
        )
    assert error.value.code == "COORDINATES_UNAVAILABLE"
    result = await agent.execute(OWNER, "observe")
    result = await agent.execute(
        OWNER,
        "click",
        observation_id=result.data["observation_id"],
        ref=element(result, "搜索"),
    )
    assert "搜索结果" in text(result)


@pytest.fixture
async def mode_agent(browser_agent, monkeypatch):
    """Exercise two real Browser processes without requiring a desktop in CI."""
    chromium = browser_agent.service._playwright.chromium
    launch = chromium.launch

    async def launch_without_window(**kwargs):
        return await launch(**{**kwargs, "headless": True})

    monkeypatch.setattr(chromium, "launch", launch_without_window)
    return browser_agent


async def test_mode_status_disabled_and_invalid_do_not_start_browser(tmp_path):
    agent = AgentBrowser(BrowserService(), data_dir=tmp_path, headed_enabled=False)
    try:
        assert not (await agent.execute(OWNER, "mode")).data["session_active"]
        assert not (await agent.execute(OWNER, "mode", mode="headless")).data[
            "session_active"
        ]
        for value, code in [
            ("headed", "HEADED_DISABLED"),
            ("invalid", "INVALID_ARGUMENT"),
        ]:
            with pytest.raises(AgentBrowserError) as error:
                await agent.execute(OWNER, "mode", mode=value)
            assert error.value.code == code
        await agent.execute(OWNER, "close")
        assert agent.service._playwright is None
    finally:
        await agent.close()


async def test_mode_switch_preserves_login_environment_and_never_writes_state(
    mode_agent, site
):
    agent = mode_agent
    await agent.execute(OWNER, "open", url=site[0])
    previous = agent._sessions[OWNER]
    previous_page = previous.tabs["tab_1"]
    await previous.context.add_cookies(
        [{"name": "login", "value": "alice", "url": site[0]}]
    )
    await previous.tabs["tab_1"].evaluate("localStorage.setItem('account','alice')")
    await agent.execute(OWNER, "state", operation="clear")
    await agent.execute(OWNER, "mode", mode="headed")
    headed = agent.service._headed_browser
    assert headed is not agent.service._browser
    assert previous_page.is_closed()
    assert agent._sessions[OWNER].mode == "headed"
    assert not agent._sessions[OWNER].auto_save
    assert (await agent.execute(OWNER, "open", url=site[0])).data["mode"] == "headed"
    page = agent._sessions[OWNER].tabs["tab_1"]
    assert await page.evaluate("localStorage.getItem('account')") == "alice"
    assert (await agent._sessions[OWNER].context.cookies())[0]["value"] == "alice"
    await agent.execute(OWNER, "emulate", operation="set", settings={"locale": "zh-CN"})
    assert agent._sessions[OWNER].mode == "headed"
    assert agent.service._headed_browser is headed
    await agent.execute(OWNER, "mode", mode="headless")
    assert agent._sessions[OWNER].environment["locale"] == "zh-CN"
    assert not agent._sessions[OWNER].auto_save
    assert agent.service._headed_browser is None and not headed.is_connected()
    assert agent._headed_owner is None
    assert not agent.storage.state_path(OWNER).is_file()
    assert agent.service.ready


async def test_headed_exclusivity_idempotence_and_headless_users(mode_agent, site):
    agent = mode_agent
    await agent.execute(OWNER, "mode", mode="headed")
    first = agent._sessions[OWNER]
    assert not (await agent.execute(OWNER, "mode", mode="headed")).data[
        "session_rebuilt"
    ]
    assert agent._sessions[OWNER] is first
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OTHER, "mode", mode="headed")
    assert error.value.code == "HEADED_BUSY"
    assert (await agent.execute(OTHER, "open", url=site[0])).data["mode"] == "headless"
    assert (await agent.service.render_html("<html>offline</html>")).startswith(
        b"\x89PNG"
    )
    assert (await agent.execute(OWNER, "close")).data["state_saved"] is True
    assert agent.service._headed_browser is None
    await agent.execute(OTHER, "mode", mode="headed")
    assert agent._headed_owner == OTHER


@pytest.mark.parametrize("end", ["close", "last_tab", "idle", "shutdown"])
async def test_headed_end_saves_once_and_releases_browser(
    mode_agent, site, monkeypatch, end
):
    agent = mode_agent
    await agent.execute(OWNER, "mode", mode="headed")
    await agent.execute(OWNER, "open", url=site[0])
    session = agent._sessions[OWNER]
    headed = agent.service._headed_browser
    await session.tabs["tab_1"].evaluate("localStorage.setItem('account','headed')")
    write = agent.storage.write_state
    writes = []

    def record(owner, state):
        writes.append(owner)
        return write(owner, state)

    monkeypatch.setattr(agent.storage, "write_state", record)
    if end == "close":
        await agent.execute(OWNER, "close")
    elif end == "last_tab":
        await agent.execute(OWNER, "tabs", operation="close", tab_id="tab_1")
    elif end == "idle":
        session.last_used = 0
        await agent.reap_idle()
    else:
        await agent.close()
    assert writes == [OWNER]
    assert agent._headed_owner is None and agent.service._headed_browser is None
    assert not headed.is_connected() and agent.service.ready
    assert OWNER not in agent._sessions
    if end != "shutdown":
        assert (await agent.execute(OWNER, "open", url=site[0])).data[
            "mode"
        ] == "headless"
        assert (
            await agent._sessions[OWNER]
            .tabs["tab_1"]
            .evaluate("localStorage.getItem('account')")
            == "headed"
        )


async def test_headed_restore_and_clear_stay_in_current_mode(mode_agent, site):
    agent = mode_agent
    await agent.execute(OWNER, "mode", mode="headed")
    await agent.execute(OWNER, "open", url=site[0])
    await agent.execute(OWNER, "state", operation="save")
    headed = agent.service._headed_browser
    await agent.execute(OWNER, "state", operation="restore")
    assert agent._sessions[OWNER].mode == "headed"
    assert agent.service._headed_browser is headed
    await agent.execute(OWNER, "state", operation="clear")
    assert (await agent.execute(OWNER, "close")).data["state_saved"] is None
    assert not agent.storage.state_path(OWNER).is_file()


@pytest.mark.parametrize("failure", ["launch", "context", "cancel"])
async def test_failed_mode_switch_retains_original_session(
    mode_agent, site, monkeypatch, failure
):
    agent = mode_agent
    await agent.execute(OWNER, "open", url=site[0])
    previous = agent._sessions[OWNER]
    if failure == "launch":
        monkeypatch.setattr(
            agent.service._playwright.chromium,
            "launch",
            AsyncMock(side_effect=PlaywrightError("no display")),
        )
    else:
        monkeypatch.setattr(
            agent,
            "_new_session",
            AsyncMock(
                side_effect=asyncio.CancelledError()
                if failure == "cancel"
                else PlaywrightError("new context failed")
            ),
        )
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else AgentBrowserError
    ):
        await agent.execute(OWNER, "mode", mode="headed")
    assert agent._sessions[OWNER] is previous
    assert not previous.tabs["tab_1"].is_closed()
    assert agent.service.ready and agent.service._headed_browser is None
    assert agent._headed_owner is None
    assert not agent.storage.state_path(OWNER).is_file()


async def test_switch_serializes_owner_but_keeps_other_users_running(
    mode_agent, site, monkeypatch
):
    agent = mode_agent
    await agent.execute(OWNER, "open", url=site[0])
    launch = agent.service._playwright.chromium.launch
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_launch(**kwargs):
        started.set()
        await release.wait()
        return await launch(**kwargs)

    monkeypatch.setattr(agent.service._playwright.chromium, "launch", delayed_launch)
    switch = asyncio.create_task(agent.execute(OWNER, "mode", mode="headed"))
    observe = None
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        observe = asyncio.create_task(agent.execute(OWNER, "observe"))
        assert (
            await asyncio.wait_for(agent.execute(OTHER, "open", url=site[0]), timeout=3)
        ).data["mode"] == "headless"
        assert not observe.done()
    finally:
        release.set()
        await switch
        if observe is not None:
            assert (await observe).data["mode"] == "headed"


async def test_headed_manual_activity_refreshes_idle_timer(mode_agent, site):
    agent = mode_agent
    await agent.execute(OWNER, "mode", mode="headed")
    await agent.execute(OWNER, "open", url=site[0])
    session = agent._sessions[OWNER]
    page = session.tabs["tab_1"]
    activity = asyncio.Event()
    await page.expose_function("__test_activity_received", activity.set)
    await page.evaluate("""() => {
        const original = window.__astrbot_browser_activity;
        window.__astrbot_browser_activity = async () => {
            await original();
            await window.__test_activity_received();
        };
    }""")
    session.last_used = 0
    await page.mouse.click(20, 20)
    await asyncio.wait_for(activity.wait(), timeout=2)
    assert time.monotonic() - session.last_used < 2
    await agent.reap_idle()
    assert agent._sessions[OWNER] is session
    assert not agent.storage.state_path(OWNER).is_file()


@pytest.mark.parametrize("external", ["page", "context", "browser"])
async def test_external_headed_close_releases_slot_without_reopening(
    mode_agent, site, external
):
    agent = mode_agent
    await agent.execute(OWNER, "mode", mode="headed")
    await agent.execute(OWNER, "open", url=site[0])
    previous = agent._sessions[OWNER]
    if external == "page":
        await previous.tabs["tab_1"].close()
    elif external == "context":
        await previous.context.close()
    else:
        await agent.service._headed_browser.close()
    if agent._maintenance:
        await asyncio.gather(*list(agent._maintenance))
    assert OWNER not in agent._sessions
    assert agent._headed_owner is None and agent.service._headed_browser is None
    assert agent.service.ready
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OWNER, "observe")
    assert error.value.code == "NO_SESSION"
    assert agent.service._headed_browser is None
    await agent.execute(OTHER, "mode", mode="headed")
    await agent._external_close(previous)
    assert agent._sessions[OTHER].mode == "headed"


@pytest.mark.skipif(
    sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
    reason="A graphical desktop is required",
)
async def test_real_headed_window_runs_existing_tools_and_preserves_renderer(
    browser_agent, site
):
    agent = browser_agent
    await agent.execute(OWNER, "mode", mode="headed")
    opened = await agent.execute(OWNER, "open", url=site[0])
    filled = await agent.execute(
        OWNER,
        "fill",
        observation_id=opened.data["observation_id"],
        ref=element(opened, "搜索商品"),
        text="有头浏览",
    )
    result = await agent.execute(
        OWNER, "press", observation_id=filled.data["observation_id"], key="Enter"
    )
    assert result.data["mode"] == "headed" and "回车：有头浏览" in text(result)
    assert (await agent.execute(OWNER, "screenshot")).image.startswith(b"\x89PNG")
    assert (await agent.service.render_html("<html>still offline</html>")).startswith(
        b"\x89PNG"
    )
    await agent.execute(OWNER, "close")
    assert agent.service._headed_browser is None and agent.service.ready
