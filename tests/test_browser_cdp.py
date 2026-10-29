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
    _complete_websocket,
    _discover_running_browser,
    _local_endpoint,
    _resolve_implicit_endpoint,
    browser_user_data_dirs,
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
    "endpoint",
    [
        None,
        123,
        "example.com:9222",
        "192.168.1.2:9222",
        "127.0.0.1:65536",
        "localhost:9222/devtools/page/id",
        "file:///tmp/browser",
        "http://example.com:9222",
        "http://192.168.1.2:9222",
        "http://0.0.0.0:9222",
        "http://[::]:9222",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://user:password@localhost:9222",
        "http://localhost:9222/?token=1",
        "http://localhost:9222/#fragment",
        "http://localhost:9222/json/list",
        "http://local\nhost:9222",
        "http://localhost:9222/\x00",
        "ws://localhost:9222/devtools/browser/id/extra",
        "ws://localhost:9222/devtools/page/id",
    ],
)
async def test_manual_endpoint_rejects_invalid_or_nonlocal_addresses_at_connection(
    endpoint, monkeypatch
):
    start = Mock(side_effect=AssertionError("Invalid address must not start a driver"))
    discover = Mock(side_effect=AssertionError("Invalid address must not fall back"))
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._discover_running_browser", discover
    )
    cdp = LocalCDP(enabled=True, endpoint=endpoint)
    with pytest.raises(AgentBrowserError, match="browser_use.cdp.endpoint") as error:
        await cdp.connect(1)
    assert error.value.code == "CDP_INVALID_ENDPOINT"
    assert cdp.browser is None
    assert cdp._runtime is None
    start.assert_not_called()
    discover.assert_not_called()


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("   ", ""),
        ("127.0.0.1:41063/", "auto://127.0.0.1:41063"),
        (
            "localhost:41063/devtools/browser/id",
            "ws://localhost:41063/devtools/browser/id",
        ),
        ("[::1]:41063/devtools/browser/", "ws://[::1]:41063/devtools/browser/"),
        ("ws://localhost:41063", "ws://localhost:41063"),
        ("ws://localhost:41063/", "ws://localhost:41063/"),
        (
            "ws://localhost:41063/devtools/browser/",
            "ws://localhost:41063/devtools/browser/",
        ),
        ("127.0.0.1:41063", "auto://127.0.0.1:41063"),
        (" localhost:41063 ", "auto://localhost:41063"),
        ("[::1]:41063", "auto://[::1]:41063"),
        ("localhost:41063/json/version", "http://localhost:41063"),
        (" http://localhost:41063/json/version/ ", "http://localhost:41063"),
        ("https://127.0.0.1:9222/", "https://127.0.0.1:9222"),
        ("http://[::1]:9222", "http://[::1]:9222"),
        (
            "ws://127.0.0.1:41063/devtools/browser/id",
            "ws://127.0.0.1:41063/devtools/browser/id",
        ),
        (
            "wss://[::1]:9222/devtools/browser/id",
            "wss://[::1]:9222/devtools/browser/id",
        ),
    ],
)
def test_manual_endpoint_accepts_local_http_and_browser_websocket(endpoint, expected):
    assert _local_endpoint(endpoint) == expected


