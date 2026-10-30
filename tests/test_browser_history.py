import json
from copy import deepcopy

import pytest
from astrbot.core.agent.message import ImageURLPart, Message, TextPart, ToolCall
from astrbot_plugin_browser.browser_history import compact_observations


def observation(number, **extra):
    return {
        "success": True,
        "observation_id": f"obs_{number}",
        "url": f"https://example.com/{number}",
        "title": f"页面 {number}",
        "active_tab": "tab_1",
        "viewport": {"width": 1280, "height": 720},
        "frames": [{"text": f"正文 {number}" * 3000, "elements": [{"ref": "e1"}]}],
        **extra,
    }


def tool_result(number, data, name="browser_observe", suffix="", multipart=False):
    text = json.dumps(data, ensure_ascii=False) + suffix
    return [
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id=str(number),
                    function=ToolCall.FunctionBody(name=name, arguments="{}"),
                )
            ],
        ),
        Message(
            role="tool",
            tool_call_id=str(number),
            content=[TextPart(text=text)] if multipart else text,
        ),
    ]


def result_data(message):
    text = message.content
    if isinstance(text, list):
        text = text[0].text
    return json.JSONDecoder().raw_decode(text)[0]


def image_notice(path):
    return (
        f"\n\nImage returned and cached at path='{path}'. "
        "Review the image below. Use send_message_to_user to send it to the user if satisfied, "
        f"with type='image' and path='{path}'."
    )


def screenshot_parts(path):
    return [
        TextPart(text=f"[Image from tool 'browser_screenshot', path='{path}']"),
        ImageURLPart(
            image_url=ImageURLPart.ImageURL(url="data:image/png;base64,b2xk", id=path)
        ),
    ]


def test_compaction_includes_last_result_and_keeps_pairs_metadata_and_guidance():
    guidance = "\n\n[SYSTEM NOTICE] Finish the task."
    messages = [
        *tool_result(1, observation(1), suffix=guidance),
        *tool_result(2, observation(2)),
    ]
    original = deepcopy(messages)
    compact_observations(messages)

    summary = result_data(messages[1])
    assert summary["observation_compacted"] is True
    assert summary["url"] == "https://example.com/1"
    assert summary["title"] == "页面 1"
    assert "frames" not in summary
    assert "viewport" not in summary
    assert messages[1].content.endswith(guidance)
    assert len(messages[1].content) < len(original[1].content) / 10
    assert messages[0] == original[0]
    assert messages[1].tool_call_id == original[1].tool_call_id
    assert messages[2] == original[2]
    assert messages[3].tool_call_id == original[3].tool_call_id
    assert result_data(messages[3])["observation_compacted"] is True
    assert "frames" not in result_data(messages[3])

    compacted = deepcopy(messages)
    compact_observations(messages)
    assert messages == compacted


def test_non_page_data_errors_user_content_and_other_tools_are_preserved():
    durable = {
        "evaluation": {"result": "脚本提取的正文"},
        "downloads": [{"id": "file_1"}],
        "attachment_errors": ["附件无法读取"],
        "interception_errors": ["拦截失败"],
        "dialogs": [{"message": "提交成功"}],
    }
    messages = [
        *tool_result(
            1, observation(1, **durable), name="browser_evaluate", multipart=True
        ),
        *tool_result(2, observation(2)),
        *tool_result(3, {"success": False, "error": {"code": "TIMEOUT"}}),
        *tool_result(
            4, {"success": True, "text": "提取的正文"}, name="browser_extract"
        ),
        *tool_result(5, observation(5), name="other_tool"),
        Message(role="user", content="保留用户的问题"),
    ]
    original = deepcopy(messages)
    compact_observations(messages)

    summary = result_data(messages[1])
    for key, value in durable.items():
        assert summary[key] == value
    assert "frames" not in result_data(messages[3])
    assert messages[4:] == original[4:]


def test_all_browser_screenshots_are_compacted_and_other_images_are_preserved():
    old_path, latest_path = "/tmp/old.png", "/tmp/latest.png"
    user_parts = [
        TextPart(text="用户上传的图片"),
        ImageURLPart(
            image_url=ImageURLPart.ImageURL(url="https://example.com/user.png")
        ),
    ]
    other_parts = [
        TextPart(text="[Image from tool 'other_tool', path='/tmp/other.png']"),
        ImageURLPart(
            image_url=ImageURLPart.ImageURL(url="https://example.com/other.png")
        ),
    ]
    messages = [
        *tool_result(
            1, observation(1), name="browser_screenshot", suffix=image_notice(old_path)
        ),
        Message(
            role="user",
            content=[*screenshot_parts(old_path), *user_parts, *other_parts],
        ),
        *tool_result(
            2,
            observation(2),
            name="browser_screenshot",
            suffix=image_notice(latest_path),
        ),
        Message(role="user", content=screenshot_parts(latest_path)),
    ]
    compact_observations(messages)

    assert "Image returned" not in messages[1].content
    assert messages[2].content == [*user_parts, *other_parts]
    assert "Image returned" not in messages[-2].content
    assert result_data(messages[-2])["observation_compacted"] is True
    assert all(part.type == "text" for part in messages[-1].content)


def test_unmatched_image_and_unparseable_results_are_left_untouched():
    messages = [
        *tool_result(1, observation(1), name="browser_screenshot"),
        Message(role="user", content=screenshot_parts("/tmp/unrelated.png")),
        *tool_result(2, observation(2)),
        *tool_result(3, "not an observation"),
        Message(role="tool", tool_call_id="3", content="{bad json"),
    ]
    original = deepcopy(messages)
    compact_observations(messages)
    assert messages[2:4] == original[2:4]
    assert messages[5:] == original[5:]


@pytest.mark.parametrize("failed_call", [False, True])
def test_single_observation_is_compacted_and_a_following_error_is_preserved(
    failed_call,
):
    messages = [
        *tool_result(
            1,
            observation(1),
            name="browser_screenshot",
            suffix=image_notice("/tmp/old.png"),
        ),
        Message(role="user", content=screenshot_parts("/tmp/old.png")),
    ]
    if failed_call:
        messages.extend(
            tool_result(2, {"success": False, "error": {"code": "TIMEOUT"}})
        )
    original = deepcopy(messages)
    compact_observations(messages)
    assert result_data(messages[1])["observation_compacted"] is True
    assert "frames" not in result_data(messages[1])
    assert all(part.type == "text" for part in messages[2].content)
    assert messages[3:] == original[3:]


def test_tool_calls_stored_as_dictionaries_are_supported():
    messages = [*tool_result(1, observation(1)), *tool_result(2, observation(2))]
    messages[0].tool_calls = [messages[0].tool_calls[0].model_dump()]
    compact_observations(messages)
    assert result_data(messages[1])["observation_compacted"] is True


def test_malformed_tool_metadata_does_not_prevent_compaction():
    malformed = Message(role="assistant", tool_calls=[{"id": "incomplete"}])
    messages = [
        malformed,
        *tool_result(1, observation(1)),
        *tool_result(2, observation(2)),
    ]
    compact_observations(messages)
    assert result_data(messages[2])["observation_compacted"] is True
