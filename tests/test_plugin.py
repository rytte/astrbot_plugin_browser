import base64
import json
from copy import copy
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_browser.agent_browser import AgentBrowserError, BrowserResult
from astrbot_plugin_browser.main import BrowserPlugin
from astrbot_plugin_browser.service import BrowserServiceError

BASE_TOOLS = {
    "browser_open",
    "browser_close",
    "browser_observe",
    "browser_screenshot",
    "browser_click",
    "browser_hover",
    "browser_fill",
    "browser_press",
    "browser_select",
    "browser_scroll",
    "browser_wait",
    "browser_tabs",
}
STANDARD_TOOLS = BASE_TOOLS | {
    "browser_drag",
    "browser_upload",
    "browser_download",
    "browser_extract",
    "browser_dialog",
    "browser_state",
}
FULL_TOOLS = STANDARD_TOOLS | {
    "browser_network",
    "browser_intercept",
    "browser_emulate",
    "browser_evaluate",
}


@pytest.fixture(autouse=True)
def plugin_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "astrbot_plugin_browser.main.StarTools.get_data_dir", lambda name: tmp_path
    )


def test_plugin_owns_one_configured_service():
    plugin = BrowserPlugin(
        SimpleNamespace(),
        {
            "browser": {
                "browser_executable": "",
                "max_concurrent_sessions": 3,
                "queue_timeout": 20,
                "startup_timeout": 15,
            },
        },
    )

    assert plugin.service.max_concurrent_sessions == 3
    assert plugin.service.queue_timeout == 20
    assert plugin.service.startup_timeout == 15


def test_plugin_rejects_unknown_settings():
    with pytest.raises(BrowserServiceError, match="未知插件配置"):
        BrowserPlugin(SimpleNamespace(), {"unrecognized": True})


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({"browser_executable": ""}, "未知插件配置.*browser_executable"),
        ({"max_concurrent_sessions": 2}, "未知插件配置.*max_concurrent_sessions"),
        ({"queue_timeout": 20}, "未知插件配置.*queue_timeout"),
        ({"startup_timeout": 15}, "未知插件配置.*startup_timeout"),
        ({"browser": None}, "browser 必须是配置对象"),
        ({"browser": []}, "browser 必须是配置对象"),
        ({"browser": {"unknown": 1}}, "未知浏览器基础配置.*browser.unknown"),
    ],
)
def test_plugin_rejects_flat_or_invalid_browser_settings(config, error):
    with pytest.raises(BrowserServiceError, match=error):
        BrowserPlugin(SimpleNamespace(), config)


def test_plugin_reads_grouped_browser_use_settings():
    plugin = BrowserPlugin(
        SimpleNamespace(),
        {
            "browser_use": {
                "enabled": False,
                "tool_level": "standard",
                "max_sessions": 2,
                "idle_timeout": 120,
                "action_timeout": 10,
            }
        },
    )
    assert plugin.browser_use_enabled is False
    assert plugin.agent.tool_level == "standard"
    assert plugin.agent.max_sessions == 2
    assert plugin.agent.idle_timeout == 120
    assert plugin.agent.action_timeout == 10


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({"max_agent_sessions": 2}, "未知插件配置.*max_agent_sessions"),
        ({"agent_idle_timeout": 120}, "未知插件配置.*agent_idle_timeout"),
        ({"agent_action_timeout": 10}, "未知插件配置.*agent_action_timeout"),
        ({"browser_use": None}, "browser_use 必须是配置对象"),
        ({"browser_use": False}, "browser_use 必须是配置对象"),
        ({"browser_use": {"unknown": True}}, "未知 BrowserUse 配置"),
        ({"browser_use": {"enabled": "false"}}, "enabled 必须是布尔值"),
        ({"browser_use": {"enabled": 0}}, "enabled 必须是布尔值"),
        ({"browser_use": {"max_sessions": 0}}, "browser_use.max_sessions"),
        ({"browser_use": {"idle_timeout": 0}}, "browser_use.idle_timeout"),
        ({"browser_use": {"action_timeout": 0}}, "browser_use.action_timeout"),
    ],
)
def test_plugin_rejects_old_or_invalid_browser_use_config(config, error):
    with pytest.raises(BrowserServiceError, match=error):
        BrowserPlugin(SimpleNamespace(), config)


