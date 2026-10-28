"""Exercise CDP against disposable user-owned browsers, never desktop sessions."""

import asyncio
import ctypes
import ctypes.util
import json
import os
import shutil
import signal
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_browser.agent_browser import AgentBrowser
from astrbot_plugin_browser.browser_cdp import (
    LocalCDP,
    _discover_running_chrome,
    chrome_user_data_dir,
)
from astrbot_plugin_browser.browser_errors import AgentBrowserError
from astrbot_plugin_browser.service import (
    BrowserService,
    find_system_chromium,
)
from playwright.async_api import async_playwright

OWNER = ("test:private", "alice")
OTHER = ("test:another-chat", "alice")


@pytest.mark.parametrize(
    ("platform", "environment", "expected"),
    [
        ("linux", {}, "/account/.config/google-chrome"),
        ("linux", {"XDG_CONFIG_HOME": "/xdg"}, "/xdg/google-chrome"),
        (
            "linux",
            {"CHROME_CONFIG_HOME": "/chrome", "XDG_CONFIG_HOME": "/xdg"},
            "/chrome/google-chrome",
        ),
        ("darwin", {}, "/account/Library/Application Support/Google/Chrome"),
        ("win32", {"LOCALAPPDATA": "/local"}, "/local/Google/Chrome/User Data"),
    ],
)
def test_running_chrome_default_profile_location(
    monkeypatch, platform, environment, expected
):
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.sys.platform", platform)
    monkeypatch.setattr(Path, "home", lambda: Path("/account"))
    for key in ("XDG_CONFIG_HOME", "CHROME_CONFIG_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert chrome_user_data_dir() == Path(expected)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"9222",
        b"9222\n/devtools/page/tab",
        b"9222\n/devtools/browser/",
        b"0\n/devtools/browser/test",
        b"65536\n/devtools/browser/test",
        b"9222@evil\n/devtools/browser/test",
        b"9222\n//example.com/devtools/browser/test",
        b"9222\n/devtools/browser/test?redirect=evil",
        b"9222\n/devtools/browser/test#fragment",
        b"9222\n/devtools/browser/test\nextra",
        b"9222\n/devtools/browser/\xff",
        b"9222\n/devtools/browser/" + b"a" * 4096,
    ],
)
async def test_auto_discovery_rejects_corrupt_or_unsafe_files(
    tmp_path, monkeypatch, payload
):
    (tmp_path / "DevToolsActivePort").write_bytes(payload)
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    cdp = LocalCDP(enabled=True, user_data_dir=str(tmp_path))
    with pytest.raises(AgentBrowserError) as error:
        await cdp.connect(1)
    assert error.value.code == "CDP_DISCOVERY_FAILED"
    start.assert_not_called()


async def test_auto_discovery_missing_file_never_falls_back_or_creates_profile(
    tmp_path, monkeypatch
):
    profile = tmp_path / "missing-profile"
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    cdp = LocalCDP(enabled=True, user_data_dir=str(profile))
    with pytest.raises(AgentBrowserError) as error:
        await cdp.connect(1)
    assert error.value.code == "CDP_DISCOVERY_FAILED"
    assert not profile.exists()
    start.assert_not_called()


def test_auto_discovery_rereads_dynamic_port_and_uses_loopback(tmp_path):
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_bytes(b"9222\r\n/devtools/browser/first\r\n")
    assert (
        _discover_running_chrome(tmp_path)
        == "ws://127.0.0.1:9222/devtools/browser/first"
    )
    port_file.write_text("9333\n/devtools/browser/second\n")
    assert (
        _discover_running_chrome(tmp_path)
        == "ws://127.0.0.1:9333/devtools/browser/second"
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_auto_connect_denial_or_pending_authorization_cleans_driver(
    tmp_path, monkeypatch, cancel
):
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/approval\n")
    requested = asyncio.Event()

    async def connect(endpoint, **kwargs):
        assert endpoint == "ws://127.0.0.1:9222/devtools/browser/approval"
        requested.set()
        if not cancel:
            raise RuntimeError("User rejected connection")
        await asyncio.Future()

    runtime = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=connect)),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.async_playwright",
        Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime))),
    )
    cdp = LocalCDP(enabled=True, user_data_dir=str(tmp_path))
    task = asyncio.create_task(cdp.connect(1))
    await asyncio.wait_for(requested.wait(), timeout=2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AgentBrowserError) as error:
            await task
        assert error.value.code == "CDP_CONNECTION_FAILED"
        assert "授权" in str(error.value)
    runtime.stop.assert_awaited_once()
    assert cdp.browser is None
    assert cdp._runtime is None


