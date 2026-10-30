"""Compact a completed run's browser observations without breaking tool-call pairs."""

from __future__ import annotations

import json
import re

from astrbot.core.agent.message import Message, TextPart

OBSERVATION_TOOLS = frozenset(
    f"browser_{action}"
    for action in (
        "open",
        "observe",
        "screenshot",
        "click",
        "hover",
        "drag",
        "fill",
        "press",
        "select",
        "scroll",
        "wait",
        "upload",
        "tabs",
        "evaluate",
    )
)
SNAPSHOT_FIELDS = {
    "frames",
    "frames_truncated",
    "viewport",
    "coordinate_click_available",
    "coordinate_notice",
}
IMAGE_PATH = re.compile(r"Image returned and cached at path='([^'\n]+)'\.")


def is_observation(data: object) -> bool:
    """Recognize successful page observations eligible for compaction."""
    return (
        isinstance(data, dict)
        and data.get("success") is True
        and isinstance(data.get("observation_id"), str)
        and bool(data["observation_id"])
        and isinstance(data.get("frames"), list)
    )


def compact_observations(messages: list[Message]) -> None:
    """Compact all page snapshots from a completed run, including its last one.

    The caller must supply only messages newly added in that run, after the
    final model response and before persistence. Historical messages are excluded.

    AstrBot appends image notices and system guidance after a tool's JSON, so
    decode only the JSON prefix and preserve the remaining text. Images are
    removed only when both their tool notice and cache ID match a stale result.
    """
    tool_names: dict[str, str] = {}
    observations = []
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls or []:
                if isinstance(call, dict):
                    function = call.get("function")
                    if (
                        isinstance(call.get("id"), str)
                        and isinstance(function, dict)
                        and isinstance(function.get("name"), str)
                    ):
                        tool_names[call["id"]] = function["name"]
                else:
                    tool_names[call.id] = call.function.name
        name = tool_names.get(message.tool_call_id or "", "")
        if message.role != "tool" or name not in OBSERVATION_TOOLS:
            continue
        parts = (
            [message.content]
            if isinstance(message.content, str)
            else message.content or []
        )
        for part in parts:
            text = part if isinstance(part, str) else getattr(part, "text", None)
            if not isinstance(text, str):
                continue
            try:
                text = text.lstrip()
                data, end = json.JSONDecoder().raw_decode(text)
            except ValueError:
                continue
            if is_observation(data):
                observations.append((message, part, name, data, text[end:]))
                break

    stale_paths: set[str] = set()
    for message, part, name, data, suffix in observations:
        summary = {
            key: value for key, value in data.items() if key not in SNAPSHOT_FIELDS
        }
        summary["observation_compacted"] = True
        summary["observation_note"] = (
            "页面观察已在本轮结束后精简；继续操作前请重新观察或截图。"
        )
        if name == "browser_screenshot":
            for path in IMAGE_PATH.findall(suffix):
                stale_paths.add(path)
                # This is AstrBot's generated image notice, not page content.
                notice = (
                    f"Image returned and cached at path='{path}'. "
                    "Review the image below. Use send_message_to_user to send it to the user if satisfied, "
                    f"with type='image' and path='{path}'."
                )
                suffix = suffix.replace(notice, "")
        compacted = json.dumps(summary, ensure_ascii=False) + suffix
        if isinstance(part, str):
            message.content = compacted
        else:
            part.text = compacted

    if stale_paths:
        _compact_images(messages, stale_paths)


def _compact_images(messages: list[Message], stale_paths: set[str]) -> None:
    markers = {
        f"[Image from tool 'browser_screenshot', path='{path}']": path
        for path in stale_paths
    }
    for message in messages:
        if message.role != "user" or not isinstance(message.content, list):
            continue
        parts = message.content
        kept = []
        index = 0
        while index < len(parts):
            path = markers.get(getattr(parts[index], "text", ""))
            following = parts[index + 1] if index + 1 < len(parts) else None
            if (
                path is not None
                and getattr(following, "type", None) == "image_url"
                and getattr(following.image_url, "id", None) == path
            ):
                index += 2
            else:
                kept.append(parts[index])
                index += 1
        if len(kept) != len(parts):
            message.content = kept or [TextPart(text="旧浏览器截图已随页面观察精简。")]
