# 浏览器服务与 Agent 网页工具

让 AstrBot 的 Agent 通过模型工具操作真实网页：打开网站、观察页面、点击、填写表单、滚动和切换标签页。插件也为其他 AstrBot 插件提供 HTML 截图和 PDF 渲染服务，两类任务共用一个 Chromium 进程。

它启动的是**无头 Chromium**，不会弹出浏览器窗口。Agent 会话按“消息来源＋发送者”隔离，并跨多轮工具调用保留页面和登录状态；离线渲染任务使用临时 BrowserContext，结束后释放。

## 🤖 Agent 如何使用

启用插件，在插件配置的 **BrowserUse** 组中打开“启用 BrowserUse 模型工具”（默认开启），并在 AstrBot 的工具管理中为目标人格启用所需的 `browser_*` 工具。模型需要支持工具调用；显式配置提供商 `modalities` 时应包含 `tool_use`。随后可以直接提出任务，例如：

> 打开这个网站，搜索无线键盘，按价格排序，整理前三项结果。

Agent 使用 AstrBot 自身的工具调用循环决定下一步，插件负责执行操作并返回真实页面状态。

| 工具 | 用途 |
|---|---|
| `browser_open(url, new_tab=False)` | 打开完整 HTTP/HTTPS 网址；首次调用创建个人会话 |
| `browser_observe()` | 返回网址、标题、文字、元素清单及新的 `observation_id` |
| `browser_screenshot()` | 将当前视口 PNG 和页面观察返回给支持视觉的模型 |
| `browser_click(observation_id, ref="", x=-1, y=-1)` | 按元素编号点击，或按最新截图的坐标点击；两种目标互斥 |
| `browser_fill(observation_id, ref, text)` | 替换输入框或可编辑区域的内容，空字符串用于清空 |
| `browser_press(observation_id, key)` | 在当前焦点按键，例如 `Enter`、`Tab`、`Control+a` |
| `browser_select(observation_id, ref, value)` | 按观察结果中的 `value` 选择原生下拉框选项 |
| `browser_scroll(observation_id, direction="down", pixels=600)` | 向指定方向滚动，`pixels` 为 1～5000 的整数 |
| `browser_wait(text)` | 等待主页面出现指定的可见文字 |
| `browser_tabs(operation="list", tab_id="")` | 列出、切换或关闭标签页，操作为 `list` / `switch` / `close` |
| `browser_close()` | 关闭个人会话和全部标签页，清除本次登录状态 |

### 观察与操作

`browser_open()` 和页面动作会返回新的观察结果，包括 `observation_id`、当前标签页、各 frame 的页面文字及元素信息。例如：

```json
{
  "observation_id": "obs_...",
  "active_tab": "tab_1",
  "frames": [{
    "text": "商品搜索……",
    "elements": [
      {"ref": "e1", "tag": "input", "name": "搜索商品"},
      {"ref": "e2", "tag": "button", "name": "搜索"}
    ]
  }]
}
```

模型使用这次观察中的编号填写 `e1`，再根据返回的新观察点击搜索按钮。元素编号只在当前观察中有效；重新观察、截图、操作、切换标签页之后，旧的 `observation_id` 会被拒绝。目标元素被网页替换或更名时，也需要重新观察。

观察支持 iframe 和开放的 Shadow DOM。单次结果最多返回 16 个 frame、150 个可操作元素、合计 16000 字符的正文前缀与 `viewport_text`（视口文字），元素描述另有总长度限制。结果优先列出视口内元素，并明确标注截断情况；滚动后可重新观察后面的内容。密码输入框的值不会出现在元素清单中。

### 截图与坐标

截图以图片内容进入模型上下文，不会自动发送到聊天中。需要使用支持图片输入的模型，并在 AstrBot 提供商配置的 `modalities` 中显式启用 `image`；未配置时返回 `VISION_UNAVAILABLE`，不会用图片路径代替模型可见的图片。

截图固定为当前 1280×720 视口，使用 CSS 像素。坐标点击必须带上对应截图的 `observation_id`；检测到 DOM、滚动位置、网址或视口发生变化时要求重新截图。Canvas 动画等不产生 DOM 变化的视觉更新无法被完整检测，操作动态界面时应及时重新截图。