async def test_auto_connect_authorization_timeout_releases_owner(tmp_path, monkeypatch):
    service = BrowserService()
    agent = AgentBrowser(
        service,
        data_dir=tmp_path,
        action_timeout=1,
        cdp_enabled=True,
    )

    async def pending(timeout):
        await asyncio.Future()

    monkeypatch.setattr(agent.cdp, "connect", pending)
    try:
        with pytest.raises(AgentBrowserError) as error:
            await agent.execute(OWNER, "mode", mode="cdp", cdp_authorized=True)
        assert error.value.code == "TIMEOUT"
        assert "授权" in str(error.value)
        assert not agent._sessions
        assert not agent._creating
        assert agent._cdp_owner is None
    finally:
        await agent.close()
        await service.close()


@pytest.fixture
async def cdp_site():
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            body = b'<title>CDP fixture</title><button onclick="this.textContent=\'clicked\'">Click</button><a target="_blank" href="/popup">Popup</a>'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def external_browser(tmp_path, cdp_site):
    async with async_playwright() as runtime:
        bundled = Path(runtime.chromium.executable_path)
        executable = bundled if bundled.is_file() else find_system_chromium()
        if executable is None:
            pytest.skip("Install Chromium to run CDP integration tests")
        profile = tmp_path / "external-profile"
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1600,1000",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            port_file = profile / "DevToolsActivePort"

            async def wait_for_port():
                while not port_file.is_file():
                    if process.returncode is not None:
                        pytest.fail("Disposable CDP browser exited during startup")
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(wait_for_port(), timeout=15)
            port = port_file.read_text().splitlines()[0]
            endpoint = f"http://127.0.0.1:{port}"
            browser = await runtime.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            user_page = context.pages[0]
            await user_page.goto(cdp_site)
            await user_page.evaluate(
                "localStorage.setItem('profile', 'external-login')"
            )
            await context.add_cookies(
                [{"name": "external", "value": "secret", "url": cdp_site}]
            )
            yield SimpleNamespace(
                process=process,
                browser=browser,
                context=context,
                user_page=user_page,
                profile=profile,
                url=cdp_site,
            )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


@pytest.fixture
async def cdp_agent(external_browser, tmp_path):
    service = BrowserService()
    agent = AgentBrowser(
        service,
        cdp_enabled=True,
        cdp_user_data_dir=str(external_browser.profile),
        data_dir=tmp_path / "plugin",
        tool_level="full",
    )
    try:
        yield agent
    finally:
        await agent.close()
        await service.close()


async def execute(agent, action, **args):
    return await agent.execute(OWNER, action, cdp_authorized=True, **args)


@pytest.mark.parametrize("custom_profile", [False, True])
async def test_auto_connect_reuses_running_profile_and_retains_adopted_page(
    external_browser, tmp_path, monkeypatch, custom_profile
):
    external = external_browser
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.chrome_user_data_dir",
        lambda: external.profile,
    )
    profile_bytes = (external.profile / "DevToolsActivePort").read_bytes()
    service = BrowserService()
    agent = AgentBrowser(
        service,
        data_dir=tmp_path / "plugin",
        tool_level="full",
        cdp_enabled=True,
        cdp_user_data_dir=str(external.profile) if custom_profile else "",
        cdp_allow_existing_tabs=True,
    )
    try:
        status = await execute(agent, "mode", mode="status")
        assert status.data["cdp_enabled"] is True
        assert agent.cdp.browser is None
        assert len(external.context.pages) == 1
        await execute(agent, "mode", mode="cdp")
        available = await execute(agent, "tabs", operation="available")
        tab = next(
            tab
            for tab in available.data["tabs"]
            if tab["url"] == external.user_page.url
        )
        await execute(agent, "tabs", operation="attach", tab_id=tab["id"])
        session = agent._sessions[OWNER]
        assert (
            await session.tabs[session.active_tab].evaluate(
                "localStorage.getItem('profile')"
            )
            == "external-login"
        )
        assert any(
            cookie["name"] == "external" for cookie in await session.context.cookies()
        )
        await execute(agent, "close")
        assert external.process.returncode is None
        assert len(external.context.pages) == 1
        assert await external.user_page.title() == "CDP fixture"
        assert (external.profile / "DevToolsActivePort").read_bytes() == profile_bytes
        assert service._browser is None
        # A fresh connection still rediscovers the running instance.
        await execute(agent, "mode", mode="cdp")
        await execute(agent, "close")
    finally:
        await agent.close()
        await service.close()


