from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_browser.main import BrowserPlugin
from astrbot_plugin_browser.service import BrowserServiceError


def test_plugin_owns_one_configured_service():
    plugin = BrowserPlugin(
        SimpleNamespace(),
        {
            "browser_executable": "",
            "max_concurrent_sessions": 3,
            "queue_timeout": 20,
            "startup_timeout": 15,
        },
    )

    assert plugin.service.max_concurrent_sessions == 3
    assert plugin.service.queue_timeout == 20
    assert plugin.service.startup_timeout == 15


def test_plugin_rejects_unknown_settings():
    with pytest.raises(BrowserServiceError, match="未知插件配置"):
        BrowserPlugin(SimpleNamespace(), {"unrecognized": True})


async def test_plugin_lifecycle_delegates_to_service():
    plugin = BrowserPlugin(SimpleNamespace(), {})
    plugin.service.initialize = AsyncMock()
    plugin.service.close = AsyncMock()

    await plugin.initialize()
    await plugin.terminate()

    plugin.service.initialize.assert_awaited_once()
    plugin.service.close.assert_awaited_once()