async def test_manual_endpoint_failure_never_falls_back_to_discovery(
    tmp_path, monkeypatch
):
    discover = Mock(side_effect=AssertionError("Do not discover a different browser"))
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._discover_running_browser", discover
    )
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(
            connect_over_cdp=AsyncMock(side_effect=RuntimeError("Connection rejected"))
        ),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.async_playwright",
        Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime))),
    )
    cdp = LocalCDP(
        enabled=True,
        endpoint="ws://127.0.0.1:41063/devtools/browser/id",
        user_data_dir=str(tmp_path),
    )
    with pytest.raises(AgentBrowserError) as error:
        await cdp.connect(1)
    assert error.value.code == "CDP_CONNECTION_FAILED"
    discover.assert_not_called()
    runtime.stop.assert_awaited_once()
    assert cdp.browser is None


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("127.0.0.1:41063", "ws://127.0.0.1:41063/devtools/browser/target"),
        ("localhost:41063/", "ws://localhost:41063/devtools/browser/target"),
        ("ws://localhost:41063", "ws://localhost:41063/devtools/browser/target"),
        ("ws://127.0.0.1:41063/", "ws://127.0.0.1:41063/devtools/browser/target"),
        (
            "ws://127.0.0.1:41063/devtools/browser",
            "ws://127.0.0.1:41063/devtools/browser/target",
        ),
        (
            "ws://127.0.0.1:41063/devtools/browser/",
            "ws://127.0.0.1:41063/devtools/browser/target",
        ),
        ("wss://[::1]:41063", "wss://[::1]:41063/devtools/browser/target"),
    ],
)
async def test_short_websocket_reads_only_path_matching_requested_port(
    tmp_path, monkeypatch, endpoint, expected
):
    profiles = (tmp_path / "chrome", tmp_path / "edge", tmp_path / "chromium")
    for profile, payload in zip(
        profiles,
        (
            "9222\n/devtools/browser/other\n",
            "41063\n/devtools/browser/target\n",
            "invalid",
        ),
    ):
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(payload)
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs", lambda: profiles
    )
    resolve = Mock(
        side_effect=AssertionError("A matching file must skip HTTP discovery")
    )
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp._resolve_endpoint", resolve)
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(
            connect_over_cdp=AsyncMock(return_value=Mock(contexts=[Mock()]))
        ),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.async_playwright",
        Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime))),
    )
    cdp = LocalCDP(enabled=True, endpoint=endpoint)
    try:
        await cdp.connect(1)
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            expected, timeout=1000
        )
        resolve.assert_not_called()
    finally:
        await cdp.disconnect()


@pytest.mark.parametrize("profile_selection", ["default", "missing", "other"])
async def test_short_websocket_never_uses_unmatched_port_or_ignores_selected_directory(
    tmp_path, monkeypatch, profile_selection
):
    other = tmp_path / "other"
    other.mkdir()
    (other / "DevToolsActivePort").write_text("9222\n/devtools/browser/other\n")
    target = tmp_path / "target"
    target.mkdir()
    (target / "DevToolsActivePort").write_text("41063\n/devtools/browser/target\n")
    profiles = (other,) if profile_selection == "default" else (other, target)
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs", lambda: profiles
    )
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    cdp = LocalCDP(
        enabled=True,
        endpoint="ws://127.0.0.1:41063",
        user_data_dir=""
        if profile_selection == "default"
        else str(tmp_path / profile_selection),
    )
    with pytest.raises(AgentBrowserError) as error:
        await cdp.connect(1)
    assert error.value.code == "CDP_DISCOVERY_FAILED"
    assert "41063" in str(error.value)
    start.assert_not_called()


async def test_short_websocket_ambiguous_paths_require_selected_directory(
    tmp_path, monkeypatch
):
    profiles = (tmp_path / "chrome", tmp_path / "edge")
    for profile in profiles:
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            f"41063\n/devtools/browser/{profile.name}\n"
        )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs", lambda: profiles
    )
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    with pytest.raises(AgentBrowserError) as error:
        await LocalCDP(enabled=True, endpoint="ws://127.0.0.1:41063").connect(1)
    assert error.value.code == "CDP_MULTIPLE_BROWSERS"
    start.assert_not_called()
    assert (
        _complete_websocket("ws://localhost:41063", profiles[1])
        == "ws://localhost:41063/devtools/browser/edge"
    )
    http = Mock(side_effect=AssertionError("Ambiguous matches must not try HTTP"))
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp._resolve_endpoint", http)
    with pytest.raises(AgentBrowserError) as error:
        _resolve_implicit_endpoint("auto://localhost:41063", None, 1)
    assert error.value.code == "CDP_MULTIPLE_BROWSERS"
    http.assert_not_called()