### 会话和错误

- 同一消息来源中的不同发送者拥有独立的 Cookie、存储和页面；模型不能指定或访问其他用户的会话。
- 会话默认保留至空闲 600 秒，并由周期任务回收。关闭最后一个标签页、调用 `browser_close()` 或卸载插件也会释放会话。
- 每个会话最多 8 个标签页。网页弹出的标签页会出现在列表中，由模型明确切换。
- 登录态仅保留在运行中的 Context 内，插件或 AstrBot 重启后不保留。
- Agent 页面启用 JavaScript 并允许 HTTP/HTTPS 请求；网络访问使用 AstrBot 所在主机的网络环境。
- JavaScript 的 alert/confirm/prompt 会被关闭，并在结果的 `notices` 中说明。文件上传下载、拖拽和人工登录接管尚未提供模型工具。
- 失败结果包含 `success=false` 及 `error.code`、`error.message`。元素失效时重新观察；工具超时后操作可能已执行，应先观察确认，避免重复提交。
- 浏览器重启会返回 `SESSION_LOST`；原会话不自动重放，模型需要重新打开网站。

模型工具在 AstrBot 进程内运行。网页文字作为任务资料使用，任务目标来自用户。

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

这样可以减少重复的浏览器进程和基础开销，通常比每个插件各自启动浏览器更省内存。`browser.max_concurrent_sessions` 还可以限制同时运行的页面任务数量，避免并发渲染把内存一次性推高。

共享不等于没有成本：Chromium 本身仍然需要内存，每个并发页面也会占用额外资源。插件通过任务排队、超时和像素上限把这笔开销控制在可预期范围内。

#### 浏览器内存结构示例

```bash
Chromium（浏览器实例）									# 150～300 MB
└── BrowserContext（独立会话：Cookie、登录状态、缓存等）  # 10～60 MB
    ├── Page（标签页）								 # 10～80 MB
    └── Page（标签页）								 # 10～80 MB
```

### 会话与渲染任务相互隔离

每个 Agent 使用者及每个离线渲染任务使用独立的 BrowserContext。不同上下文之间不共享 Cookie、Local Storage 和页面状态；同一 Agent 会话内部保留状态，便于连续操作。

### HTML 截图和 PDF 一套接口

- HTML 渲染为 PNG 字节，可直接交给 `Image.fromBytes()`。
- HTML 渲染为 PDF 字节，可直接保存或交给文件消息组件。
- 支持完整 CSS、字体等待、元素选择器截图和整页截图。

### 离线渲染接口

- 浏览器使用无头模式运行。
- `render_html()`、`render_pdf()` 和 `session()` 默认禁用 JavaScript。
- 这些渲染接口的页面请求全部拦截，不访问外部网站、外部图片或本地文件。
- Agent 工具使用独立的联网会话，不改变渲染接口的离线约定。

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

插件配置分为两组。**浏览器基础设置**（`browser`）由 BrowserUse 和离线渲染共用：

| 配置项 | 默认值 | 范围 | 说明 |
|---|---:|---:|---|
| `browser.browser_executable` | 空 | 绝对路径 | 留空自动选择 Playwright Chromium 或本机 Chromium 系浏览器；也可以填写已有浏览器的绝对路径，不要加引号 |
| `browser.max_concurrent_sessions` | `4` | 1～16 | 离线渲染与 BrowserUse 操作共享的执行并发；空闲会话不占执行名额 |
| `browser.queue_timeout` | `30` 秒 | 1～300 | 任务排队超时；执行名额满时等待的最长时间 |
| `browser.startup_timeout` | `30` 秒 | 1～120 | 启动共用 Chromium 的最长时间 |

**BrowserUse**（`browser_use`）组只放 Agent 网页工具的专用配置：

| 配置项 | 默认值 | 范围 | 说明 |
|---|---:|---:|---|
| `browser_use.enabled` | `true` | 布尔值 | 是否向模型提供 BrowserUse 工具 |
| `browser_use.max_sessions` | `4` | 1～16 | 同时保留的 Agent 联网会话上限，与执行并发分别限制 |
| `browser_use.idle_timeout` | `600` 秒 | 30～3600 | Agent 会话空闲回收时间 |
| `browser_use.action_timeout` | `30` 秒 | 1～120 | 单次 Agent 工具总时限，包括排队、操作和观察 |