@pytest.mark.skipif(
    sys.platform != "linux", reason="Native dialog smoke uses isolated Xvfb"
)
async def test_running_chrome_native_authorization_without_debugging_launch_flags(
    tmp_path,
    cdp_site,
):
    xvfb = shutil.which("Xvfb")
    x11_name, xtst_name = (
        ctypes.util.find_library("X11"),
        ctypes.util.find_library("Xtst"),
    )
    if not xvfb or not x11_name or not xtst_name:
        pytest.skip("Native approval test needs Xvfb, X11 and XTest")
    async with async_playwright() as runtime:
        executable = Path(runtime.chromium.executable_path)
    if not executable.is_file():
        pytest.skip("Native approval test needs Playwright's Chrome 144+")
    # This is the preference set by chrome://inspect/#remote-debugging, written
    # only to a disposable profile. Chrome gets no remote-debugging launch flag.
    (tmp_path / "Local State").write_text(
        json.dumps({"devtools": {"remote_debugging": {"user-enabled": True}}})
    )
    display_server = await asyncio.create_subprocess_exec(
        xvfb,
        "-displayfd",
        "1",
        "-screen",
        "0",
        "1280x1024x24",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    process = None
    display = None
    task = None
    service = BrowserService()
    agent = AgentBrowser(
        service,
        data_dir=tmp_path / "plugin",
        cdp_enabled=True,
        cdp_user_data_dir=str(tmp_path),
        cdp_allow_existing_tabs=True,
    )
    cdp = agent.cdp
    x11, xtst = ctypes.CDLL(x11_name), ctypes.CDLL(xtst_name)
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_uint
    x11.XFlush.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    try:
        number = await asyncio.wait_for(display_server.stdout.readline(), timeout=5)
        assert number.strip().isdigit()
        display_name = ":" + number.decode().strip()
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--disable-search-engine-choice-screen",
            "--window-size=1000,800",
            f"--user-data-dir={tmp_path}",
            "about:blank",
            env={**os.environ, "DISPLAY": display_name},
            start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def wait_for_port():
            while not (tmp_path / "DevToolsActivePort").is_file():
                assert process.returncode is None
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait_for_port(), timeout=15)
        display = x11.XOpenDisplay(display_name.encode())
        assert display
        for allowed in (False, True):
            task = asyncio.create_task(cdp.connect(10))
            await asyncio.sleep(1)
            assert not task.done(), "Chrome must wait for approval before connecting"
            # Input can reach only the private test display, never the desktop.
            for sym in (0xFF09, 0xFF0D) if allowed else (0xFF1B,):
                key = x11.XKeysymToKeycode(display, sym)
                xtst.XTestFakeKeyEvent(display, key, 1, 0)
                xtst.XTestFakeKeyEvent(display, key, 0, 0)
                x11.XFlush(display)
                await asyncio.sleep(0.1)
            if not allowed:
                with pytest.raises(AgentBrowserError) as error:
                    await task
                assert error.value.code == "CDP_CONNECTION_FAILED"
                assert cdp._runtime is None
            else:
                browser = await task
                assert browser.contexts[0].pages[0].url == "about:blank"
        # Playwright simulates focus for background pages, so verify the real
        # Chrome window title on this private X display instead of DOM visibility.
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        title_atom = x11.XInternAtom(display, b"_NET_WM_NAME", 0)
        x11.XFree.argtypes = [ctypes.c_void_p]

        async def visible_title(expected):
            async def wait():
                while True:
                    root, parent, count = (
                        ctypes.c_ulong(),
                        ctypes.c_ulong(),
                        ctypes.c_uint(),
                    )
                    children = ctypes.POINTER(ctypes.c_ulong)()
                    assert x11.XQueryTree(
                        display,
                        x11.XDefaultRootWindow(display),
                        ctypes.byref(root),
                        ctypes.byref(parent),
                        ctypes.byref(children),
                        ctypes.byref(count),
                    )
                    try:
                        for i in range(count.value):
                            name = ctypes.c_void_p()
                            actual_type, length, remaining = (
                                ctypes.c_ulong(),
                                ctypes.c_ulong(),
                                ctypes.c_ulong(),
                            )
                            format_bits = ctypes.c_int()
                            x11.XGetWindowProperty(
                                display,
                                children[i],
                                title_atom,
                                0,
                                1024,
                                0,
                                0,
                                ctypes.byref(actual_type),
                                ctypes.byref(format_bits),
                                ctypes.byref(length),
                                ctypes.byref(remaining),
                                ctypes.byref(name),
                            )
                            if name:
                                title = ctypes.string_at(name, length.value).decode(
                                    errors="replace"
                                )
                                x11.XFree(name)
                                if expected in title:
                                    return
                    finally:
                        if children:
                            x11.XFree(children)
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(wait(), timeout=5)

        original = browser.contexts[0].pages[0]
        await original.evaluate("document.title = 'CDP original'")
        await original.bring_to_front()
        await visible_title("CDP original")
        await execute(agent, "mode", mode="cdp")
        session = agent._sessions[OWNER]
        first_id = session.active_tab
        first = agent._page(session)
        await first.evaluate("document.title = 'CDP first'")
        await visible_title("CDP first")
        available = await execute(agent, "tabs", operation="available")
        await execute(
            agent, "tabs", operation="attach", tab_id=available.data["tabs"][0]["id"]
        )
        adopted_id = session.active_tab
        await visible_title("CDP original")
        await execute(agent, "open", url=cdp_site, new_tab=True)
        await visible_title("CDP fixture")
        await execute(agent, "tabs", operation="switch", tab_id=first_id)
        await visible_title("CDP first")
        await execute(agent, "tabs", operation="close", tab_id=first_id)
        assert session.active_tab == adopted_id
        await visible_title("CDP original")
        await execute(agent, "close")
        assert not original.is_closed()
        assert process.returncode is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await agent.close()
        await service.close()
        if process is not None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=10)
        if display:
            x11.XCloseDisplay(display)
        display_server.terminate()
        await asyncio.wait_for(display_server.wait(), timeout=5)