@pytest.mark.parametrize("returned_port", [41063, 9222])
def test_implicit_address_without_matching_file_uses_http_on_same_port(
    tmp_path, monkeypatch, returned_port
):
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs",
        lambda: (tmp_path / "missing",),
    )
    http = Mock(return_value=f"ws://127.0.0.1:{returned_port}/devtools/browser/legacy")
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp._resolve_endpoint", http)
    if returned_port == 41063:
        assert (
            _resolve_implicit_endpoint("auto://localhost:41063", None, 1)
            == "ws://localhost:41063/devtools/browser/legacy"
        )
    else:
        with pytest.raises(ValueError, match="different debugging port"):
            _resolve_implicit_endpoint("auto://localhost:41063", None, 1)
    http.assert_called_once_with("http://localhost:41063", 1)


@pytest.mark.parametrize(
    "relative",
    [
        "snap/chromium/common/chromium",
        ".var/app/org.chromium.Chromium/config/chromium",
        ".var/app/com.microsoft.Edge/config/microsoft-edge",
        ".var/app/com.google.Chrome/config/google-chrome",
    ],
)
def test_short_websocket_finds_snap_and_flatpak_profiles(
    tmp_path, monkeypatch, relative
):
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    profile = tmp_path / relative
    profile.mkdir(parents=True)
    (profile / "DevToolsActivePort").write_text("41063\n/devtools/browser/packaged\n")
    assert (
        _complete_websocket("ws://127.0.0.1:41063", None)
        == "ws://127.0.0.1:41063/devtools/browser/packaged"
    )


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (302, b""),
        (200, b"not-json"),
        (200, b"{}"),
        (200, b"[]"),
        (200, b"x" * 65537),
        (200, b'{"webSocketDebuggerUrl":"ws://example.com:9222/devtools/browser/id"}'),
        (200, b'{"webSocketDebuggerUrl":"http://127.0.0.1:9222"}'),
        (200, b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222"}'),
    ],
)
async def test_manual_http_discovery_rejects_redirects_or_invalid_metadata(
    monkeypatch, status, payload
):
    requests = []

    async def respond(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                f"HTTP/1.1 {status} Test\r\nContent-Length: {len(payload)}\r\nLocation: http://example.com/\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        with pytest.raises(AgentBrowserError) as error:
            await LocalCDP(enabled=True, endpoint=endpoint).connect(1)
        assert error.value.code == "CDP_CONNECTION_FAILED"
        assert len(requests) == 1
        assert requests[0].startswith(b"GET /json/version HTTP/1.1\r\n")
        start.assert_not_called()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("platform", "environment", "expected"),
    [
        (
            "linux",
            {},
            [
                "/account/.config/google-chrome",
                "/account/.config/microsoft-edge",
                "/account/.config/chromium",
            ],
        ),
        (
            "linux",
            {"XDG_CONFIG_HOME": "/xdg"},
            ["/xdg/google-chrome", "/xdg/microsoft-edge", "/xdg/chromium"],
        ),
        (
            "linux",
            {"CHROME_CONFIG_HOME": "/chrome", "XDG_CONFIG_HOME": "/xdg"},
            ["/chrome/google-chrome", "/xdg/microsoft-edge", "/xdg/chromium"],
        ),
        (
            "darwin",
            {},
            [
                "/account/Library/Application Support/Google/Chrome",
                "/account/Library/Application Support/Microsoft Edge",
                "/account/Library/Application Support/Chromium",
            ],
        ),
        (
            "win32",
            {"LOCALAPPDATA": "/local"},
            [
                "/local/Google/Chrome/User Data",
                "/local/Microsoft/Edge/User Data",
                "/local/Chromium/User Data",
            ],
        ),
    ],
)
def test_browser_default_profile_locations(
    monkeypatch, platform, environment, expected
):
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.sys.platform", platform)
    monkeypatch.setattr(Path, "home", lambda: Path("/account"))
    for key in ("XDG_CONFIG_HOME", "CHROME_CONFIG_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    if platform == "linux":
        expected += [
            "/account/snap/chromium/common/chromium",
            "/account/.var/app/org.chromium.Chromium/config/chromium",
            "/account/.var/app/com.microsoft.Edge/config/microsoft-edge",
            "/account/.var/app/com.google.Chrome/config/google-chrome",
        ]
    assert browser_user_data_dirs() == tuple(Path(path) for path in expected)


@pytest.mark.parametrize("browser", ["google-chrome", "microsoft-edge", "chromium"])
async def test_default_discovery_connects_only_matching_browser(
    tmp_path, monkeypatch, browser
):
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CHROME_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = tmp_path / browser
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/target\n")
    # A corrupt unrelated profile must not hide the target.
    for other in {"google-chrome", "microsoft-edge", "chromium"} - {browser}:
        root = tmp_path / other
        root.mkdir()
        (root / "DevToolsActivePort").write_bytes(b"invalid")
    connected_browser = Mock(contexts=[Mock()])
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(
            connect_over_cdp=AsyncMock(return_value=connected_browser)
        ),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.async_playwright",
        Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime))),
    )
    cdp = LocalCDP(enabled=True)
    try:
        assert await cdp.connect(2) is connected_browser
        runtime.chromium.connect_over_cdp.assert_awaited_once_with(
            "ws://127.0.0.1:9222/devtools/browser/target", timeout=2000
        )
    finally:
        await cdp.disconnect()


