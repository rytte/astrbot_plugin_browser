"""Exercise agent operations against a real local Chromium and HTTP site."""

import asyncio
import struct
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
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
            if path == "/detail":
                body = "<html><title>商品详情</title><body>价格 129 元</body></html>"
            elif path == "/frame":
                body = "<html><body><button>子页面按钮</button></body></html>"
            else:
                body = HTML
            payload = body.encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
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
        await server.wait_closed()
        for task in list(connections):
            task.cancel()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)


@pytest.fixture
async def browser_agent():
    async with async_playwright() as playwright:
        bundled = Path(playwright.chromium.executable_path)
    executable = bundled if bundled.is_file() else find_system_chromium()
    if executable is None:
        pytest.skip("Install Playwright Chromium to run browser integration tests")
    service = BrowserService(browser_executable=str(executable))
    agent = AgentBrowser(service)
    await service.initialize()
    agent.start()
    try:
        yield agent
    finally:
        await agent.close()
        await service.close()


def element(result, name):
    return next(
        item["ref"]
        for frame in result.data["frames"]
        for item in frame["elements"]
        if item["name"] == name
    )


def text(result):
    return "\n".join(frame["text"] for frame in result.data["frames"])


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


async def test_shadow_dom_and_iframe_elements(browser_agent, site):
    result = await browser_agent.execute(OWNER, "open", url=site[0])
    await browser_agent.execute(OWNER, "wait", text="尚未搜索")
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
    result = await agent.execute(OWNER, "wait", text="价格 129 元")
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
        await agent.execute(OWNER, "wait", text="不会出现的文字")
    agent.action_timeout = 30
    await agent.execute(OWNER, "observe")
    entered = asyncio.Event()
    original_act = agent._act

    async def track_act(*args):
        entered.set()
        return await original_act(*args)

    monkeypatch.setattr(agent, "_act", track_act)
    task = asyncio.create_task(agent.execute(OWNER, "wait", text="不会出现的文字"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not agent._sessions[OWNER].lock.locked()
    assert agent.service._semaphore._value == agent.service.max_concurrent_sessions
    assert not agent.service._active_tasks
    await agent.execute(OWNER, "observe")


async def test_shutdown_cancels_active_calls_and_closes_contexts(
    browser_agent, site, monkeypatch
):
    agent = browser_agent
    await agent.execute(OWNER, "open", url=site[0])
    context = agent._sessions[OWNER].context
    entered = asyncio.Event()
    original_act = agent._act

    async def track_act(*args):
        entered.set()
        return await original_act(*args)

    monkeypatch.setattr(agent, "_act", track_act)
    task = asyncio.create_task(agent.execute(OWNER, "wait", text="不会出现的文字"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await agent.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not agent._sessions
    assert context not in agent.service._browser.contexts
    assert agent._reaper.done()


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
    result = await agent.execute(OWNER, "wait", text="异步结果")
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
    ],
)
async def test_invalid_arguments_fail_before_browser_access(action, args):
    agent = AgentBrowser(BrowserService())
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
    result = await browser_agent.execute(OWNER, "wait", text="完成")
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