def test_schema_loads_both_configuration_groups(tmp_path):
    from astrbot.api import AstrBotConfig

    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    config = AstrBotConfig(str(tmp_path / "config.json"), schema=schema)
    assert config == {
        "browser": {
            "browser_executable": "",
            "max_concurrent_sessions": 4,
            "queue_timeout": 30,
            "startup_timeout": 30,
        },
        "browser_use": {
            "enabled": True,
            "tool_level": "basic",
            "max_sessions": 4,
            "idle_timeout": 600,
            "action_timeout": 30,
        },
    }
    plugin = BrowserPlugin(SimpleNamespace(), config)
    assert plugin.browser_use_enabled is True
    assert plugin.agent.tool_level == "basic"
    assert schema["browser_use"]["items"]["tool_level"]["options"] == [
        "basic",
        "standard",
        "full",
    ]


@pytest.fixture
def tool_manager():
    from astrbot.core.provider.func_tool_manager import FunctionToolManager
    from astrbot.core.provider.register import llm_tools

    manager = FunctionToolManager()
    for tool in llm_tools.func_list:
        if getattr(tool.handler, "__module__", None) == BrowserPlugin.__module__:
            tool = copy(tool)
            tool.handler_module_path = BrowserPlugin.__module__
            manager.func_list.append(tool)
    assert len(manager.func_list) == 22
    return manager


@pytest.mark.parametrize("level", ["basic", "standard", "full"])
async def test_disabled_browser_use_removes_tool_definitions(tool_manager, level):
    from astrbot.api import FunctionTool

    other = FunctionTool(
        name="browser_other_plugin",
        description="Other plugin tool",
        parameters={"type": "object", "properties": {}},
        handler_module_path="other_plugin.main",
    )
    tool_manager.func_list.append(other)
    context = SimpleNamespace(get_llm_tool_manager=lambda: tool_manager)
    plugin = BrowserPlugin(
        context,
        {
            "browser_use": {
                "enabled": False,
                "tool_level": level,
            }
        },
    )
    # AstrBot binds decorator handlers and activation flags before initialize().
    for tool in tool_manager.func_list[:-1]:
        tool.handler = partial(tool.handler, plugin)
    plugin.service.initialize = AsyncMock()
    plugin.service.close = AsyncMock()
    plugin.agent.execute = AsyncMock()
    try:
        await plugin.initialize()
        assert plugin.agent._reaper is None
        assert tool_manager.func_list == [other]
        schemas = tool_manager.get_func_desc_openai_style()
        assert [item["function"]["name"] for item in schemas] == [other.name]
        assert tool_manager.get_func("browser_open") is None
        result = await plugin.browser_screenshot(event())
        assert result.isError
        assert json.loads(result.content[0].text)["error"]["code"] == (
            "BROWSER_USE_DISABLED"
        )
        plugin.agent.execute.assert_not_awaited()
        plugin.service.initialize.assert_awaited_once()
    finally:
        await plugin.terminate()
    plugin.service.close.assert_awaited_once()


async def test_enabled_browser_use_preserves_tool_activation(tool_manager):
    tool_manager.get_func("browser_open").active = False
    context = SimpleNamespace(get_llm_tool_manager=lambda: tool_manager)
    plugin = BrowserPlugin(context, {"browser_use": {"enabled": True}})
    plugin.service.initialize = AsyncMock()
    plugin.service.close = AsyncMock()
    try:
        await plugin.initialize()
        assert plugin.agent._reaper is not None
        assert len(tool_manager.func_list) == 12
        names = {
            item["function"]["name"]
            for item in tool_manager.get_func_desc_openai_style()
        }
        assert len(names) == 11
        assert "browser_open" not in names
        assert "browser_screenshot" in names
    finally:
        await plugin.terminate()