async def test_multiple_browser_profiles_require_explicit_directory(
    tmp_path, monkeypatch
):
    profiles = (tmp_path / "chrome", tmp_path / "edge")
    for port, profile in enumerate(profiles, start=9222):
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            f"{port}\n/devtools/browser/target\n"
        )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs", lambda: profiles
    )
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    with pytest.raises(AgentBrowserError) as error:
        await LocalCDP(enabled=True).connect(1)
    assert error.value.code == "CDP_MULTIPLE_BROWSERS"
    for profile in profiles:
        assert str(profile) in str(error.value)
    start.assert_not_called()
    assert (
        _discover_running_browser(profiles[1])
        == "ws://127.0.0.1:9223/devtools/browser/target"
    )


async def test_default_discovery_missing_profiles_never_creates_directories(
    tmp_path, monkeypatch
):
    profiles = (tmp_path / "chrome", tmp_path / "edge", tmp_path / "chromium")
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs", lambda: profiles
    )
    start = Mock()
    monkeypatch.setattr("astrbot_plugin_browser.browser_cdp.async_playwright", start)
    with pytest.raises(AgentBrowserError) as error:
        await LocalCDP(enabled=True).connect(1)
    assert error.value.code == "CDP_DISCOVERY_FAILED"
    assert not any(profile.exists() for profile in profiles)
    start.assert_not_called()


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
        _discover_running_browser(tmp_path)
        == "ws://127.0.0.1:9222/devtools/browser/first"
    )
    port_file.write_text("9333\n/devtools/browser/second\n")
    assert (
        _discover_running_browser(tmp_path)
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


@pytest.mark.parametrize(
    "connection", ["default", "directory", "http", "bare", "ws", "short_ws"]
)
async def test_auto_connect_reuses_running_profile_and_retains_adopted_page(
    external_browser, tmp_path, monkeypatch, connection
):
    external = external_browser
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp.browser_user_data_dirs",
        lambda: (tmp_path / "missing", external.profile),
    )
    profile_bytes = (external.profile / "DevToolsActivePort").read_bytes()
    endpoint = ""
    if connection == "short_ws":
        endpoint = "ws://127.0.0.1:" + profile_bytes.decode().splitlines()[0]
    if connection in {"http", "bare", "ws"}:
        endpoint = _discover_running_browser(external.profile)
        if connection in {"http", "bare"}:
            endpoint = "http://127.0.0.1:" + profile_bytes.decode().splitlines()[0]
            if connection == "bare":
                endpoint = endpoint.removeprefix("http://")
        monkeypatch.setattr(
            "astrbot_plugin_browser.browser_cdp._discover_running_browser",
            Mock(side_effect=AssertionError("Explicit address must skip discovery")),
        )
    service = BrowserService()
    agent = AgentBrowser(
        service,
        data_dir=tmp_path / "plugin",
        tool_level="full",
        cdp_enabled=True,
        cdp_endpoint=endpoint,
        cdp_user_data_dir=(
            str(external.profile)
            if connection == "directory"
            else str(tmp_path / "missing")
            if endpoint and connection != "short_ws"
            else ""
        ),
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
@pytest.mark.parametrize("browser_kind", ["chrome", "edge"])
@pytest.mark.parametrize("connection", ["directory", "ws", "short_ws", "bare"])
async def test_running_browser_native_authorization_without_debugging_launch_flags(
    tmp_path,
    cdp_site,
    browser_kind,
    connection,
):
    xvfb = shutil.which("Xvfb")
    x11_name, xtst_name = (
        ctypes.util.find_library("X11"),
        ctypes.util.find_library("Xtst"),
    )
    if not xvfb or not x11_name or not xtst_name:
        pytest.skip("Native approval test needs Xvfb, X11 and XTest")
    if browser_kind == "edge":
        edge = shutil.which("microsoft-edge")
        if not edge:
            pytest.skip("Native Edge approval test needs Microsoft Edge")
        executable = Path(edge)
    else:
        async with async_playwright() as runtime:
            executable = Path(runtime.chromium.executable_path)
    if not executable.is_file():
        pytest.skip("Native approval test needs Playwright's Chrome 144+")
    # The inspect page preference is written only to a disposable profile.
    # The browser gets no remote-debugging launch flag.
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
        if connection == "ws":
            if browser_kind == "chrome":
                port = (tmp_path / "DevToolsActivePort").read_text().splitlines()[0]
                cdp.endpoint = f"http://127.0.0.1:{port}"
                # Native session sharing can omit the legacy HTTP discovery API.
                with pytest.raises(AgentBrowserError) as error:
                    await cdp.connect(2)
                assert error.value.code == "CDP_CONNECTION_FAILED"
                assert "/json/version" in str(error.value)
                assert cdp._runtime is None
            cdp.endpoint = _discover_running_browser(tmp_path)
        elif connection in {"short_ws", "bare"}:
            port = (tmp_path / "DevToolsActivePort").read_text().splitlines()[0]
            cdp.endpoint = f"ws://127.0.0.1:{port}"
            if connection == "bare":
                cdp.endpoint = cdp.endpoint.removeprefix("ws://")
        display = x11.XOpenDisplay(display_name.encode())
        assert display
        for allowed in (False, True):
            task = asyncio.create_task(cdp.connect(10))
            await asyncio.sleep(1)
            assert not task.done(), "Browser must wait for approval before connecting"
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


@pytest.mark.parametrize("failure", ["connect", "cancel_setup", "invalid_endpoint"])
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
    elif failure == "invalid_endpoint":
        agent.cdp.endpoint = "ws://127.0.0.1:41063/devtools/page/id"
        error_type = AgentBrowserError
    else:
        new_page = agent._new_page

        async def cancel_after_setup(session):
            await new_page(session)
            raise asyncio.CancelledError

        monkeypatch.setattr(agent, "_new_page", cancel_after_setup)
        error_type = asyncio.CancelledError
    with pytest.raises(error_type) as error:
        await execute(agent, "mode", mode="cdp")
    assert agent._sessions[OWNER] is previous
    assert not agent._page(previous).is_closed()
    assert agent._cdp_owner is None
    assert agent.cdp.browser is None
    assert not agent._creating
    assert len(external_browser.context.pages) == 1
    if failure == "invalid_endpoint":
        assert error.value.code == "CDP_INVALID_ENDPOINT"
        assert (await execute(agent, "observe")).data["success"]
        png = await agent.service.render_html("<main>Rendering still works</main>")
        assert png.startswith(b"\x89PNG")


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
@pytest.mark.parametrize(
    "endpoint",
    ["", "http://127.0.0.1:41063", "ws://127.0.0.1:41063", "127.0.0.1:41063"],
)
async def test_cdp_status_is_lazy_and_disabled_entry_never_connects(
    tmp_path, monkeypatch, enabled, endpoint
):
    discover = Mock(side_effect=AssertionError("Must not read the user's profile"))
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._discover_running_browser", discover
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._resolve_endpoint", discover
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._complete_websocket", discover
    )
    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_cdp._resolve_implicit_endpoint", discover
    )
    agent = AgentBrowser(
        BrowserService(), data_dir=tmp_path, cdp_enabled=enabled, cdp_endpoint=endpoint
    )
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
