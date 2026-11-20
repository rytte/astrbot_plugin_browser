"""Verify that only manually committed state survives a browser service restart."""

import asyncio
import base64
import time
from pathlib import Path

import pytest
from astrbot_plugin_browser.agent_browser import AgentBrowser
from astrbot_plugin_browser.browser_errors import AgentBrowserError
from astrbot_plugin_browser.manual_login import ManualLogin
from astrbot_plugin_browser.service import BrowserService, find_system_chromium
from playwright.async_api import async_playwright

OWNER = ("test:FriendMessage:one", "alice")
OTHER = ("test:FriendMessage:two", "alice")
URL = "https://login.test/"
ADMIN = "admin"


async def test_manual_login_disconnect_and_restart(tmp_path, monkeypatch):
    async with async_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
    if not await asyncio.to_thread(executable.is_file):
        executable = find_system_chromium()
    if executable is None:
        pytest.skip("Install Chromium to verify manual login")
    service = BrowserService(browser_executable=str(executable))
    agent = AgentBrowser(service, data_dir=tmp_path, tool_level="full")
    login = ManualLogin(agent)

    async def navigate(page, url):
        await page.route(
            url,
            lambda route: route.fulfill(
                content_type="text/html",
                body="""<style>body{margin:0} input,button{width:200px;height:30px;display:block}</style>
                <input type=password id=password>
                <button onclick="if (password.value === 'fake password') {
                   document.cookie = 'session=active; Secure; Path=/';
                   localStorage.setItem('account', 'signed-in');
                   document.body.textContent = 'Signed in';
                }">Submit</button>""",
            ),
        )
        await page.goto(url)

    monkeypatch.setattr(login, "_navigate", navigate)

    async def begin():
        await login.request(OWNER)
        status = await login.operate(ADMIN, {"action": "status"})
        assert status["offer"]["origin"] == OWNER[0]
        assert status["offer"]["sender"] == OWNER[1]
        data = await login.operate(
            ADMIN, {"action": "start", "offer": status["offer"]["id"], "url": URL}
        )
        session = login.session
        assert session is not None
        await session.navigation
        payload = {"token": data["token"]}
        image = await login.operate(ADMIN, {"action": "frame", **payload})
        assert base64.b64decode(image["jpeg"]).startswith(b"\xff\xd8")
        return session, {**payload, "frame": image["frame"]}

    try:
        async with service._operation() as browser:
            previous = await agent._get_session(OWNER, browser, create=True)
        previous.network.active = True
        previous.network.entries["old"] = object()
        session, control = await begin()
        assert session.context is not previous.context
        assert OWNER not in agent._sessions
        assert not previous.network.active and not previous.network.entries
        for action, args in (
            ("open", {"url": URL}),
            ("network", {}),
            ("screenshot", {}),
            ("evaluate", {"expression": "document.title"}),
            ("close", {}),
        ):
            with pytest.raises(AgentBrowserError, match="正在人工登录"):
                await agent.execute(OWNER, action, **args)
        assert (await agent.execute(OTHER, "mode", mode="status")).data["success"]
        with pytest.raises(AgentBrowserError, match="无权访问"):
            await login.operate("other-admin", {"action": "frame", **control})
        with pytest.raises(AgentBrowserError, match="页面已变化"):
            await login.operate(
                ADMIN,
                {**control, "action": "text", "frame": "stale", "text": "invalid"},
            )
        await login.operate(ADMIN, {"action": "click", "x": 30, "y": 15, **control})
        await login.operate(
            ADMIN, {"action": "text", "text": "fake password", **control}
        )
        await login.operate(ADMIN, {"action": "click", "x": 30, "y": 50, **control})
        await session.page.get_by_text("Signed in").wait_for()
        assert (await login.operate(ADMIN, {"action": "save", **control})) == {
            "saved": True
        }
        assert session.page.is_closed() and agent.manual_login_owner is None
        assert "fake password" not in agent.storage.state_path(OWNER).read_text()
        saved = agent.storage.read_state(OWNER)

        session, abandoned = await begin()
        await session.context.add_cookies(
            [{"url": URL, "name": "abandoned", "value": "unsaved"}]
        )
        # Stop polling, as when the Dashboard tab is closed or loses connectivity.
        disconnected = asyncio.Event()
        session.context.once("close", lambda _: disconnected.set())
        session.heartbeat = time.monotonic() - 60
        await asyncio.wait_for(disconnected.wait(), 8)
        with pytest.raises(AgentBrowserError, match="无权访问"):
            await login.operate(ADMIN, {"action": "save", **abandoned})
        assert session.page.is_closed()
        assert agent.manual_login_owner is None
        assert agent.storage.read_state(OWNER) == saved
    finally:
        await login.close()
        await agent.close()
        await service.close()

    restarted_service = BrowserService(browser_executable=str(executable))
    restarted = AgentBrowser(restarted_service, data_dir=tmp_path)
    try:
        async with restarted_service._operation() as new_browser:
            restored = await restarted._get_session(OWNER, new_browser, create=True)
            isolated = await restarted._get_session(OTHER, new_browser, create=True)
            assert {
                cookie["name"]: cookie["value"]
                for cookie in await restored.context.cookies(URL)
            } == {"session": "active"}
            assert not await isolated.context.cookies(URL)
            page = await restored.context.new_page()
            # Fall through the agent's request handler to the test page.
            await navigate(page, URL)
            assert await page.evaluate("localStorage.getItem('account')") == "signed-in"
    finally:
        await restarted.close()
        await restarted_service.close()