保存配置后重载插件生效。关闭 BrowserUse 后，全部 11 个 `browser_*` 工具从可用工具列表中移除，工具定义不会发送给模型；缓存的直接调用也会返回 `BROWSER_USE_DISABLED`。重载会关闭原有 Agent 会话并清除登录状态。离线渲染服务仍然可用，工具管理中的单项启停设置保持不变。

原顶层浏览器路径、并发、排队和启动超时配置已移入 `browser` 组；原顶层 `max_agent_sessions`、`agent_idle_timeout`、`agent_action_timeout` 已移入 `browser_use` 组并更名。手动配置请使用表中的新路径，不提供旧字段别名；若将旧字段直接传入插件，会报未知配置错误。AstrBot 配置加载器可能按新 schema 清理旧字段并补入默认值，升级前请记录旧值，再在新组中重新设置。

浏览器固定以无头模式启动。配置 `browser.browser_executable` 只会改变使用的浏览器程序，不会让它弹出窗口。显式配置的路径始终优先，路径无效或启动失败时不会自动切换。

## 🛡️ 默认边界和限制

- 离线渲染接口中止页面请求，因此外部 CSS、图片、字体和脚本不会被加载。
- 离线渲染接口默认禁用 JavaScript；调用方可以对单个任务显式开启。
- Agent 会话启用 JavaScript、允许联网，并独立保留 Cookie 和存储。
- HTML 输入最大 48 MiB。
- 单个视口最多约 1678 万像素。
- `render_html()` 的 PNG 截图默认最多 3200 万像素；使用低层 `session()` 时，调用方需要自行控制截图尺寸。
- `session()` 设置 Playwright 默认操作时限；`render_html()`、`render_pdf()` 和 Agent 工具另有整体超时保护。
- Python 渲染接口失败抛出 `BrowserServiceError`；Agent 工具失败返回带错误码的结果。

## 🧯 常见问题

### 启动失败，提示安装 Chromium

插件会在启动时自动安装 Chromium。如果自动安装失败，请确认网络和写入权限，并使用启动 AstrBot 的同一个 Python 环境手动执行：

```bash
python -m playwright install chromium
```

如果配置了 `browser.browser_executable`，插件只会启动这个路径指向的浏览器；路径无效或启动失败时不会自动切换。

### 为什么没有浏览器窗口？

这是预期行为。服务使用无头 Chromium，适合后台机器人长期运行，也不会在服务器桌面弹出窗口。

### 为什么外部图片显示不出来？

离线渲染接口拦截所有页面请求，只允许 HTML 内联内容参与渲染。请将资源内联，或由调用方在生成 HTML 前自行处理资源。Agent 联网页面的资源则由网页正常请求。

### 为什么任务排队或超时？

离线渲染和 BrowserUse 共用执行名额。当前并发数达到 `browser.max_concurrent_sessions` 后，新任务会排队等待；超过 `browser.queue_timeout` 就会失败。BrowserUse 还受 `browser_use.action_timeout` 的整体时限约束，其中包含排队、页面操作和结果观察。可以适当调大并发数，但并发越高，内存占用也会随之增加。

## 🧪 开发验证

在已安装 AstrBot 依赖的同一 Python 环境中运行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
```

`tests/test_agent_browser.py` 自动选择已安装的 Chromium，启动本地测试网站验证搜索、表单、多标签页、iframe、Shadow DOM、截图坐标、会话隔离和取消清理；没有浏览器时明确跳过。`tests/test_service.py` 的额外 PNG/PDF 冒烟测试通过 `ASTRBOT_BROWSER_EXECUTABLE` 指定浏览器路径启用。测试不调用外部模型 API；截图结果另外经过 AstrBot 的真实工具执行器和 Agent 循环，验证图片进入下一次模型请求。

## 📜 定位

这是一个面向 AstrBot Agent 和其他插件的浏览器基础服务：Agent 通过工具观察和操作网页，上层插件通过 Python 接口执行离线渲染。插件统一管理 Chromium 的启动、隔离、执行并发和会话回收。
