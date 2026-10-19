"""Check private file quotas, atomic login-state writes and file cleanup."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_browser.agent_browser import (
    AgentBrowser,
    AgentBrowserError,
    AgentSession,
)
from astrbot_plugin_browser.browser_storage import BrowserStorage
from astrbot_plugin_browser.service import BrowserService
from playwright.async_api import Error as PlaywrightError

OWNER = ("test:group:one", "alice")


async def test_import_quota_failure_leaves_no_partial_file_and_keeps_previous(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("astrbot_plugin_browser.browser_storage.MAX_FILE_BYTES", 8)
    monkeypatch.setattr("astrbot_plugin_browser.browser_storage.MAX_OWNER_BYTES", 10)
    storage = BrowserStorage(tmp_path)
    source = tmp_path / "input"
    source.write_bytes(b"1234")
    try:
        first = await storage.import_file(OWNER, source, "../foo\\bar.txt", "first")
        assert "/" not in first.name and "\\" not in first.name
        source.write_bytes(b"1234567")
        with pytest.raises(AgentBrowserError) as error:
            await storage.import_file(OWNER, source, "second", "second")
        assert error.value.code == "FILE_LIMIT"
        assert list(storage.files[OWNER]) == [first.id]
        assert first.path.read_bytes() == b"1234"
        assert list(first.path.parent.iterdir()) == [first.path]
    finally:
        storage.close()


async def test_cancelled_attachment_import_removes_partial_file(tmp_path, monkeypatch):
    storage = BrowserStorage(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"large enough to yield")

    async def cancel_copy(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "astrbot_plugin_browser.browser_storage.asyncio.sleep", cancel_copy
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await storage.import_file(OWNER, source, "source.txt", "key")
        assert storage.files[OWNER] == {}
        assert source.exists()
    finally:
        storage.close()


@pytest.mark.parametrize("failure", ["network", "quota", "cancel"])
async def test_failed_download_cancels_and_marks_file_without_partial_data(
    tmp_path, monkeypatch, failure
):
    agent = AgentBrowser(BrowserService(), data_dir=tmp_path)
    session = AgentSession(context=None, owner=OWNER)
    monkeypatch.setattr("astrbot_plugin_browser.browser_storage.MAX_FILE_BYTES", 4)

    async def save(path):
        Path(path).write_bytes(b"too large")
        if failure == "network":
            raise PlaywrightError("network error")
        if failure == "cancel":
            raise asyncio.CancelledError()

    download = SimpleNamespace(
        suggested_filename="report.txt",
        save_as=save,
        cancel=AsyncMock(),
        delete=AsyncMock(),
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await agent._save_download(session, download)
    else:
        await agent._save_download(session, download)
    item = next(iter(agent.storage.files[OWNER].values()))
    assert item.status == "failed"
    assert item.done.is_set()
    assert not item.path.exists()
    download.cancel.assert_awaited_once()
    download.delete.assert_awaited_once()
    await agent.close()


async def test_saved_state_atomic_replacement_and_size_limit(tmp_path, monkeypatch):
    storage = BrowserStorage(tmp_path)
    state = {"cookies": [], "origins": []}
    storage.write_state(OWNER, state)
    monkeypatch.setattr("astrbot_plugin_browser.browser_storage.MAX_STATE_BYTES", 5)
    with pytest.raises(AgentBrowserError) as error:
        storage.write_state(OWNER, {"cookies": [], "origins": [{"origin": "example"}]})
    assert error.value.code == "STATE_LIMIT"
    monkeypatch.setattr("astrbot_plugin_browser.browser_storage.MAX_STATE_BYTES", 1000)
    assert storage.read_state(OWNER) == state
    assert not list(tmp_path.rglob("*.tmp"))
    storage.close()
    assert storage.state_path(OWNER).is_file()


async def test_file_and_state_metadata_do_not_require_a_browser(tmp_path):
    agent = AgentBrowser(BrowserService(), data_dir=tmp_path, tool_level="standard")
    try:
        assert (await agent.execute(OWNER, "download")).data["files"] == []
        assert not (await agent.execute(OWNER, "state")).data["saved"]
        await agent.execute(OWNER, "state", operation="clear")
        assert agent.service._browser is None
    finally:
        await agent.close()