async def test_plugin_lifecycle_delegates_to_service(tool_manager):
    plugin = BrowserPlugin(
        SimpleNamespace(get_llm_tool_manager=lambda: tool_manager), {}
    )
    plugin.service.initialize = AsyncMock()
    plugin.service.close = AsyncMock()

    await plugin.initialize()
    await plugin.terminate()

    plugin.service.initialize.assert_awaited_once()
    plugin.service.close.assert_awaited_once()


def event(sender="alice", origin="test:group:one"):
    return SimpleNamespace(
        unified_msg_origin=origin,
        get_sender_id=lambda: sender,
        get_extra=lambda key: None,
    )


async def test_tool_uses_event_identity_and_returns_structured_page_state():
    plugin = BrowserPlugin(SimpleNamespace(), {})
    plugin.agent.execute = AsyncMock(
        return_value=BrowserResult({"success": True, "title": "页面"})
    )
    result = await plugin.browser_open(event(), "https://example.com")
    plugin.agent.execute.assert_awaited_once_with(
        ("test:group:one", "alice"), "open", url="https://example.com", new_tab=False
    )
    assert json.loads(result.content[0].text)["title"] == "页面"
    assert not result.isError


async def test_tool_preserves_error_code():
    plugin = BrowserPlugin(SimpleNamespace(), {})
    plugin.agent.execute = AsyncMock(
        side_effect=AgentBrowserError("NO_SESSION", "请先打开网页")
    )
    result = await plugin.browser_observe(event())
    assert result.isError
    assert json.loads(result.content[0].text)["error"] == {
        "code": "NO_SESSION",
        "message": "请先打开网页",
    }


async def test_screenshot_is_image_content_and_survives_astrbot_tool_executor():
    from astrbot.api import FunctionTool
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    provider = SimpleNamespace(provider_config={"modalities": ["text", "image"]})
    context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
    plugin = BrowserPlugin(context, {})
    png = b"\x89PNG\r\n\x1a\nimage"
    plugin.agent.execute = AsyncMock(
        return_value=BrowserResult({"success": True}, image=png)
    )
    message_event = event()
    message_event.send = AsyncMock()
    message_event.get_result = Mock(return_value=None)
    tool = FunctionTool(
        name="browser_screenshot",
        description="Screenshot",
        parameters={"type": "object"},
        handler=plugin.browser_screenshot,
    )
    run_context = SimpleNamespace(
        context=SimpleNamespace(event=message_event), tool_call_timeout=30
    )
    outputs = [
        result
        async for result in FunctionToolExecutor._execute_local(tool, run_context)
    ]
    assert len(outputs) == 1
    assert outputs[0].content[1].type == "image"
    assert base64.b64decode(outputs[0].content[1].data) == png
    message_event.send.assert_not_awaited()


@pytest.mark.parametrize("modalities", [None, [], ["text"]])
async def test_screenshot_rejects_unconfigured_or_text_only_provider(modalities):
    context = SimpleNamespace(
        get_using_provider_async=AsyncMock(
            return_value=SimpleNamespace(provider_config={"modalities": modalities})
        )
    )
    plugin = BrowserPlugin(context, {})
    plugin.agent.execute = AsyncMock()
    result = await plugin.browser_screenshot(event())
    assert result.isError
    assert json.loads(result.content[0].text)["error"]["code"] == "VISION_UNAVAILABLE"
    plugin.agent.execute.assert_not_awaited()


def test_all_tools_registered_with_parameter_descriptions():
    from astrbot.core.provider.register import llm_tools

    tools = {
        tool.name: tool
        for tool in llm_tools.func_list
        if tool.name.startswith("browser_")
    }
    assert set(tools) == {
        "browser_open",
        "browser_observe",
        "browser_screenshot",
        "browser_click",
        "browser_hover",
        "browser_drag",
        "browser_fill",
        "browser_press",
        "browser_select",
        "browser_scroll",
        "browser_wait",
        "browser_upload",
        "browser_download",
        "browser_extract",
        "browser_dialog",
        "browser_state",
        "browser_network",
        "browser_intercept",
        "browser_emulate",
        "browser_evaluate",
        "browser_tabs",
        "browser_close",
    }
    assert (
        tools["browser_click"].parameters["properties"]["observation_id"]["type"]
        == "string"
    )
    assert (
        tools["browser_scroll"].parameters["properties"]["pixels"]["type"] == "number"
    )
    assert (
        tools["browser_click"].parameters["properties"]["click_count"]["type"]
        == "number"
    )
    assert tools["browser_drag"].parameters["properties"]["end_y"]["type"] == "number"
    assert tools["browser_upload"].parameters["properties"]["file_ids"]["items"] == {
        "type": "string"
    }