async def test_cdp_reuses_login_and_scopes_pages_network_and_popups(
    cdp_agent, external_browser
):
    agent, external = cdp_agent, external_browser
    await execute(agent, "mode", mode="cdp")
    session = agent._sessions[OWNER]
    assert len(session.tabs) == 1
    assert len(external.context.pages) == 2
    assert next(iter(session.tabs.values())).url == "about:blank"
    assert agent.service._playwright is None
    with pytest.raises(AgentBrowserError) as error:
        await execute(agent, "tabs", operation="available")
    assert error.value.code == "CDP_EXISTING_TABS_DISABLED"
    await execute(agent, "open", url=external.url)
    page = agent._page(session)
    screenshot = await execute(agent, "screenshot")
    assert screenshot.image.startswith(b"\x89PNG")
    assert screenshot.data["viewport"]["width"] > 1280
    await execute(
        agent,
        "hover",
        observation_id=screenshot.data["observation_id"],
        x=screenshot.data["viewport"]["width"] - 10,
        y=10,
    )
    assert await page.evaluate("localStorage.getItem('profile')") == "external-login"
    assert (await session.context.cookies())[0]["name"] == "external"
    await execute(agent, "network", operation="start")
    await external.user_page.goto(external.url + "/private")
    assert not session.network.entries
    await execute(
        agent,
        "intercept",
        operation="add",
        rule={
            "url_pattern": "*",
            "action": "fulfill",
            "content_type": "text/html",
            "body": "<title>Agent only</title>",
        },
    )
    await execute(agent, "open", url=external.url)
    assert await page.title() == "Agent only"
    await external.user_page.reload()
    assert await external.user_page.title() == "CDP fixture"
    await execute(agent, "intercept", operation="clear")
    await execute(agent, "open", url=external.url)
    async with page.expect_popup():
        await page.locator("a").click()
    await asyncio.gather(*list(agent._maintenance))
    assert len(session.tabs) == 2
    assert not agent.storage.state_path(OWNER).exists()
    await execute(agent, "close")
    assert external.browser.is_connected()
    assert not external.user_page.is_closed()
    assert len(external.context.pages) == 1


