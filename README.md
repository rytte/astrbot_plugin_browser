# 浏览器服务

让 AstrBot 的 Agent 通过模型工具操作真实网页：观察与截图、鼠标和表单操作、文件上传下载、结构化提取、弹窗处理、登录状态保存和多标签页。插件也为其他 AstrBot 插件提供 HTML 截图和 PDF 渲染服务，两类任务共用一个 Chromium 进程。

它启动的是**无头 Chromium**，不会弹出浏览器窗口。Agent 会话按“消息来源＋发送者”隔离，并跨多轮工具调用保留页面和登录状态；离线渲染任务使用临时 BrowserContext，结束后释放。

## 🤖 Agent 如何使用

启用插件，在插件配置的 **BrowserUse** 组中打开“启用 BrowserUse 模型工具”（默认开启），选择“模型工具层级”（默认“基础浏览”，三个选项均以中文和工具数量显示），并在 AstrBot 的工具管理中为目标人格启用所需的 `browser_*` 工具。模型需要支持工具调用；显式配置提供商 `modalities` 时应包含 `tool_use`。随后可以直接提出任务，例如：

> 打开这个网站，搜索无线键盘，按价格排序，整理前三项结果。

Agent 使用 AstrBot 自身的工具调用循环决定下一步，插件负责执行操作并返回真实页面状态。工具分为三个累计层级，高层包含低层全部工具；未选层级的工具定义会从可用列表中移除，不占用模型上下文。

| 配置值 | 层级 | 工具总数 | 适用任务 |
|---|---|---:|---|
| `basic`（默认） | 第一层：基础浏览 | 12 | 搜索、阅读、表单填写、菜单和标签页操作 |
| `standard` | 第二层：完整交互 | 18 | 增加拖拽、文件上传下载、结构化提取、弹窗和登录状态管理 |
| `full` | 第三层：高级控制 | 22 | 增加网络监听、请求拦截、设备环境模拟和主动执行 JavaScript |

### 第一层：基础浏览

| 工具 | 用途 |
|---|---|
| `browser_open(url, new_tab=False)` | 打开完整 HTTP/HTTPS 网址；首次调用创建个人会话 |
| `browser_observe()` | 返回网址、标题、文字、元素清单及新的 `observation_id` |
| `browser_screenshot()` | 将当前视口 PNG 和页面观察返回给支持视觉的模型 |
| `browser_click(observation_id, ref="", x=-1, y=-1, button="left", click_count=1)` | 按元素编号或最新截图坐标点击；支持左键、右键、中键及双击，两种目标互斥 |
| `browser_hover(observation_id, ref="", x=-1, y=-1)` | 移动鼠标并悬停，展开菜单或提示；使用元素编号或最新截图坐标 |
| `browser_fill(observation_id, ref, text)` | 替换输入框或可编辑区域的内容，空字符串用于清空 |
| `browser_press(observation_id, key)` | 在当前焦点按键，例如 `Enter`、`Tab`、`Control+a` |
| `browser_select(observation_id, ref, value)` | 按观察结果中的 `value` 选择原生下拉框选项 |
| `browser_scroll(observation_id, direction="down", pixels=600)` | 向指定方向滚动，`pixels` 为 1～5000 的整数 |
| `browser_wait(condition, value, state="", frame_index=0)` | 等待文字、CSS 元素、网址或页面加载状态；文字和元素支持出现/消失 |
| `browser_tabs(operation="list", tab_id="")` | 列出、切换或关闭标签页，操作为 `list` / `switch` / `close` |
| `browser_close()` | 自动保存登录快照后关闭个人会话和全部标签页 |

### 第二层：完整交互

设置 `browser_use.tool_level="standard"`，在基础浏览的 12 个工具上增加以下 6 个：

| 工具 | 用途 |
|---|---|
| `browser_drag(observation_id, source_ref="", target_ref="", start_x=-1, start_y=-1, end_x=-1, end_y=-1)` | 使用两个元素编号或四个截图坐标，按住左键从起点拖到终点后释放；两种模式不能混用 |
| `browser_upload(operation="list", observation_id="", ref="", file_ids=[])` | 导入当前消息附件并列出个人文件，或向文件输入框上传/清空文件 |
| `browser_download(operation="list", file_id="")` | 查看个人文件、等待下载、发送到当前聊天或删除文件 |
| `browser_extract(kind="tables", selector="", frame_index=0, limit=100)` | 提取表格、链接、列表或指定区域正文 |
| `browser_dialog(operation="list", dialog_type="", message="", prompt_text="")` | 查询弹窗历史，预设下一次弹窗的接受/取消和 prompt 输入 |
| `browser_state(operation="status")` | 查询、显式保存、恢复或删除个人登录状态 |

