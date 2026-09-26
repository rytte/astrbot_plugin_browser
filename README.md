# 浏览器服务插件

给 AstrBot 插件提供一个可复用的本地 Chromium 渲染服务。HTML 截图、PDF 生成、Markdown 卡片，这些任务都可以交给同一个浏览器进程完成。

它启动的是**无头 Chromium**，不会弹出浏览器窗口；每个调用都会创建独立的 BrowserContext，任务结束后自动释放上下文。

## ✨ 为什么需要它

### 一个浏览器，多个插件共享

浏览器是比较占资源的组件。如果 Markdown 渲染、网页卡片、报告导出等插件各自启动一套 Chromium，很快就会出现多个浏览器进程、重复的 Playwright 驱动和重复的缓存。

本插件只维护一个共享的 Chromium 进程：

```text
AstrBot
  ├─ Markdown 渲染插件 ─┐
  ├─ 报告导出插件      ─┼─> 共享 Browser(Chromium) ─> 独立 BrowserContext
  └─ 网页卡片插件      ─┘
```

这样可以减少重复的浏览器进程和基础开销，通常比每个插件各自启动浏览器更省内存。`max_concurrent_sessions` 还可以限制同时运行的页面任务数量，避免并发渲染把内存一次性推高。

共享不等于没有成本：Chromium 本身仍然需要内存，每个并发页面也会占用额外资源。插件通过会话队列、超时和像素上限把这笔开销控制在可预期范围内。

#### 浏览器内存结构示例

```bash
Chromium（浏览器实例）									# 150～300 MB
└── BrowserContext（独立会话：Cookie、登录状态、缓存等）  # 10～60 MB
    ├── Page（标签页）								 # 10～80 MB
    └── Page（标签页）								 # 10～80 MB
```

### 任务之间互相隔离

每项任务使用独立的 BrowserContext，Cookie、缓存、Local Storage 和页面状态不会在任务之间共享。任务结束时上下文自动关闭，调用方不需要手动清理页面。

### HTML 截图和 PDF 一套接口

- HTML 渲染为 PNG 字节，可直接交给 `Image.fromBytes()`。
- HTML 渲染为 PDF 字节，可直接保存或交给文件消息组件。
- 支持完整 CSS、字体等待、元素选择器截图和整页截图。

### 默认离线，更适合机器人内容

- 浏览器使用无头模式运行。
- 页面默认禁用 JavaScript。
- 页面请求默认全部拦截，不访问外部网站、外部图片或本地文件。
- 不提供任意网址导航、持久化登录资料或面向模型的浏览器工具。

如果业务确实需要脚本，可以只对单个 `session()` 调用设置 `javascript_enabled=True`。这不会改变其他任务的默认设置。

## 🧩 安装

要求：

- AstrBot `>=4.27,<5`
- Python 环境中的 Playwright `>=1.58,<2`

将插件目录放入：

```text
AstrBot/data/plugins/astrbot_plugin_browser/
```

在运行 AstrBot 的同一个 Python 环境中安装 Python 依赖：

```bash
python -m pip install -r data/plugins/astrbot_plugin_browser/requirements.txt
```

插件启动时会自动按以下顺序准备 Chromium：

1. 使用 Playwright 已安装的 Chromium。
2. 查找本机可用的 Chromium、Chrome、Edge、Brave、Vivaldi 或 Opera。
3. 如果仍未找到，自动执行 `python -m playwright install chromium`。

因此通常不需要手动执行浏览器安装命令。自动下载需要网络和当前 Python 环境的写入权限。

Linux 如果自动下载后仍缺少浏览器系统依赖，可以手动安装：

```bash
python -m playwright install --with-deps chromium
```

安装完成后，在 AstrBot 插件管理页面加载并启用“浏览器服务”。插件加载时会启动 Chromium，插件卸载或重载时会关闭 Chromium。

## 🔌 获取服务

其他插件通过 AstrBot 的插件上下文获取已注册的浏览器服务：

```python
from astrbot.api.star import Star


class ExamplePlugin(Star):
    def get_browser(self):
        metadata = self.context.get_registered_star("astrbot_plugin_browser")
        if (
            metadata is None
            or not metadata.activated
            or metadata.star_cls is None
        ):
            raise RuntimeError("请先启用 astrbot_plugin_browser")

        browser = metadata.star_cls.service
        if not browser.ready:
            raise RuntimeError("浏览器服务尚未就绪")
        return browser
```

浏览器服务的生命周期由本插件管理。调用方只使用 `service`，不要调用 `initialize()`、`close()`，也不要关闭共享的 Browser 或 Playwright 实例。

### 接口一：`render_html()`

把完整 HTML 渲染为 PNG 字节：

```python
browser = self.get_browser()

png = await browser.render_html(
    """
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8">
        <style>
          body { margin: 0; padding: 32px; font-size: 24px; }
          .card { padding: 24px; border: 1px solid #ddd; }
        </style>
      </head>
      <body><main class="card">本地 HTML 截图</main></body>
    </html>
    """,
    viewport={"width": 900, "height": 720},
    selector=".card",
)
```

参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `html` | 必填 | 完整 HTML 字符串，最大 48 MiB |
| `viewport` | `1280x720` | CSS 视口，宽高均为 1～8192；面积最多约 1678 万像素 |
| `selector` | `None` | 指定元素截图；元素不存在或不可见时抛出 `BrowserServiceError` |
| `full_page` | `True` | 没有 `selector` 时是否截取完整页面 |
| `javascript_enabled` | `False` | 是否为本次页面启用 JavaScript |
| `timeout` | `30` | 本次任务超时时间，范围 0.1～300 秒 |
| `max_pixels` | `32000000` | 整页或目标元素的最大截图面积；传 `None` 可关闭该项检查 |