@pytest.mark.parametrize(
    ("name", "arguments", "action"),
    [
        ("browser_click", {"ref": "e1", "button": "right", "click_count": 1}, "click"),
        ("browser_click", {"x": 30, "y": 40, "click_count": 2}, "click"),
        ("browser_hover", {"ref": "e1"}, "hover"),
        ("browser_drag", {"source_ref": "e1", "target_ref": "e2"}, "drag"),
        (
            "browser_drag",
            {"start_x": 10, "start_y": 20, "end_x": 100, "end_y": 200},
            "drag",
        ),
    ],
)
async def test_mouse_tool_parameters_reach_agent(name, arguments, action):
    plugin = BrowserPlugin(
        SimpleNamespace(), {"browser_use": {"tool_level": "standard"}}
    )
    plugin.agent.execute = AsyncMock(return_value=BrowserResult({"success": True}))
    result = await getattr(plugin, name)(
        event(), observation_id="obs_test", **arguments
    )
    assert not result.isError
    call = plugin.agent.execute.await_args
    assert call.args == (("test:group:one", "alice"), action)
    assert call.kwargs["observation_id"] == "obs_test"
    for key, value in arguments.items():
        assert call.kwargs[key] == value


async def test_screenshot_reaches_next_model_request(tmp_path, monkeypatch):
    from astrbot.api import FunctionTool, ToolSet
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool_image_cache import tool_image_cache
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest

    monkeypatch.setattr(tool_image_cache, "_cache_dir", str(tmp_path))
    provider = SimpleNamespace(
        provider_config={"id": "test", "modalities": ["text", "image", "tool_use"]}
    )
    provider.text_chat = AsyncMock(
        side_effect=[
            LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["browser_screenshot"],
                tools_call_args=[{}],
                tools_call_ids=["browser-image-test"],
            ),
            LLMResponse(role="assistant", completion_text="已查看截图"),
        ]
    )
    plugin = BrowserPlugin(
        SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider)), {}
    )
    png = b"\x89PNG\r\n\x1a\nimage"
    plugin.agent.execute = AsyncMock(
        return_value=BrowserResult({"success": True}, image=png)
    )
    message_event = event()
    message_event.send = AsyncMock()
    message_event.get_result = Mock(return_value=None)
    tool = FunctionTool(
        name="browser_screenshot",
        description="Screenshot",
        parameters={"type": "object", "properties": {}},
        handler=plugin.browser_screenshot,
    )
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=ProviderRequest(
            prompt="查看页面", contexts=[], func_tool=ToolSet(tools=[tool])
        ),
        run_context=ContextWrapper(context=SimpleNamespace(event=message_event)),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
    )
    async for _ in runner.step_until_done(3):
        pass
    assert provider.text_chat.await_count == 2
    contexts = provider.text_chat.await_args_list[1].kwargs["contexts"]
    encoded = base64.b64encode(png).decode()
    assert encoded in str(contexts)
    assert "image_url" in str(contexts)
    message_event.send.assert_not_awaited()


async def test_screenshot_checks_explicitly_selected_provider():
    provider = SimpleNamespace(
        provider_config={"modalities": ["text", "image", "tool_use"]}
    )
    context = SimpleNamespace(
        get_provider_by_id=Mock(return_value=provider),
        get_using_provider_async=AsyncMock(),
    )
    plugin = BrowserPlugin(context, {})
    plugin.agent.execute = AsyncMock(
        return_value=BrowserResult({"success": True}, image=b"png")
    )
    message_event = event()
    message_event.get_extra = lambda key: "selected-model"
    result = await plugin.browser_screenshot(message_event)
    assert not result.isError
    context.get_provider_by_id.assert_called_once_with("selected-model")
    context.get_using_provider_async.assert_not_awaited()


