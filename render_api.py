"""Expose rendering operations without returning Playwright management objects."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from functools import wraps
from typing import Any, Literal, ParamSpec, TypeVar

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from .service import (
    MAX_SCREENSHOT_PIXELS,
    MAX_VIEWPORT_PIXELS,
    BrowserService,
    BrowserServiceError,
)

P = ParamSpec("P")
T = TypeVar("T")
TextFilter = str | re.Pattern[str] | None


def _render_action(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Check wrapper lifetime and normalize errors for explicitly declared calls."""

    @wraps(function)
    async def call(*args: P.args, **kwargs: P.kwargs) -> T:
        args[0]._check_active()
        timeout = (
            kwargs.get("timeout") if "timeout" in function.__annotations__ else None
        )
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise BrowserServiceError("操作 timeout 必须是非负有限毫秒数或 None。")
        try:
            return await function(*args, **kwargs)
        except PlaywrightError as exc:
            raise BrowserServiceError(f"渲染操作 {function.__name__} 失败。") from exc

    return call


def _validate_evaluation(expression: str, arg: Any) -> None:
    if not isinstance(expression, str) or not expression.strip():
        raise BrowserServiceError("expression 必须是非空 JavaScript 字符串。")
    # Playwright accepts by-value data beyond JSON, including dates, non-finite
    # numbers and cyclic lists/dicts. Never pass through native object handles.
    pending = [arg]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if type(value) in (type(None), bool, int, float, str, datetime):
            continue
        if type(value) not in (list, tuple, dict):
            raise BrowserServiceError(
                "evaluate 的 arg 只能包含基础值、datetime、列表、元组和字符串键字典，"
                "不能包含 Playwright 对象。"
            )
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise BrowserServiceError("evaluate 的 arg 字典键必须是字符串。")
            pending.extend(value.values())
        else:
            pending.extend(value)


def _validate_screenshot(image_type: str, animations: str) -> None:
    if image_type not in ("png", "jpeg"):
        raise BrowserServiceError("截图 type 必须是 png 或 jpeg。")
    if animations not in ("allow", "disabled"):
        raise BrowserServiceError("截图 animations 必须是 allow 或 disabled。")


async def _screenshot_bytes(action: Awaitable[bytes], timeout: float | None) -> bytes:
    # Bound the whole screenshot, including the locator resolution step, without
    # changing the page's shared default timeout for concurrent operations.
    if timeout is None or timeout == 0:
        return await action
    try:
        return await asyncio.wait_for(action, timeout=timeout / 1000)
    except asyncio.TimeoutError as exc:
        raise BrowserServiceError("渲染截图操作超时。") from exc


def _native_locator(locator: RenderLocator, owner: RenderPage) -> Locator:
    """Unwrap only a locator owned by this exact temporary page."""
    if not isinstance(locator, RenderLocator):
        raise BrowserServiceError(
            "必须使用同一渲染页面的 RenderLocator，不能传原生对象。"
        )
    owner._check_active()
    if locator._RenderLocator__owner is not owner:
        raise BrowserServiceError("不能使用其他渲染会话的 RenderLocator。")
    return locator._RenderLocator__locator


def _locator_filters(
    owner: RenderPage,
    has_text: TextFilter,
    has_not_text: TextFilter,
    has: RenderLocator | None,
    has_not: RenderLocator | None,
) -> dict[str, Any]:
    for text in (has_text, has_not_text):
        if text is not None and not isinstance(text, (str, re.Pattern)):
            raise BrowserServiceError("文本筛选条件必须是字符串或正则表达式。")
    return {
        "has_text": has_text,
        "has_not_text": has_not_text,
        "has": _native_locator(has, owner) if has is not None else None,
        "has_not": _native_locator(has_not, owner) if has_not is not None else None,
    }


def _native_masks(
    mask: Sequence[RenderLocator] | None, owner: RenderPage
) -> list[Locator] | None:
    if mask is None:
        return None
    if not isinstance(mask, (list, tuple)):
        raise BrowserServiceError("mask 必须是同一页面的 RenderLocator 列表或元组。")
    return [_native_locator(locator, owner) for locator in mask]