返回值是 PNG `bytes`：

```python
from astrbot.api.message_components import Image

image = Image.fromBytes(png)
```

指定 `selector` 时只截取目标元素；不指定时，`full_page=True` 会截取整个页面，`full_page=False` 只截取当前视口。

### 接口二：`render_pdf()`

把完整 HTML 渲染为 PDF 字节：

```python
browser = self.get_browser()

pdf = await browser.render_pdf(
    "<html><body><h1>报告</h1><p>本地 PDF 内容</p></body></html>",
    format="A4",
    print_background=True,
    margin={
        "top": "10mm",
        "right": "10mm",
        "bottom": "10mm",
        "left": "10mm",
    },
)
```

参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `html` | 必填 | 完整 HTML 字符串，最大 48 MiB |
| `format` | `A4` | Chromium 纸张格式，例如 `A4`、`Letter`、`Legal` |
| `landscape` | `False` | 是否横向排版 |
| `print_background` | `True` | 是否打印 CSS 背景 |
| `prefer_css_page_size` | `True` | 是否优先使用 CSS `@page` 尺寸 |
| `margin` | `None` | 页边距字典，值使用 CSS 单位，例如 `8mm`、`1cm` |
| `viewport` | `1280x720` | 页面布局使用的 CSS 视口 |
| `javascript_enabled` | `False` | 是否为本次页面启用 JavaScript |
| `timeout` | `30` | 本次任务超时时间，范围 0.1～300 秒 |

返回值是 PDF `bytes`。服务不会替调用方写入文件：

```python
from pathlib import Path

Path("report.pdf").write_bytes(pdf)
```

### 接口三：`session()`

需要自行测量 DOM、分页或控制截图细节时，使用页面会话接口：

```python
browser = self.get_browser()

async with browser.session(
    viewport={"width": 1200, "height": 2100},
    javascript_enabled=False,
    device_scale_factor=1,
    timeout=30,
) as page:
    await page.set_content(html, wait_until="load")
    await page.evaluate("document.fonts.ready")

    height = await page.locator("#content").evaluate(
        "element => element.scrollHeight"
    )
    png = await page.locator("#content").screenshot(
        type="png",
        animations="disabled",
    )
```

`session()` 参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `viewport` | `1280x720` | CSS 视口尺寸 |
| `javascript_enabled` | `False` | 仅影响当前会话 |
| `device_scale_factor` | `1` | 设备像素比，范围 0.5～3 |
| `timeout` | `30` | Playwright 默认操作超时时间，范围 0.1～300 秒 |

会话退出时，页面和 BrowserContext 会自动关闭。即使截图或业务代码抛出异常，也会执行清理。

## ⚙️ 配置

在插件配置页面修改以下选项：

| 配置项 | 默认值 | 范围 | 说明 |
|---|---:|---:|---|
| `browser_executable` | 空 | 绝对路径 | 留空自动选择 Playwright Chromium 或本机 Chromium 系浏览器；也可以填写已有浏览器的绝对路径，不要加引号 |
| `max_concurrent_sessions` | `4` | 1～16 | 同时运行的隔离页面数量；超过后进入队列 |
| `queue_timeout` | `30` 秒 | 1～300 | 等待并发名额的最长时间 |
| `startup_timeout` | `30` 秒 | 1～120 | 启动 Chromium 的最长时间 |

浏览器固定以无头模式启动。配置 `browser_executable` 只会改变使用的浏览器程序，不会让它弹出窗口。显式配置的路径始终优先，路径无效或启动失败时不会自动切换。

## 🛡️ 默认边界和限制

- 页面请求默认全部中止，因此外部 CSS、图片、字体和脚本不会被加载。
- 页面默认禁用 JavaScript；需要时只能由调用方对单个任务显式开启。
- 每个任务使用独立 BrowserContext，不共享 Cookie、缓存和存储。
- HTML 输入最大 48 MiB。
- 单个视口最多约 1678 万像素。
- `render_html()` 的 PNG 截图默认最多 3200 万像素；使用低层 `session()` 时，调用方需要自行控制截图尺寸。
- 页面会话和渲染方法都受超时保护。
- 所有调用失败都会抛出 `BrowserServiceError`，调用方应向用户返回可理解的错误，而不是无限重试。

## 🧯 常见问题

### 启动失败，提示安装 Chromium

插件会在启动时自动安装 Chromium。如果自动安装失败，请确认网络和写入权限，并使用启动 AstrBot 的同一个 Python 环境手动执行：

```bash
python -m playwright install chromium
```

如果配置了 `browser_executable`，插件只会启动这个路径指向的浏览器；路径无效或启动失败时不会自动切换。

### 为什么没有浏览器窗口？

这是预期行为。服务使用无头 Chromium，适合后台机器人长期运行，也不会在服务器桌面弹出窗口。

### 为什么外部图片显示不出来？

服务默认拦截所有页面请求，只允许 HTML 内联内容参与渲染。请将资源内联，或由调用方在生成 HTML 前自行处理资源。

### 为什么任务排队或超时？

当前并发数达到 `max_concurrent_sessions` 后，新任务会等待队列名额；超过 `queue_timeout` 就会失败。可以适当调大并发数，但并发越高，内存占用也会随之增加。

## 📜 许可与定位

这是一个供 AstrBot 其他插件调用的基础服务插件，不提供聊天命令，也不直接面向模型开放浏览器控制能力。它负责统一管理 Chromium 的启动、隔离、限流和回收，让上层插件专注于生成 HTML 和处理渲染结果。