async def test_upload_imports_direct_chat_attachments_and_deduplicates(tmp_path):
    from astrbot.api.message_components import File, Image

    plugin = BrowserPlugin(
        SimpleNamespace(), {"browser_use": {"tool_level": "standard"}}
    )
    source = tmp_path / "attachment.txt"
    source.write_bytes(b"attachment")
    picture = tmp_path / "photo.png"
    picture.write_bytes(b"image")
    image = Image.fromFileSystem(str(picture))
    current = event()
    current.get_messages = lambda: [
        File(name="说明.txt", file=str(source)),
        image,
        SimpleNamespace(chain=[File(name="quoted.txt", file=str(source))]),
    ]
    try:
        first = await plugin.browser_upload(current)
        data = json.loads(first.content[0].text)
        assert not first.isError
        assert len(data["files"]) == 2
        assert {f["source"] for f in data["files"]} == {"attachment"}
        assert str(tmp_path) not in first.content[0].text
        again = await plugin.browser_upload(current)
        assert json.loads(again.content[0].text)["files"] == data["files"]
        stranger = event(sender="bob")
        stranger.get_messages = lambda: []
        assert (
            json.loads((await plugin.browser_upload(stranger)).content[0].text)["files"]
            == []
        )
        assert plugin.service._browser is None
    finally:
        paths = [
            f.path
            for f in plugin.agent.storage.files.get(
                (current.unified_msg_origin, "alice"), {}
            ).values()
        ]
        await plugin.terminate()
    assert all(not path.exists() for path in paths)
    assert source.exists() and picture.exists()


async def test_file_send_is_explicit_and_delivery_failure_is_reported(tmp_path):
    from astrbot.api.message_components import File

    plugin = BrowserPlugin(
        SimpleNamespace(), {"browser_use": {"tool_level": "standard"}}
    )
    source = tmp_path / "attachment.txt"
    source.write_bytes(b"hello")
    current = event()
    current.get_messages = lambda: [File(name="report.txt", file=str(source))]
    current.chain_result = lambda chain: chain
    current.send = AsyncMock()
    try:
        listed = await plugin.browser_upload(current)
        file_id = json.loads(listed.content[0].text)["files"][0]["file_id"]
        current.send.assert_not_awaited()
        sent = await plugin.browser_download(current, operation="send", file_id=file_id)
        assert json.loads(sent.content[0].text)["sent_to_current_chat"]
        outgoing = current.send.call_args.args[0][0]
        assert outgoing.name == "report.txt"
        assert Path(await outgoing.get_file()).read_bytes() == b"hello"
        current.send.side_effect = RuntimeError("platform unavailable")
        failed = await plugin.browser_download(
            current, operation="send", file_id=file_id
        )
        assert failed.isError
        assert json.loads(failed.content[0].text)["error"]["code"] == "FILE_SEND_FAILED"
        assert (
            await plugin.agent.execute(
                (current.unified_msg_origin, "alice"), "download"
            )
        ).data["files"][0]["status"] == "ready"
    finally:
        await plugin.terminate()


async def test_disabled_upload_never_reads_attachments():
    plugin = BrowserPlugin(SimpleNamespace(), {"browser_use": {"enabled": False}})
    current = event()
    current.get_messages = Mock(side_effect=AssertionError("must not read attachments"))
    result = await plugin.browser_upload(current)
    assert json.loads(result.content[0].text)["error"]["code"] == "BROWSER_USE_DISABLED"
    current.get_messages.assert_not_called()


async def test_attachment_error_is_visible_and_does_not_expose_local_paths(tmp_path):
    from astrbot.api.message_components import File

    plugin = BrowserPlugin(
        SimpleNamespace(), {"browser_use": {"tool_level": "standard"}}
    )
    current = event()
    current.get_messages = lambda: [
        File(name="missing.txt", file=str(tmp_path / "missing.txt"))
    ]
    try:
        result = await plugin.browser_upload(current)
        data = json.loads(result.content[0].text)
        assert data["attachment_errors"]
        assert not data["files"]
        assert str(tmp_path) not in result.content[0].text
    finally:
        await plugin.terminate()