### 第三层：高级控制

设置 `browser_use.tool_level="full"`，在完整交互的 18 个工具上增加以下 4 个：

| 工具 | 用途 |
|---|---|
| `browser_network(operation="list", request_id="", url_pattern="", limit=50, include_body=False)` | 开始/停止网络监听，查询请求、状态、失败原因及文本响应 |
| `browser_intercept(operation="list", rule_id="", rule={})` | 屏蔽请求、修改请求头/网址/方法/正文、模拟文本响应 |
| `browser_emulate(operation="status", settings={})` | 模拟视口、触摸、语言、时区、定位和显示偏好 |
| `browser_evaluate(observation_id, expression, argument={}, frame_index=0)` | 主动运行页面 JavaScript，取得 JSON 数据和新观察 |

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

观察支持 iframe 和开放的 Shadow DOM，frame 带有用于等待和提取的 `frame_index`。单次结果最多返回 16 个 frame、150 个可操作元素、合计 16000 字符的正文前缀与 `viewport_text`（视口文字），元素描述另有总长度限制。结果优先列出视口内元素，并明确标注截断情况；滚动后可重新观察后面的内容。密码输入框的值不会出现在元素清单中。隐藏的文件输入框也会列出，便于上传。

### 鼠标操作

- 双击使用 `browser_click(..., click_count=2)`；右键使用 `button="right"`，可以操作网页自定义右键菜单。浏览器原生右键菜单不属于网页 DOM，不提供菜单操作工具。
- 悬停使用 `browser_hover()`。菜单或提示异步出现时，可以继续观察或等待其中的文字，再使用新元素编号操作。
- 元素拖拽使用同一次观察中的 `source_ref` 和 `target_ref`，从起点元素中心拖到终点元素中心。两者需同时位于视口内，可先滚动再重新观察。观察结果会标记原生可拖动元素的 `draggable=true`。
- 滑块、画布等区域可以使用截图拖拽，传入 `start_x/start_y/end_x/end_y` 四个 CSS 坐标。全部坐标在操作开始前一起校验，不能混用元素编号。
- 鼠标操作后返回新的页面观察，旧编号随即失效。拖拽超时或取消会尝试释放鼠标按键；网页是否接受拖放需要根据返回状态确认。

### 截图与坐标

截图以图片内容进入模型上下文，不会自动发送到聊天中。需要使用支持图片输入的模型，并在 AstrBot 提供商配置的 `modalities` 中显式启用 `image`；未配置时返回 `VISION_UNAVAILABLE`，不会用图片路径代替模型可见的图片。

截图使用当前视口，默认 1280×720，可以通过环境模拟工具调整；PNG 使用 CSS 像素，不随设备像素比放大。坐标点击、悬停和拖拽必须带上对应截图的 `observation_id`；检测到 DOM、滚动位置、网址、视口或视觉缩放发生变化时要求重新截图。移动端页面自动缩放时返回 `coordinate_click_available=false`，改用元素编号操作。Canvas 动画等不产生 DOM 变化的视觉更新无法被完整检测，操作动态界面时应及时重新截图。

### 会话和错误

- 同一消息来源中的不同发送者拥有独立的 Cookie、存储和页面；模型不能指定或访问其他用户的会话。
- 会话默认保留至空闲 600 秒，并由周期任务回收。关闭最后一个标签页、调用 `browser_close()` 或卸载插件也会释放会话。
- 每个会话最多 8 个标签页。网页弹出的标签页会出现在列表中，由模型明确切换。
- 会话正常结束前自动保存登录快照，创建新会话时按用户自动恢复；适用于全部工具层级。浏览操作期间不定时保存，也不会每次操作都写盘，仍可通过 `browser_state(operation="save")` 提前保存。
- Agent 页面启用 JavaScript 并允许 HTTP/HTTPS 请求；网络访问使用 AstrBot 所在主机的网络环境。
- JavaScript 弹窗按一次性预设规则处理，无匹配规则时自动取消；结果包含 `dialogs` 历史和 `notices`。人工登录接管暂未提供。
- 失败结果包含 `success=false` 及 `error.code`、`error.message`。元素失效时重新观察；工具超时后操作可能已执行，应先观察确认，避免重复提交。
- 浏览器重启会返回 `SESSION_LOST`；原会话不自动重放，模型需要重新打开网站。