@pytest.mark.parametrize(
    "ending", ["close", "idle", "shutdown", "last_tab", "disconnect"]
)
async def test_cdp_lifecycle_never_closes_external_context_or_saves_profile(
    cdp_agent, external_browser, monkeypatch, ending
):
    agent = cdp_agent
    await execute(agent, "mode", mode="cdp")
    session = agent._sessions[OWNER]
    state = AsyncMock(
        side_effect=AssertionError("External profile must not be exported")
    )
    monkeypatch.setattr(session.context, "storage_state", state)
    monkeypatch.setattr(
        session.context,
        "close",
        AsyncMock(side_effect=AssertionError("Do not close external context")),
    )
    monkeypatch.setattr(
        session.context.browser,
        "close",
        AsyncMock(side_effect=AssertionError("Do not close external browser")),
    )
    if ending == "close":
        await execute(agent, "close")
    elif ending == "idle":
        session.last_used -= 10000
        await agent.reap_idle()
    elif ending == "shutdown":
        await agent.close()
    elif ending == "last_tab":
        await agent._page(session).close()
        if agent._maintenance:
            await asyncio.gather(*list(agent._maintenance))
    else:
        await agent.cdp.disconnect()
        if agent._maintenance:
            await asyncio.gather(*list(agent._maintenance))
        if OWNER in agent._sessions:
            with pytest.raises(AgentBrowserError) as error:
                await execute(agent, "observe")
            assert error.value.code == "SESSION_LOST"
    assert OWNER not in agent._sessions
    assert agent._cdp_owner is None
    assert agent.cdp.browser is None
    assert external_browser.browser.is_connected()
    assert not external_browser.user_page.is_closed()
    state.assert_not_awaited()


async def test_cdp_rejects_other_chat_and_rechecks_authorization_after_owner_lock(
    cdp_agent,
):
    agent = cdp_agent
    async with agent._owner_lock(OWNER):
        enter = asyncio.create_task(execute(agent, "mode", mode="cdp"))
        await asyncio.sleep(0)
        unauthorized = asyncio.create_task(agent.execute(OWNER, "observe"))
        await asyncio.sleep(0)
    await enter
    with pytest.raises(AgentBrowserError) as error:
        await unauthorized
    assert error.value.code == "CDP_PERMISSION_DENIED"
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OTHER, "mode", mode="cdp", cdp_authorized=True)
    assert error.value.code == "CDP_BUSY"


async def test_cdp_existing_tab_is_explicit_and_retained_on_detach(
    cdp_agent, external_browser
):
    agent = cdp_agent
    agent.cdp.allow_existing_tabs = True
    await execute(agent, "mode", mode="cdp")
    listed = await execute(agent, "tabs", operation="available")
    assert len(listed.data["tabs"]) == 1
    tab_id = listed.data["tabs"][0]["id"]
    assert tab_id not in agent._sessions[OWNER].tabs
    attached = await execute(agent, "tabs", operation="attach", tab_id=tab_id)
    assert attached.data["title"] == "CDP fixture"
    await execute(
        agent,
        "intercept",
        operation="add",
        rule={"url_pattern": "*", "action": "abort"},
    )
    await execute(agent, "close")
    assert not external_browser.user_page.is_closed()
    await external_browser.user_page.reload()
    assert await external_browser.user_page.title() == "CDP fixture"
    assert len(external_browser.context.pages) == 1


async def test_cdp_mode_switches_do_not_transfer_external_profile(
    cdp_agent, external_browser, monkeypatch
):
    agent = cdp_agent
    await execute(agent, "open", url=external_browser.url)
    await agent._page(agent._sessions[OWNER]).evaluate(
        "localStorage.setItem('profile', 'plugin-login')"
    )
    await execute(agent, "mode", mode="cdp")
    await execute(agent, "open", url=external_browser.url)
    external_session = agent._sessions[OWNER]
    assert (
        await agent._page(external_session).evaluate("localStorage.getItem('profile')")
        == "external-login"
    )
    for action, args in [
        ("state", {"operation": "save"}),
        ("state", {"operation": "restore"}),
        ("state", {"operation": "clear"}),
        ("emulate", {"operation": "reset"}),
    ]:
        with pytest.raises(AgentBrowserError) as error:
            await execute(agent, action, **args)
        assert error.value.code == "CDP_UNSUPPORTED"
    snapshot = agent.storage.state_path(OWNER).read_bytes()
    state = AsyncMock(side_effect=AssertionError("Must not migrate CDP state"))
    monkeypatch.setattr(external_session.context, "storage_state", state)
    await execute(agent, "mode", mode="headless")
    await execute(agent, "open", url=external_browser.url)
    assert (
        await agent._page(agent._sessions[OWNER]).evaluate(
            "localStorage.getItem('profile')"
        )
        == "plugin-login"
    )
    assert agent.storage.state_path(OWNER).read_bytes() == snapshot
    state.assert_not_awaited()
    assert external_browser.browser.is_connected()
    png = await agent.service.render_html(
        "<main>Shared rendering is independent</main>"
    )
    assert png.startswith(b"\x89PNG")