@pytest.mark.parametrize(
    ("level", "expected", "count"),
    [
        ("basic", BASE_TOOLS, 12),
        ("standard", STANDARD_TOOLS, 18),
        ("full", FULL_TOOLS, 22),
    ],
)
async def test_tool_levels_expose_exact_definitions_without_changing_other_plugins(
    tool_manager, level, expected, count
):
    from astrbot.api import FunctionTool

    other = FunctionTool(
        name="browser_external",
        description="Other plugin tool",
        parameters={"type": "object", "properties": {}},
        handler_module_path="other_plugin.main",
    )
    tool_manager.func_list.append(other)
    context = SimpleNamespace(get_llm_tool_manager=lambda: tool_manager)
    plugin = BrowserPlugin(context, {"browser_use": {"tool_level": level}})
    plugin.service.initialize = AsyncMock()
    plugin.service.close = AsyncMock()
    try:
        await plugin.initialize()
        names = {tool.name for tool in tool_manager.func_list}
        assert names == expected | {other.name}
        assert len(names) == count + 1
        schemas = tool_manager.get_func_desc_openai_style()
        assert {item["function"]["name"] for item in schemas} == expected | {other.name}
        for name in FULL_TOOLS - expected:
            assert tool_manager.get_func(name) is None
    finally:
        await plugin.terminate()


async def test_enabling_all_advanced_tools_exposes_22_and_preserves_activation(
    tool_manager,
):
    context = SimpleNamespace(get_llm_tool_manager=lambda: tool_manager)
    plugin = BrowserPlugin(context, {"browser_use": {"tool_level": "full"}})
    plugin.service.initialize = AsyncMock()
    plugin.service.close = AsyncMock()
    tool_manager.get_func("browser_evaluate").active = False
    try:
        await plugin.initialize()
        assert len(tool_manager.func_list) == 22
        names = {
            item["function"]["name"]
            for item in tool_manager.get_func_desc_openai_style()
        }
        assert len(names) == 21
        assert "browser_evaluate" not in names
    finally:
        await plugin.terminate()


@pytest.mark.parametrize(
    ("level", "name"),
    [("basic", name) for name in sorted(FULL_TOOLS - BASE_TOOLS)]
    + [("standard", name) for name in sorted(FULL_TOOLS - STANDARD_TOOLS)],
)
async def test_cached_higher_level_calls_fail_before_agent_execution(level, name):
    plugin = BrowserPlugin(SimpleNamespace(), {"browser_use": {"tool_level": level}})
    plugin.agent.execute = AsyncMock()
    result = await plugin._tool(event(), name.removeprefix("browser_"))
    assert result.isError
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "TOOL_LEVEL_REQUIRED"
    assert "tool_level" in error["message"]
    plugin.agent.execute.assert_not_awaited()


async def test_basic_upload_never_reads_attachments():
    plugin = BrowserPlugin(SimpleNamespace(), {})
    current = event()
    current.get_messages = Mock(side_effect=AssertionError("must not read attachments"))
    result = await plugin.browser_upload(current)
    assert json.loads(result.content[0].text)["error"]["code"] == "TOOL_LEVEL_REQUIRED"
    current.get_messages.assert_not_called()
    assert not plugin.agent.storage.files


@pytest.mark.parametrize("level", [None, "", "advanced", "Basic", 1, True, [], {}])
def test_tool_level_rejects_unknown_values_and_invalid_types(level):
    with pytest.raises(BrowserServiceError, match="browser_use.tool_level 必须是"):
        BrowserPlugin(SimpleNamespace(), {"browser_use": {"tool_level": level}})


@pytest.mark.parametrize(
    "config_key",
    ["network_enabled", "intercept_enabled", "emulation_enabled", "javascript_enabled"],
)
@pytest.mark.parametrize("value", [False, True])
def test_removed_advanced_switches_fail_fast_even_with_explicit_level(
    config_key, value
):
    with pytest.raises(
        BrowserServiceError, match="未知 BrowserUse 配置.*" + config_key
    ):
        BrowserPlugin(
            SimpleNamespace(),
            {"browser_use": {"tool_level": "full", config_key: value}},
        )