模型工具在 AstrBot 进程内运行。网页文字作为任务资料使用，任务目标来自用户。

### 上传与下载

文件与会话使用相同的“消息来源＋发送者”隔离。`browser_upload(operation="list")` 会导入当前用户本条消息中直接携带的文件和图片附件，并返回个人文件清单。引用消息中的附件不自动导入；平台需要能通过 AstrBot 提供附件内容。重复列出同一附件不会生成重复文件。

上传时使用最新观察中的 `type=file` 元素编号和个人 `file_ids`，例如 `browser_upload(operation="set", observation_id="obs_...", ref="e1", file_ids=["file_..."])`；`clear` 清空该输入框。支持隐藏文件输入框，多文件上传需要网页输入框支持 `multiple`。

点击网页下载链接后，下载自动登记在个人文件区。页面观察的 `downloads` 或 `browser_download(operation="list")` 返回编号及 `downloading/ready/failed` 状态。使用 `wait` 等待完成，`send` 才会发送文件到当前聊天；平台不支持文件消息时明确返回发送失败，文件仍可保留或删除。自己下载的文件也可以再次上传。

模型不能传入服务器路径，也不能访问其他用户的文件编号。每人最多 20 个文件、合计 200 MiB，单文件最多 50 MiB，同时最多 3 个下载；下载后台最多等待 120 秒，`wait` 调用仍受单次工具时限限制，超时不会主动取消后台下载。下载完成后校验大小并删除超限文件，浏览器接收过程的临时缓存可能超过该大小。文件可以跨会话使用，在删除文件或卸载/重载插件时清理；关闭浏览器会话不会删除已完成文件，但会中断未完成下载。

### 提取与等待

`browser_extract` 的 `kind` 支持 `tables`（表格的标题和行列）、`links`（文字和完整链接）、`lists`（有序/无序列表）及 `text`（正文）。`selector` 可限定 CSS 范围，`frame_index` 指定观察中的 frame；支持开放 Shadow DOM。提取可见内容，单次最多 200 条、24000 字符；表格按行、列表按列表项计数，超限返回 `truncated=true`。它使用固定的只读提取脚本；定制 JavaScript 使用第三层的 `browser_evaluate`。

等待示例：

```python
browser_wait(condition="text", value="保存完成")
browser_wait(condition="text", value="加载中", state="hidden")
browser_wait(condition="selector", value="#result", state="visible", frame_index=0)
browser_wait(condition="selector", value=".loading", state="detached")
browser_wait(condition="url", value="https://example.com/result*")
browser_wait(condition="load", value="domcontentloaded")
```

文字支持 `visible/hidden`，CSS 元素另支持 `attached/detached`，默认 `visible`。网址和加载状态只支持主页面，`state` 留空；加载状态可选 `domcontentloaded/load`。等待成功返回新观察，旧元素编号失效。`browser_wait` 参数已改为 `condition/value`，不保留旧 `text` 参数。

### 弹窗与登录状态

弹窗需要先预设，再触发：例如先调用 `browser_dialog(operation="accept", dialog_type="confirm", message="确认操作")`，再点击页面按钮。`dialog_type` 和 `message` 为空时不限制；填写后必须完全匹配，命中一次就消耗规则，其他弹窗自动取消。`prompt_text` 用于接受 prompt 时输入文字，`clear` 删除尚未使用的规则，`list` 返回待用规则和最近 10 次历史。它不会使网页一直停在弹窗上等待下一轮工具调用。

每个用户有一份独立的登录状态快照，保存 Cookie、Local Storage 和 IndexedDB，不保存标签页、密码输入框或 sessionStorage。调用 `browser_close()`、通过 `browser_tabs` 关闭最后一个标签页、空闲回收和插件正常停止/卸载/重载时，在关闭 Context 前自动保存一次；创建新会话时自动载入自己的快照，无需手动调用 `save` 或 `restore`。自动保存和恢复在 `basic`、`standard`、`full` 层级均生效，浏览操作期间不自动写盘。切换浏览环境或显式恢复快照时的 Context 重建不触发自动保存。