class RenderPage:
    """Allow layout and screenshots only within one temporary render context."""

    __slots__ = ("__page", "__device_scale_factor")

    def __init__(self, page: Page, device_scale_factor: float = 1) -> None:
        self.__page: Page | None = page
        self.__device_scale_factor = device_scale_factor

    def _invalidate(self) -> None:
        self.__page = None

    def _check_active(self) -> None:
        if self.__page is None:
            raise BrowserServiceError("渲染会话已结束，页面和元素不能继续使用。")

    @property
    def url(self) -> str:
        self._check_active()
        return self.__page.url

    @property
    def viewport_size(self) -> dict[str, int] | None:
        self._check_active()
        viewport = self.__page.viewport_size
        return dict(viewport) if viewport is not None else None

    @_render_action
    async def set_content(
        self,
        html: str,
        *,
        wait_until: Literal[
            "commit", "domcontentloaded", "load", "networkidle"
        ] = "load",
        timeout: float | None = None,
    ) -> None:
        """Set inline HTML; external resource requests remain blocked."""
        BrowserService._validate_html(html)
        if wait_until not in ("commit", "domcontentloaded", "load", "networkidle"):
            raise BrowserServiceError("wait_until 必须是 Playwright 支持的加载状态。")
        await self.__page.set_content(html, wait_until=wait_until, timeout=timeout)

    @_render_action
    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate in this page, accepting data arguments and returning by value."""
        _validate_evaluation(expression, arg)
        return await self.__page.evaluate(expression, arg)

    def locator(
        self,
        selector: str,
        *,
        has_text: TextFilter = None,
        has_not_text: TextFilter = None,
        has: RenderLocator | None = None,
        has_not: RenderLocator | None = None,
    ) -> RenderLocator:
        """Return a restricted locator, accepting only same-page filters."""
        self._check_active()
        if not isinstance(selector, str) or not selector.strip():
            raise BrowserServiceError("selector 必须是非空字符串。")
        return RenderLocator(
            self,
            self.__page.locator(
                selector, **_locator_filters(self, has_text, has_not_text, has, has_not)
            ),
        )

    @_render_action
    async def content(self) -> str:
        return await self.__page.content()

    @_render_action
    async def title(self) -> str:
        return await self.__page.title()

    @_render_action
    async def set_viewport_size(self, viewport_size: dict[str, int]) -> None:
        viewport = BrowserService._validate_viewport(viewport_size)
        if (
            viewport["width"] * viewport["height"] * self.__device_scale_factor**2
            > MAX_VIEWPORT_PIXELS
        ):
            raise BrowserServiceError(
                f"视口乘设备像素比后最多允许 {MAX_VIEWPORT_PIXELS} 像素。"
            )
        await self.__page.set_viewport_size(viewport)

    @_render_action
    async def emulate_media(
        self,
        *,
        media: Literal["null", "print", "screen"] | None = None,
        color_scheme: Literal["dark", "light", "no-preference", "null"] | None = None,
        reduced_motion: Literal["no-preference", "null", "reduce"] | None = None,
        forced_colors: Literal["active", "none", "null"] | None = None,
        contrast: Literal["more", "no-preference", "null"] | None = None,
    ) -> None:
        await self.__page.emulate_media(
            media=media,
            color_scheme=color_scheme,
            reduced_motion=reduced_motion,
            forced_colors=forced_colors,
            contrast=contrast,
        )

    @_render_action
    async def wait_for_load_state(
        self,
        state: Literal["domcontentloaded", "load", "networkidle"] = "load",
        *,
        timeout: float | None = None,
    ) -> None:
        await self.__page.wait_for_load_state(state, timeout=timeout)

    @_render_action
    async def screenshot(
        self,
        *,
        type: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        animations: Literal["allow", "disabled"] = "allow",
        timeout: float | None = None,
        quality: int | None = None,
        omit_background: bool | None = None,
        clip: dict[str, float] | None = None,
        caret: Literal["hide", "initial"] | None = None,
        scale: Literal["css", "device"] | None = None,
        mask: Sequence[RenderLocator] | None = None,
        mask_color: str | None = None,
        style: str | None = None,
    ) -> bytes:
        """Return image bytes without accepting host file paths or handles."""
        _validate_screenshot(type, animations)
        if not isinstance(full_page, bool):
            raise BrowserServiceError("full_page 必须是布尔值。")
        native_mask = _native_masks(mask, self)
        return await _screenshot_bytes(
            self.__page.screenshot(
                type=type,
                full_page=full_page,
                animations=animations,
                timeout=timeout,
                quality=quality,
                omit_background=omit_background,
                clip=clip,
                caret=caret,
                scale=scale,
                mask=native_mask,
                mask_color=mask_color,
                style=style,
            ),
            timeout,
        )

    @_render_action
    async def pdf(
        self,
        *,
        scale: float | None = None,
        display_header_footer: bool | None = None,
        header_template: str | None = None,
        footer_template: str | None = None,
        print_background: bool | None = None,
        landscape: bool | None = None,
        page_ranges: str | None = None,
        format: str | None = None,
        width: str | float | None = None,
        height: str | float | None = None,
        prefer_css_page_size: bool | None = None,
        margin: dict[str, str | float] | None = None,
        outline: bool | None = None,
        tagged: bool | None = None,
    ) -> bytes:
        """Print this page to bytes; host file paths are deliberately excluded."""
        return await self.__page.pdf(
            scale=scale,
            display_header_footer=display_header_footer,
            header_template=header_template,
            footer_template=footer_template,
            print_background=print_background,
            landscape=landscape,
            page_ranges=page_ranges,
            format=format,
            width=width,
            height=height,
            prefer_css_page_size=prefer_css_page_size,
            margin=margin,
            outline=outline,
            tagged=tagged,
        )


class RenderLocator:
    """Expose same-page element queries, layout and screenshot bytes."""

    __slots__ = ("__owner", "__locator")

    def __init__(self, owner: RenderPage, locator: Locator) -> None:
        self.__owner = owner
        self.__locator = locator

    def _check_active(self) -> None:
        self.__owner._check_active()

    @property
    def page(self) -> RenderPage:
        self._check_active()
        return self.__owner

    @property
    def first(self) -> RenderLocator:
        self._check_active()
        return RenderLocator(self.__owner, self.__locator.first)

    @property
    def last(self) -> RenderLocator:
        self._check_active()
        return RenderLocator(self.__owner, self.__locator.last)

    def nth(self, index: int) -> RenderLocator:
        self._check_active()
        return RenderLocator(self.__owner, self.__locator.nth(index))

    def locator(
        self,
        selector_or_locator: str | RenderLocator,
        *,
        has_text: TextFilter = None,
        has_not_text: TextFilter = None,
        has: RenderLocator | None = None,
        has_not: RenderLocator | None = None,
    ) -> RenderLocator:
        self._check_active()
        selector = (
            selector_or_locator
            if isinstance(selector_or_locator, str)
            else _native_locator(selector_or_locator, self.__owner)
        )
        return RenderLocator(
            self.__owner,
            self.__locator.locator(
                selector,
                **_locator_filters(self.__owner, has_text, has_not_text, has, has_not),
            ),
        )

    def filter(
        self,
        *,
        has_text: TextFilter = None,
        has_not_text: TextFilter = None,
        has: RenderLocator | None = None,
        has_not: RenderLocator | None = None,
        visible: bool | None = None,
    ) -> RenderLocator:
        self._check_active()
        return RenderLocator(
            self.__owner,
            self.__locator.filter(
                **_locator_filters(self.__owner, has_text, has_not_text, has, has_not),
                visible=visible,
            ),
        )

    @_render_action
    async def all(self) -> list[RenderLocator]:
        return [
            RenderLocator(self.__owner, item) for item in await self.__locator.all()
        ]

    @_render_action
    async def count(self) -> int:
        return await self.__locator.count()

    @_render_action
    async def inner_text(self, *, timeout: float | None = None) -> str:
        return await self.__locator.inner_text(timeout=timeout)

    @_render_action
    async def text_content(self, *, timeout: float | None = None) -> str | None:
        return await self.__locator.text_content(timeout=timeout)

    @_render_action
    async def inner_html(self, *, timeout: float | None = None) -> str:
        return await self.__locator.inner_html(timeout=timeout)

    @_render_action
    async def get_attribute(
        self, name: str, *, timeout: float | None = None
    ) -> str | None:
        return await self.__locator.get_attribute(name, timeout=timeout)

    @_render_action
    async def is_visible(self, *, timeout: float | None = None) -> bool:
        return await self.__locator.is_visible(timeout=timeout)

    @_render_action
    async def is_hidden(self, *, timeout: float | None = None) -> bool:
        return await self.__locator.is_hidden(timeout=timeout)

    @_render_action
    async def wait_for(
        self,
        *,
        state: Literal["attached", "detached", "hidden", "visible"] = "visible",
        timeout: float | None = None,
    ) -> None:
        await self.__locator.wait_for(state=state, timeout=timeout)

    @_render_action
    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        await self.__locator.scroll_into_view_if_needed(timeout=timeout)

    @_render_action
    async def evaluate(
        self, expression: str, arg: Any = None, *, timeout: float | None = None
    ) -> Any:
        """Evaluate on the matched element without creating a JSHandle."""
        _validate_evaluation(expression, arg)
        return await self.__locator.evaluate(expression, arg, timeout=timeout)

    @_render_action
    async def evaluate_all(self, expression: str, arg: Any = None) -> Any:
        _validate_evaluation(expression, arg)
        return await self.__locator.evaluate_all(expression, arg)

    @_render_action
    async def bounding_box(
        self, *, timeout: float | None = None
    ) -> dict[str, float] | None:
        """Return the matched element's position and dimensions as plain data."""
        return await self.__locator.bounding_box(timeout=timeout)

    @_render_action
    async def screenshot(
        self,
        *,
        type: Literal["png", "jpeg"] = "png",
        animations: Literal["allow", "disabled"] = "allow",
        timeout: float | None = None,
        quality: int | None = None,
        omit_background: bool | None = None,
        caret: Literal["hide", "initial"] | None = None,
        scale: Literal["css", "device"] | None = None,
        mask: Sequence[RenderLocator] | None = None,
        mask_color: str | None = None,
        style: str | None = None,
    ) -> bytes:
        """Return element image bytes without file paths or native objects."""
        _validate_screenshot(type, animations)
        native_mask = _native_masks(mask, self.__owner)

        async def capture() -> bytes:
            # Resolve explicitly so timeout=0 also disables the locating timeout.
            # The native handle remains internal and is always disposed.
            handle = await self.__locator.element_handle(timeout=timeout)
            if handle is None:
                raise BrowserServiceError("未找到渲染截图元素。")
            try:
                return await handle.screenshot(
                    type=type,
                    animations=animations,
                    timeout=timeout,
                    quality=quality,
                    omit_background=omit_background,
                    caret=caret,
                    scale=scale,
                    mask=native_mask,
                    mask_color=mask_color,
                    style=style,
                )
            finally:
                await handle.dispose()

        return await _screenshot_bytes(capture(), timeout)


