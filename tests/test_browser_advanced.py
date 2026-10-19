"""Check optional browser features' limits and validation without network I/O."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_browser.agent_browser import AgentBrowser
from astrbot_plugin_browser.browser_advanced import (
    MAX_BODY,
    Interceptor,
    NetworkMonitor,
    environment,
    validate_rule,
)
from astrbot_plugin_browser.browser_errors import AgentBrowserError
from astrbot_plugin_browser.browser_tools import TOOL_LEVELS
from astrbot_plugin_browser.service import BrowserService

OWNER = ("test:group:one", "alice")


@pytest.mark.parametrize(
    ("level", "action"),
    [
        (level, action)
        for level in ("basic", "standard")
        for action in sorted(TOOL_LEVELS["full"] - TOOL_LEVELS[level])
    ],
)
async def test_unavailable_actions_do_not_access_browser(level, action):
    agent = AgentBrowser(BrowserService(), tool_level=level)
    agent._execute = AsyncMock()
    with pytest.raises(AgentBrowserError) as error:
        await agent.execute(OWNER, action)
    assert error.value.code == "TOOL_LEVEL_REQUIRED"
    agent._execute.assert_not_awaited()
    assert agent.service._browser is None
    await agent.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"viewport": {"width": 0, "height": 720}},
        {"viewport": {"width": 8192, "height": 8192}},
        {"viewport": {"width": 390, "height": 844, "old_field": 1}},
        {"is_mobile": "true"},
        {"device_scale_factor": float("nan")},
        {"geolocation": {"latitude": 91, "longitude": 121}},
        {"geolocation": {"latitude": True, "longitude": 121}},
        {"locale": "zh_CN"},
        {"javascript_enabled": False},
        {"timezone_id": "Asia/Shanghai\n"},
    ],
)
def test_environment_rejects_invalid_and_unknown_settings(settings):
    with pytest.raises(AgentBrowserError) as error:
        environment(settings)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "rule",
    [
        {
            "url_pattern": "*",
            "action": "modify",
            "headers": {"X-Test": "ok\r\ninjected: yes"},
        },
        {"url_pattern": "*", "action": "modify", "headers": {"Cookie": "secret=1"}},
        {"url_pattern": "*", "action": "modify", "url": "file:///secret"},
        {
            "url_pattern": "*",
            "action": "modify",
            "url": "https://user:pass@example.com",
        },
        {"url_pattern": "*", "action": "modify"},
        {"url_pattern": "*", "action": "fulfill", "body": "x" * (MAX_BODY + 1)},
        {"url_pattern": "*", "action": "fulfill", "status": True},
        {"url_pattern": "*", "action": "abort", "headers": {}},
        {"url_pattern": "*", "action": "abort", "legacy_alias": True},
    ],
)
def test_interception_rejects_unsafe_protocols_invalid_fields_and_oversized_bodies(
    rule,
):
    with pytest.raises(AgentBrowserError) as error:
        validate_rule(rule)
    assert error.value.code == "INVALID_ARGUMENT"


def test_interceptor_quota_removal_and_strict_arguments():
    interceptor = Interceptor()
    for index in range(20):
        interceptor.operate(
            {
                "operation": "add",
                "rule": {"url_pattern": f"*/{index}", "action": "abort"},
            }
        )
    with pytest.raises(AgentBrowserError) as error:
        interceptor.operate(
            {"operation": "add", "rule": {"url_pattern": "*", "action": "abort"}}
        )
    assert error.value.code == "RULE_LIMIT"
    first_id = next(iter(interceptor.rules))
    interceptor.operate({"operation": "remove", "rule_id": first_id})
    assert len(interceptor.rules) == 19
    with pytest.raises(AgentBrowserError) as error:
        interceptor.operate({"operation": "list", "rule": {"action": "abort"}})
    assert error.value.code == "INVALID_ARGUMENT"


async def test_network_retention_is_bounded_and_old_ids_are_rejected():
    monitor = NetworkMonitor()
    await monitor.operate({"operation": "start"})
    first_id = ""
    for index in range(205):
        request = Mock(
            url=f"https://example.com/{index}",
            method="GET",
            resource_type="fetch",
            headers={},
        )
        monitor.request(request)
        if index == 0:
            first_id = next(iter(monitor.entries))
    result = await monitor.operate({"operation": "list", "limit": 200})
    assert len(result["requests"]) == 200
    assert result["dropped"] == 5
    assert len(monitor.requests) == 200
    with pytest.raises(AgentBrowserError) as error:
        await monitor.operate({"operation": "detail", "request_id": first_id})
    assert error.value.code == "REQUEST_NOT_FOUND"


@pytest.mark.parametrize("declared_length", [None, MAX_BODY + 1, 50])
async def test_network_detail_redacts_headers_and_controls_body_reading(
    declared_length,
):
    monitor = NetworkMonitor()
    await monitor.operate({"operation": "start"})
    request = Mock(
        url="https://example.com/data",
        method="POST",
        resource_type="fetch",
        headers={
            "authorization": "Bearer secret",
            "cookie": "session=secret",
            "content-type": "application/json",
        },
        post_data='{"query":"test"}',
        failure=None,
    )
    monitor.request(request)
    headers = {"content-type": "application/json", "set-cookie": "session=secret"}
    if declared_length is not None:
        headers["content-length"] = str(declared_length)
    response = SimpleNamespace(
        request=request,
        headers=headers,
        status=200,
        body=AsyncMock(return_value=b"x" * (MAX_BODY + 1)),
    )
    monitor.response(response)
    monitor.finished(request)
    request_id = next(iter(monitor.entries))
    detail = await monitor.operate(
        {"operation": "detail", "request_id": request_id, "include_body": True}
    )
    record = detail["request"]
    assert record["request_headers"]["authorization"] == "<redacted>"
    assert record["response_headers"]["set-cookie"] == "<redacted>"
    if declared_length == 50:
        assert len(record["response_body"]) == MAX_BODY
        assert record["response_body_truncated"]
        response.body.assert_awaited_once()
    else:
        assert record["body_unavailable"]
        response.body.assert_not_awaited()
