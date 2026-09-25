"""Expose the shared browser service as an AstrBot plugin."""

from __future__ import annotations

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star

from .service import BrowserService, BrowserServiceError


class BrowserPlugin(Star):
    """Own the shared local Chromium process for other plugins."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        settings = dict(config)
        allowed = {
            "browser_executable",
            "max_concurrent_sessions",
            "queue_timeout",
            "startup_timeout",
        }
        unknown = set(settings) - allowed
        if unknown:
            raise BrowserServiceError("未知插件配置：" + ", ".join(sorted(unknown)))
        self.service = BrowserService(
            browser_executable=settings.get("browser_executable", ""),
            max_concurrent_sessions=settings.get("max_concurrent_sessions", 4),
            queue_timeout=settings.get("queue_timeout", 30),
            startup_timeout=settings.get("startup_timeout", 30),
        )

    async def initialize(self) -> None:
        """Start the configured browser and make the service available."""
        await self.service.initialize()
        self.logger.info("Shared browser service is ready")

    async def terminate(self) -> None:
        """Stop active sessions and release the browser process."""
        await self.service.close()