@pytest.mark.parametrize("failure", ["connect", "cancel_setup"])
async def test_failed_or_cancelled_cdp_entry_retains_previous_session(
    cdp_agent, external_browser, monkeypatch, failure
):
    agent = cdp_agent
    await execute(agent, "open", url=external_browser.url)
    previous = agent._sessions[OWNER]
    if failure == "connect":
        monkeypatch.setattr(
            agent.cdp,
            "connect",
            AsyncMock(
                side_effect=AgentBrowserError("CDP_CONNECTION_FAILED", "unreachable")
            ),
        )
        error_type = AgentBrowserError
    else:
        new_page = agent._new_page

        async def cancel_after_setup(session):
            await new_page(session)
            raise asyncio.CancelledError

        monkeypatch.setattr(agent, "_new_page", cancel_after_setup)
        error_type = asyncio.CancelledError
    with pytest.raises(error_type):
        await execute(agent, "mode", mode="cdp")
    assert agent._sessions[OWNER] is previous
    assert not agent._page(previous).is_closed()
    assert agent._cdp_owner is None
    assert agent.cdp.browser is None
    assert not agent._creating
    assert len(external_browser.context.pages) == 1


async def test_external_browser_exit_releases_slot_without_automatic_reconnect(
    cdp_agent, external_browser, monkeypatch
):
    agent = cdp_agent
    await execute(agent, "mode", mode="cdp")
    cleaned = asyncio.Event()
    drop = agent._drop

    async def notify_cleanup(owner, session, **kwargs):
        result = await drop(owner, session, **kwargs)
        if session.mode == "cdp":
            cleaned.set()
        return result

    monkeypatch.setattr(agent, "_drop", notify_cleanup)
    external_browser.process.terminate()
    await asyncio.wait_for(external_browser.process.wait(), timeout=5)
    await asyncio.wait_for(cleaned.wait(), timeout=5)
    await asyncio.gather(*list(agent._maintenance))
    assert OWNER not in agent._sessions
    assert agent._cdp_owner is None
    assert agent.cdp.browser is None
    with pytest.raises(AgentBrowserError) as error:
        await execute(agent, "observe")
    assert error.value.code == "NO_SESSION"


async def test_cancel_during_external_page_creation_cleans_the_late_page(
    cdp_agent, external_browser, monkeypatch
):
    agent = cdp_agent
    created, release = asyncio.Event(), asyncio.Event()
    connect = agent.cdp.connect

    async def connect_with_delayed_page(timeout):
        browser = await connect(timeout)
        context = browser.contexts[0]
        new_page = context.new_page

        async def delayed_page():
            page = await new_page()
            created.set()
            await release.wait()
            return page

        monkeypatch.setattr(context, "new_page", delayed_page)
        return browser

    monkeypatch.setattr(agent.cdp, "connect", connect_with_delayed_page)
    task = asyncio.create_task(execute(agent, "mode", mode="cdp"))
    await asyncio.wait_for(created.wait(), timeout=5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(external_browser.context.pages) == 1
    assert agent._cdp_owner is None
    assert not agent._sessions


@pytest.mark.parametrize("enabled", [True, False])
async def test_cdp_status_is_lazy_and_disabled_entry_never_connects(
    tmp_path, monkeypatch, enabled
):
    discover = Mock(side_effect=AssertionError("Must not read the user's profile"))
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._discover_running_chrome", discover
    )
    agent = AgentBrowser(BrowserService(), data_dir=tmp_path, cdp_enabled=enabled)
    try:
        result = await agent.execute(OWNER, "mode")
        assert result.data["cdp_enabled"] is enabled
        assert agent.cdp._runtime is None
        assert agent.service._playwright is None
        with pytest.raises(AgentBrowserError) as error:
            await agent.execute(OWNER, "mode", mode="cdp")
        assert error.value.code == (
            "CDP_PERMISSION_DENIED" if enabled else "CDP_DISABLED"
        )
        discover.assert_not_called()
        assert agent.cdp._runtime is None
    finally:
        await agent.close()