`browser_state` 在 `standard/full` 层级提供显式管理：`save` 立即覆盖自己的上次保存，并重新启用当前会话的自动保存；`restore` 关闭现有标签页并载入磁盘快照，随后调用 `browser_open` 访问网站。`clear` 删除磁盘快照并停用当前会话的自动保存，当前页面仍保留登录；环境重建也会延续停用状态，显式 `save` 或创建下一次新会话后恢复自动保存。彻底清除当前登录及其快照可先 `clear` 再 `browser_close()`。

自动保存最多等待 5 秒导出状态，失败会记录日志并继续关闭，已有快照保留；显式关闭会话的返回值 `state_saved` 为 `true`、`false`、`null`，分别表示保存成功、失败、因 `clear` 跳过。自动恢复遇到损坏或不可用的快照时，会创建空白会话并返回提示；显式 `restore` 失败仍保留原会话。浏览器崩溃、断电、强制结束进程等情况无法保证保存最新状态，登录凭据过期后仍需重新登录。

快照保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_browser/login_states/` 中，按身份哈希划分，最多 20 MiB。快照包含登录凭据，以 JSON 存在本机磁盘，请按凭据文件管理该目录。模型只收到保存/恢复结果，不会收到 Cookie 内容或磁盘路径。插件卸载、重启或关闭 BrowserUse 不会删除已保存的快照，下次创建个人会话时自动恢复。

### 高级：网络监听和请求拦截

网络监听需要主动开始，之后再打开网页或触发请求，例如：

```python
browser_network(operation="start", url_pattern="*/api/*")
browser_open(url="https://example.com")
# 操作页面后查询记录，再使用真实返回的 req_ 编号读取详情
browser_network(operation="list", limit=20)
browser_network(operation="detail", request_id="req_...", include_body=True)
browser_network(operation="stop")
```

每个用户会话最多保留最近 200 条请求元数据，超出后丢弃最旧记录并返回 `dropped`。可查询 HTTP 请求方法、资源类型、响应状态和失败原因；`detail` 返回请求/响应头。Authorization、Cookie、Set-Cookie 等敏感请求头会遮盖。正文只在 `include_body=true` 时读取：请求正文最多 64 KiB；响应需完成、属于文本类型且声明 `Content-Length <= 64 KiB`。缺少长度、二进制或过大时返回明确的 `body_unavailable`，解压后的正文仍会截断至 64 KiB。不记录 WebSocket 消息帧。

`stop` 停止接收新记录，已有请求仍可完成；`clear` 清空记录但不改变监听开关。日志只保存在当前会话内存中，关闭、空闲回收或重建会话后清空，不写磁盘。

拦截规则同样只作用于当前用户会话，在请求发出前添加：

```python
browser_intercept(operation="add", rule={
    "url_pattern": "*", "action": "abort", "resource_types": ["image", "font"]
})
browser_intercept(operation="add", rule={
    "url_pattern": "*/api/*", "action": "modify",
    "headers": {"X-Test-Mode": "true"}
})
browser_intercept(operation="add", rule={
    "url_pattern": "*/test/data", "action": "fulfill", "status": 200,
    "content_type": "application/json", "body": '{"source":"mock"}'
})
# list 返回真实 rule_ 编号，remove 移除一条，clear 移除全部规则
browser_intercept(operation="list")
browser_intercept(operation="remove", rule_id="rule_...")
```

每个会话最多 20 条规则，按添加顺序使用第一条匹配规则；URL 模式中的 `*` 匹配任意字符，可选 `resource_types` 和大写 `match_method` 缩小范围。`modify` 还支持 `url`、`method`、`post_data`，网址必须是无账号密码的 HTTP/HTTPS 地址；正文最多 64 KiB，不支持用它覆盖浏览器管理的 Cookie、Host、Content-Length 和 Connection。`fulfill` 支持 200～599 状态码、响应头和文本正文。查询时长正文以预览和截断标记返回；无效或不适用的字段会报错。规则执行失败时尝试中止请求，并通过 `errors` 和页面观察中的 `interception_errors` 返回原因。

### 高级：设备和环境模拟

`status` 返回当前环境；`set` **完整替换环境设置**，没有传入的字段使用默认值；`reset` 恢复默认桌面环境。例如：

```python
browser_emulate(operation="set", settings={
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 2, "is_mobile": True, "has_touch": True,
    "locale": "zh-CN", "timezone_id": "Asia/Shanghai",
    "color_scheme": "dark",
    "geolocation": {"latitude": 31.2, "longitude": 121.5}
})
browser_open(url="https://example.com")
```

还支持自定义 `user_agent`、`reduced_motion`（`reduce/no-preference`）及 `offline`。语言、时区和移动模式是 Context 设置，因此应用时会重建会话并关闭旧标签页，保留 Cookie、Local Storage、IndexedDB，旧观察编号失效；页面和 sessionStorage 不保留。无效环境不会替换仍可用的旧会话。设置地理位置后仅为此用户的浏览器 Context 授予定位权限。

重建会话保留拦截规则和监听的启停状态/URL 模式，清空网络历史和一次性弹窗规则。恢复登录快照时也保留当前环境。环境设置只在本次会话内有效，不写磁盘；其他用户和离线渲染使用自己的环境。`is_mobile/has_touch` 控制布局和触摸能力；若网站依赖 User-Agent 判断设备，可同时传入 `user_agent`。

### 高级：主动执行 JavaScript

`browser_evaluate` 在指定 frame 里运行表达式或函数，可以等待 Promise，支持传入 JSON 对象参数：

```python
browser_evaluate(
    observation_id="obs_...",
    expression="arg => ({title:document.title, count:document.querySelectorAll(arg.selector).length})",
    argument={"selector": "a"}, frame_index=0
)
```

结果在 `evaluation.result` 中，并附带新页面观察；脚本可以修改页面、访问当前网站存储或发起网络请求，使用后需根据新观察决定下一步。返回值必须能转成 JSON；DOM 节点、循环引用、BigInt 等会报明确错误。表达式最多 20000 字符、参数 JSON 最多 32000 字节；结果最多 24000 字符，超限改为 `evaluation.result_preview` 并标记 `truncated=true`。

脚本超时或调用取消时会关闭当前会话，避免无限循环卡住后续操作；需要重新 `browser_open`。网页产生的请求仍经过当前会话的监听和拦截规则。工具层级仅控制模型可调用的工具，网站本身的 JavaScript 在所有层级始终正常运行。

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
| `browser_use.tool_level` | `basic` | `basic` / `standard` / `full` | 基础浏览 12 个、完整交互 18 个、高级控制 22 个工具，高层包含低层全部工具 |
| `browser_use.max_sessions` | `4` | 1～16 | 同时保留的 Agent 联网会话上限，与执行并发分别限制 |
| `browser_use.idle_timeout` | `600` 秒 | 30～3600 | Agent 会话空闲回收时间 |
| `browser_use.action_timeout` | `30` 秒 | 1～120 | 单次 Agent 工具总时限，包括排队、操作和观察 |

保存配置后重载插件生效。关闭 BrowserUse 后，全部 `browser_*` 工具从可用工具列表中移除，工具定义不会发送给模型；缓存的直接调用也会返回 `BROWSER_USE_DISABLED`。重载会自动保存原有 Agent 会话的登录状态后关闭会话，并清理个人临时文件；已保存的登录快照保留。离线渲染服务仍然可用，工具管理中的单项启停设置保持不变。

启用 BrowserUse 时，仅保留所选层级的工具定义；缓存调用更高层级的工具会返回 `TOOL_LEVEL_REQUIRED`，提示所需层级。原 `browser_use.network_enabled`、`intercept_enabled`、`emulation_enabled`、`javascript_enabled` 已移除，统一使用 `browser_use.tool_level`，不提供旧开关别名。若将旧字段直接传入插件，会报未知配置错误；AstrBot 配置加载器可能清理旧字段并补入默认 `basic`，需要高级能力时请在重载后明确选择 `full`。

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

`tests/test_agent_browser.py` 自动选择已安装的 Chromium，启动本地测试网站验证表单和鼠标操作、上传下载、弹窗、结构化提取、精确等待、登录快照恢复、多标签页、iframe、Shadow DOM、截图坐标、会话隔离和取消清理；没有浏览器时明确跳过。`tests/test_browser_storage.py` 验证文件配额、失败清理与快照写入。`tests/test_service.py` 的额外 PNG/PDF 冒烟测试通过 `ASTRBOT_BROWSER_EXECUTABLE` 指定浏览器路径启用。测试不调用外部模型 API；截图结果另外经过 AstrBot 的真实工具执行器和 Agent 循环，验证图片进入下一次模型请求。

## 📜 定位

这是一个面向 AstrBot Agent 和其他插件的浏览器基础服务：Agent 通过工具观察和操作网页，上层插件通过 Python 接口执行离线渲染。插件统一管理 Chromium 的启动、隔离、执行并发和会话回收。
