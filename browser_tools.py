"""Define cumulative model tool levels shared by registration and execution."""

from __future__ import annotations

from .service import BrowserServiceError

BASIC_ACTIONS = frozenset(
    {
        "mode",
        "open",
        "close",
        "observe",
        "screenshot",
        "click",
        "hover",
        "fill",
        "press",
        "select",
        "scroll",
        "wait",
        "tabs",
    }
)
STANDARD_ACTIONS = BASIC_ACTIONS | {
    "drag",
    "upload",
    "download",
    "extract",
    "dialog",
    "state",
}
TOOL_LEVELS = {
    "basic": BASIC_ACTIONS,
    "standard": STANDARD_ACTIONS,
    "full": STANDARD_ACTIONS | {"network", "intercept", "emulate", "evaluate"},
}


def actions_for_level(level: str) -> frozenset[str]:
    """Validate a configured level without accepting aliases or fallback values."""
    if not isinstance(level, str) or level not in TOOL_LEVELS:
        raise BrowserServiceError(
            "browser_use.tool_level 必须是 basic（基础浏览）、"
            "standard（完整交互）或 full（高级控制）。"
        )
    return TOOL_LEVELS[level]