class BrowserServiceAPI:
    """Publish an explicit rendering API, including wrapped pages and locators.

    Private Python attributes are implementation details, not a security boundary
    against malicious code running in the same process.
    """

    __slots__ = ("__service", "__enabled")

    def __init__(self, service: BrowserService, *, enabled: bool = True) -> None:
        self.__service = service
        self.__enabled = enabled

    @property
    def ready(self) -> bool:
        """Whether the managed service can accept a new request."""
        return self.__enabled and self.__service.ready

    def _require_enabled(self) -> None:
        if not self.__enabled:
            raise BrowserServiceError(
                "共享渲染服务已关闭。请在插件的共享渲染服务配置组中开启并重载插件。"
            )

    @asynccontextmanager
    async def session(
        self,
        *,
        viewport: dict[str, int] | None = None,
        javascript_enabled: bool = False,
        device_scale_factor: float = 1,
        timeout: int | float = 30,
    ) -> AsyncIterator[RenderPage]:
        """Yield a render wrapper and invalidate it on every session exit."""
        self._require_enabled()
        async with self.__service.session(
            viewport=viewport,
            javascript_enabled=javascript_enabled,
            device_scale_factor=device_scale_factor,
            timeout=timeout,
        ) as page:
            wrapped = RenderPage(page, device_scale_factor)
            try:
                yield wrapped
            finally:
                wrapped._invalidate()

    async def render_html(
        self,
        html: str,
        *,
        viewport: dict[str, int] | None = None,
        selector: str | None = None,
        full_page: bool = True,
        javascript_enabled: bool = False,
        timeout: int | float = 30,
        max_pixels: int | None = MAX_SCREENSHOT_PIXELS,
    ) -> bytes:
        """Render offline HTML as PNG bytes."""
        self._require_enabled()
        return await self.__service.render_html(
            html,
            viewport=viewport,
            selector=selector,
            full_page=full_page,
            javascript_enabled=javascript_enabled,
            timeout=timeout,
            max_pixels=max_pixels,
        )

    async def render_pdf(
        self,
        html: str,
        *,
        format: str = "A4",
        landscape: bool = False,
        print_background: bool = True,
        prefer_css_page_size: bool = True,
        margin: dict[str, str] | None = None,
        viewport: dict[str, int] | None = None,
        javascript_enabled: bool = False,
        timeout: int | float = 30,
    ) -> bytes:
        """Render offline HTML as PDF bytes."""
        self._require_enabled()
        return await self.__service.render_pdf(
            html,
            format=format,
            landscape=landscape,
            print_background=print_background,
            prefer_css_page_size=prefer_css_page_size,
            margin=margin,
            viewport=viewport,
            javascript_enabled=javascript_enabled,
            timeout=timeout,
        )
