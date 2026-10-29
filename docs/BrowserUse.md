# BrowserUse：配置与使用原理

BrowserUse 向模型提供联网网页操作工具。本文说明权限、个人会话、登录状态、工具契约及故障处理；安装步骤见[项目首页](../README.md)。共用 Chromium 的配置、生命周期和供其他插件使用的渲染接口见[共享浏览器](./共享浏览器.md)。

[配置参考](#configuration) · [权限与隔离](#permissions) · [会话生命周期](#lifecycle) · [工具清单](#tools) · [页面操作](#observations) · [有头模式](#headed) · [本地浏览器 CDP](#cdp) · [登录与文件](#storage) · [高级控制](#advanced) · [边界与错误](#boundaries) · [故障排查](#faq) · [开发验证](#development)

<a id="configuration"></a>

## 💠 配置参考

**BrowserUse**（`browser_use`）组只放 Agent 网页工具的专用配置，CDP 选项集中在其 `cdp` 子配置组：

| 配置项 | 默认值 | 范围 | 说明 |
|---|---:|---:|---|
| `browser_use.enabled` | `true` | 布尔值 | 是否向模型提供 BrowserUse 工具 |
| `browser_use.user_whitelist` | `[]` | 用户 ID 字符串列表 | 个人白名单，与管理员同权限，私聊及任意群聊均可用；空列表仅允许管理员 |
| `browser_use.tool_level` | `basic` | `basic` / `standard` / `full` | 基础浏览 13 个、完整交互 19 个、高级控制 23 个工具，高层包含低层全部工具 |
| `browser_use.headed_enabled` | `true` | 布尔值 | 允许明确请求时启动有头窗口；最多一个有头实例及会话 |
| `browser_use.cdp.enabled` | `false` | 布尔值 | 明确请求时自动发现 Chrome、Edge 或 Chromium，等待浏览器授权后连接 |
| `browser_use.cdp.endpoint` | 空 | 本机调试 URL | 留空自动发现；支持省略协议、HTTP(S) 或 WebSocket 地址；仅连接指定主机和端口 |
| `browser_use.cdp.user_data_dir` | 空 | 用户数据根目录 | 可指定绝对路径；用于自动发现或补齐 WebSocket 路径；留空查找浏览器常见目录，多目标时需指定 |
| `browser_use.cdp.user_id` | 空 | 个人用户 ID | 留空允许任意 AstrBot 管理员使用；填写后仅允许该 ID 对应的管理员使用 |
| `browser_use.cdp.allow_existing_tabs` | `true` | 布尔值 | 允许列出并明确选择接入已有标签页；默认只管理插件新建的页面 |
| `browser_use.max_sessions` | `4` | 1～16 | 同时保留的 Agent 联网会话上限，与执行并发分别限制 |
| `browser_use.idle_timeout` | `600` 秒 | 30～3600 | Agent 会话空闲回收时间 |
| `browser_use.action_timeout` | `30` 秒 | 1～120 | 单次 Agent 工具总时限，包括排队、操作和观察 |

CDP 选项位于 `browser_use.cdp`，包含 `enabled`、`endpoint`、`user_data_dir`、`user_id` 和 `allow_existing_tabs` 五项。保存配置后重载插件生效。关闭 BrowserUse 后，全部 `browser_*` 工具从可用工具列表中移除，工具定义不会发送给模型；缓存的直接调用也会返回 `BROWSER_USE_DISABLED`。重载会自动保存原有 Agent 会话的登录状态后关闭会话，并清理个人临时文件；已保存的登录快照保留。离线渲染由独立的 `render.enabled` 控制，工具管理中的单项启停设置保持不变。

`render.enabled` 默认开启，关闭共享渲染不影响 BrowserUse。两者都关闭时，插件不初始化浏览器，也不触发 Chromium 自动安装。

浏览器路径、执行并发、排队、启动及实例空闲关闭属于共用的 `browser` 组，见[共享浏览器配置](./共享浏览器.md#configuration)。

### 工具层级与旧版配置

启用 BrowserUse 时，仅保留所选层级的工具定义；缓存调用更高层级的工具会返回 `TOOL_LEVEL_REQUIRED`，提示所需层级。原 `browser_use.network_enabled`、`intercept_enabled`、`emulation_enabled`、`javascript_enabled` 已移除，统一使用 `browser_use.tool_level`，不提供旧开关别名。若将旧字段直接传入插件，会报未知配置错误；AstrBot 配置加载器可能清理旧字段并补入默认 `basic`，需要高级能力时请在重载后明确选择 `full`。

原顶层 `max_agent_sessions`、`agent_idle_timeout`、`agent_action_timeout` 已移入 `browser_use` 组并更名，不提供旧字段别名。直接传入旧字段会报未知配置错误；AstrBot 配置加载器可能清理旧字段并补默认值，升级前请记录旧值，再在新组中重新设置。

<a id="permissions"></a>

## 🔐 权限与会话隔离

**默认仅 AstrBot 管理员可用。** 其他个人用户需要加入 `browser_use.user_whitelist`，例如 `["12345678", "87654321"]`。白名单与管理员拥有相同的浏览器工具权限，包括全局配置允许的有头模式及高级工具；所有人仍受 `tool_level`、`headed_enabled` 和工具启停设置限制。

白名单只填写消息发送者的个人用户 ID（如 QQ 号），不填写群号。授权只看个人，不限制聊天来源：授权用户可在私聊或任意群聊使用，同群其他人不会因此获得权限。插件仍遵守 AstrBot 自身的消息接收、唤醒和平台白名单设置。白名单按发送者 ID 精确匹配，与 AstrBot 管理员的 ID 匹配方式一致；多平台使用时，相同发送者 ID 会命中同一条目。

每次调用全部 `browser_*` 工具时都会重新校验权限，未授权返回 `PERMISSION_DENIED`，不创建会话、不读取附件、不返回截图或发送文件。普通模式中不同聊天来源的会话和登录快照分别隔离。CDP 另要求管理员身份，填写 `user_id` 后还需匹配该 ID，并使用外部浏览器自身的账号状态，见[本地浏览器 CDP](#cdp)。保存配置后重载插件生效；升级后原先可用的非管理员用户需要加入白名单。供其他插件调用的 Python 离线渲染接口不受此个人白名单限制。

会话身份由消息来源和发送者组成，对应 `(event.unified_msg_origin, event.get_sender_id())`，由插件从真实事件取得，模型不能指定。普通模式中一个人的不同聊天来源使用独立页面、文件与登录快照；CDP 同一时间只允许其中一个会话占用外部浏览器。

<a id="lifecycle"></a>

## 🧩 会话生命周期

普通 BrowserUse 与离线渲染复用无头 Chromium，明确请求有头模式时才额外启动窗口。普通模式中每个 Agent 身份有独立的 BrowserContext，跨多轮保留；登录状态不与其他身份或渲染 Context 共享，使用 `storage_state` 快照恢复登录。显式启用的 CDP 模式使用外部浏览器默认 Context，连接与退出规则见[本地浏览器 CDP](#cdp)。

### 执行并发与保留会话

`browser.max_concurrent_sessions` 限制同时执行的页面任务，BrowserUse 与渲染共用这些名额。无头、有头与 CDP 工具操作均占用执行名额，人工等待和保留但未操作的会话不占用。

`browser_use.max_sessions` 单独限制保留的 Agent 会话数，有头、无头与 CDP 合计计数；每个会话最多管理 8 个标签页。有头模式另有最多一个实例、一个活动会话的限制，CDP 也最多保留一个活动会话。

同一身份的模型操作串行执行。排队最多等待 `browser.queue_timeout` 秒，BrowserUse 还受单次工具总超时约束。

### Agent 会话结束

会话空闲达到 `browser_use.idle_timeout` 后由周期任务回收，默认 600 秒。显式调用 `browser_close()`、通过工具关闭最后一个标签页、插件正常停止或重载，也会结束会话。

普通会话正常结束时先尝试保存登录快照，再关闭 Context。有头会话结束后同时释放有头实例；无头实例由[共享浏览器的空闲关闭设置](./共享浏览器.md#lifecycle)决定是否继续驻留。CDP 不保存快照、不关闭外部 Context 或浏览器，只清理插件管理的资源并断开连接。会话保存、恢复和 `clear` 的行为见 [登录状态与文件](#storage)。

正常停止或重载插件时，先取消未完成调用并保存可用的 Agent 登录状态，再关闭会话、清理个人临时文件；已有登录快照保留。浏览器崩溃、断电或强制结束进程无法保证保存最新状态。意外重启导致旧会话丢失时返回 `SESSION_LOST`，需要重新打开网址。实例的空闲关闭不会关闭仍保留的 Context，详细条件见[共享浏览器生命周期](./共享浏览器.md#lifecycle)。

<a id="tools"></a>

## 🤖 BrowserUse 工具

模型需要支持工具调用；显式配置提供商 `modalities` 时应包含 `tool_use`。截图另需图片输入能力。

下文的工具调用代码展示模型工具的参数形式；其他插件的 Python 接入方式见 [其他插件接入](./共享浏览器.md#integration)。

Agent 使用 AstrBot 自身的工具调用循环决定下一步，插件负责执行操作并返回真实页面状态。工具分为三个累计层级，高层包含低层全部工具；未选层级的工具定义会从可用列表中移除，不占用模型上下文。

| 配置值 | 层级 | 工具总数 | 适用任务 |
|---|---|---:|---|
| `basic`（默认） | 第一层：基础浏览 | 13 | 搜索、阅读、表单填写、菜单、标签页和浏览器模式切换 |
| `standard` | 第二层：完整交互 | 19 | 增加拖拽、文件上传下载、结构化提取、弹窗和登录状态管理 |
| `full` | 第三层：高级控制 | 23 | 增加网络监听、请求拦截、设备环境模拟和主动执行 JavaScript |

### 第一层：基础浏览

| 工具 | 用途 |
|---|---|
| `browser_mode(mode="status")` | 查询模式；明确请求时切换 `headed` / `headless` / `cdp`，CDP 需额外配置授权 |
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
| `browser_tabs(operation="list", tab_id="")` | `list` / `switch` / `close` 管理会话标签页；CDP 可选 `available` / `attach` 显式接入已有页面 |
| `browser_close()` | 普通模式保存快照并关闭会话；CDP 关闭插件新建的页面并断开，保留用户已有页面 |

### 第二层：完整交互

设置 `browser_use.tool_level="standard"`，在基础浏览的 13 个工具上增加以下 6 个：

| 工具 | 用途 |
|---|---|
| `browser_drag(observation_id, source_ref="", target_ref="", start_x=-1, start_y=-1, end_x=-1, end_y=-1)` | 使用两个元素编号或四个截图坐标，按住左键从起点拖到终点后释放；两种模式不能混用 |
| `browser_upload(operation="list", observation_id="", ref="", file_ids=[])` | 导入当前消息附件并列出个人文件，或向文件输入框上传/清空文件 |
| `browser_download(operation="list", file_id="")` | 查看个人文件、等待下载、发送到当前聊天或删除文件 |
| `browser_extract(kind="tables", selector="", frame_index=0, limit=100)` | 提取表格、链接、列表或指定区域正文 |
| `browser_dialog(operation="list", dialog_type="", message="", prompt_text="")` | 查询弹窗历史，预设下一次弹窗的接受/取消和 prompt 输入 |
| `browser_state(operation="status")` | 查询、显式保存、恢复或删除个人登录状态 |

### 第三层：高级控制

设置 `browser_use.tool_level="full"`，在完整交互的 19 个工具上增加以下 4 个：

| 工具 | 用途 |
|---|---|
| `browser_network(operation="list", request_id="", url_pattern="", limit=50, include_body=False)` | 开始/停止网络监听，查询请求、状态、失败原因及文本响应 |
| `browser_intercept(operation="list", rule_id="", rule={})` | 屏蔽请求、修改请求头/网址/方法/正文、模拟文本响应 |
| `browser_emulate(operation="status", settings={})` | 模拟视口、触摸、语言、时区、定位和显示偏好 |
| `browser_evaluate(observation_id, expression, argument={}, frame_index=0)` | 主动运行页面 JavaScript，取得 JSON 数据和新观察 |

<a id="observations"></a>

## 🖱️ 页面观察与操作

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

<a id="headed"></a>

## 🪟 有头模式与人工操作

默认会话使用无头实例，只有用户明确需要可见窗口时才调用 `browser_mode(mode="headed")`。窗口显示在 **AstrBot 所在主机的图形桌面**，需要可用的桌面环境；使用普通 Context，不通过 CDP 接管用户已有的 Chrome。

```python
browser_mode(mode="status")     # 查询，不启动浏览器
browser_mode(mode="headed")     # 按需启动窗口，或者把个人会话交接到有头实例
browser_open(url="https://example.com")
# 人工登录或操作完成后，先确认标签页，再重新观察
browser_tabs(operation="list")
browser_observe()
browser_mode(mode="headless")   # 可选：在内存中交接回无头，随后需重新打开网址
# 或 browser_close()：保存登录快照并结束会话
```

最多一个有头 Browser 和一个活动有头会话。重复请求自己的当前模式不会重建会话；被其他用户占用时返回 `HEADED_BUSY`，不抢占、不长期排队。无头会话、渲染与有头工具操作共享原有执行并发名额，人工等待不占执行名额；有头与无头会话合计受 `max_sessions` 限制。

同一用户同时只有一个活动会话。模式切换时先创建目标 Context，成功后再释放旧 Context；启动或导入失败时保留原会话。Cookie、Local Storage、IndexedDB、环境设置、拦截规则和监听开关随会话交接；页面、sessionStorage、观察编号、网络历史和弹窗规则不保留，之后需 `browser_open`。切换只在内存传递状态，不写登录快照；`clear` 停用自动保存的标志也会延续。现有点击、输入、截图、文件等工具继续操作当前模式，返回结果包含 `mode`。

主动关闭、工具关闭最后一个标签页或空闲回收时，先保存，再关闭有头 Context 和 Browser，保留无头实例及共享 Playwright；插件正常停止时则先完成会话保存，再关闭两种浏览器和运行时。下一次普通 `browser_open` 默认回到无头模式。人工点击、输入和滚动会节流更新空闲时间；后台请求和自动刷新不续期，模式查询也不续期。人工操作不与模型操作自动互斥，接手前应确认目标标签页并重新观察。

手动关窗会尽力保存仍可访问的 Context，并释放占用名额；若 Context 已销毁或浏览器崩溃，则保留旧磁盘快照，无法保证保存最后状态。后续观察不会自动重开有头窗口。有头启动失败或退出不停止无头渲染服务。`browser_use.headed_enabled=false` 可关闭有头入口。

<a id="cdp"></a>

## 🔌 本地浏览器控制（CDP）

CDP 模式自动发现并连接 **AstrBot 所在主机**上正在使用的 Chrome、Edge 或 Chromium，复用该浏览器自己的 Cookie、Local Storage 和登录状态。它不能直接控制聊天用户另一台电脑上的浏览器；Docker 部署时，回环地址指容器自身。此功能默认关闭，沿用已有 Playwright 依赖，无需额外 Python 包。

### 连接正在使用的本地浏览器

Chrome 144 及以上支持在浏览器运行期间开启远程调试，连接后保留当前用户资料、登录状态和已有页面，无需重新启动浏览器或另外登录。Edge、Chromium 也支持标准目录发现，但需要所用版本提供远程调试开关、发布 `DevToolsActivePort` 文件，并支持相应的连接授权；仅有 Chromium 内核版本号不能保证这些功能均可用。

1. 在正在使用的浏览器中打开 `chrome://inspect/#remote-debugging`（Edge 使用 `edge://inspect/#remote-debugging`），启用远程调试。
2. 在 BrowserUse → 本地浏览器控制（CDP）子配置组中开启 `enabled`，保存后重载插件。`user_id`（使用者的个人 ID）留空允许任意 AstrBot 管理员使用；填写后仅允许该 ID 对应的管理员使用。BrowserUse 白名单不授予 CDP 权限。若要操作已经打开的页面，确认 `allow_existing_tabs` 已开启（默认开启）。
3. 明确请求连接当前浏览器，触发 `browser_mode(mode="cdp")`；随后在浏览器弹出的调试连接授权窗口中点击允许。授权由浏览器控制，插件不会代为开启开关或接受授权。
4. 使用下文的 `available` 和 `attach` 选择要操作的现有标签页。

`endpoint` 留空时，自动连接从浏览器用户数据根目录读取 `DevToolsActivePort`，仅连接其中的回环 WebSocket 地址，不启动浏览器、不修改用户资料。插件加载和状态查询均不读取此文件或发起授权请求；每次重新连接都会重新读取，适应浏览器更换调试端口。

`endpoint` 与 `user_data_dir` 均留空时检查运行 AstrBot 的系统用户下以下标准目录，仅在调用 `browser_mode(mode="cdp")` 时读取调试文件：

| 系统 | Chrome | Edge | Chromium |
| --- | --- | --- | --- |
| Linux | `~/.config/google-chrome` | `~/.config/microsoft-edge` | `~/.config/chromium` |
| macOS | `~/Library/Application Support/Google/Chrome` | `~/Library/Application Support/Microsoft Edge` | `~/Library/Application Support/Chromium` |
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data` | `%LOCALAPPDATA%\Microsoft\Edge\User Data` | `%LOCALAPPDATA%\Chromium\User Data` |

Linux 上三者均尊重 `XDG_CONFIG_HOME`，Chrome 还优先使用 `CHROME_CONFIG_HOME`；另检查 `~/snap/chromium/common/chromium` 和 `~/.var/app/` 下 Chrome、Edge、Chromium 的常见 Flatpak 数据目录。仅有一个目录包含有效调试信息时才连接；多个目录均包含时返回 `CDP_MULTIPLE_BROWSERS`，需填写 `user_data_dir` 或 `endpoint` 明确选择。发现阶段不探测端口或尝试连接，因此浏览器退出后残留的有效调试文件也可能计入候选；遇到此情况同样可填写目标目录。

使用自定义目录、Beta/Dev/Canary 或其他未覆盖的安装目录时，在 `user_data_dir` 填写实际绝对路径（支持 `~`）。可从 `chrome://version`（Edge 使用 `edge://version`）查看“个人资料路径”，填写其上一级用户数据目录，例如 `…/google-chrome`，**不要填写 `Default` 或 `Profile 1` 子目录**。多实例时通过此目录明确选择浏览器；不会扫描其他账户的目录或任选一个实例。AstrBot 需具备读取该文件的权限，并能连接浏览器的本机端口；Docker 默认无法直接连接宿主机 Chrome。

默认单次工具总时限为 30 秒，包含等待浏览器授权；需要更多操作时间可把 `browser_use.action_timeout` 调至 60～120 秒，并确保 AstrBot 的工具调用时限足够。找不到调试文件返回 `CDP_DISCOVERY_FAILED`；浏览器拒绝、退出或连接失败返回 `CDP_CONNECTION_FAILED`，整体超时返回 `TIMEOUT`。启用开关、允许连接或修正配置后需明确重新请求，不会自动重试或重启浏览器。

参考：[Chrome 官方的当前会话连接说明](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)。

### 手动填写调试地址

如果自动发现不可靠，可以在“本地浏览器调试地址”（`browser_use.cdp.endpoint`）中填写浏览器当前的调试地址，例如：

- `127.0.0.1:41063`：省略协议时，先从候选目录的调试文件中匹配端口并补齐 WebSocket 路径；没有匹配时，再尝试该主机和端口的 HTTP `/json/version` 接口。若匹配到多个不同路径则报错，不会尝试 HTTP；已找到路径但连接失败时也不会重试。
- `http://127.0.0.1:41063`：插件读取该地址的 `/json/version` 获取浏览器连接路径，也接受以 `/json/version` 结尾的地址。目标浏览器必须提供此 HTTP 接口；新版“当前会话调试”可能不提供，此时可填写下面的简写 WebSocket 地址，由插件自动读取路径。
- `ws://127.0.0.1:41063`：只填写主机和端口。插件读取候选目录中的 `DevToolsActivePort`，仅使用端口为 `41063` 的浏览器路径，并保留你填写的主机、端口和协议。也接受以 `/`、`/devtools/browser` 或 `/devtools/browser/` 结尾的简写。
- `ws://127.0.0.1:41063/devtools/browser/实际浏览器ID`：已知完整路径时直接连接，不读取调试文件。

支持 `http`、`https`、`ws`、`wss`；只填 `127.0.0.1:41063`、`localhost:41063` 或 `[::1]:41063` 时自动选择文件匹配或 HTTP 发现。已知完整 `/devtools/browser/…` 路径时也可省略 `ws://`，直接填写 `127.0.0.1:41063/devtools/browser/实际浏览器ID`。显式填写协议则按指定方式处理；使用 TLS 时需保留 `https://` 或 `wss://`。主机仅支持本机回环 IP 或 `localhost`。HTTP 发现不使用代理、不跟随重定向，返回的 WebSocket 地址也必须指向本机。填写 HTTP 地址或完整 WebSocket 地址时不读取用户数据目录；WebSocket 简写或省略协议的主机和端口则读取指定的 `user_data_dir`，未指定时检查上述浏览器常见目录。显式 WebSocket 简写找不到匹配端口时返回 `CDP_DISCOVERY_FAILED`，同一端口匹配到多个不同路径返回 `CDP_MULTIPLE_BROWSERS`，可填写目标用户数据目录解决。不会连接其他端口的浏览器，连接失败不会回退。浏览器重启可能改变端口或浏览器 ID；简写地址每次重新连接都会重新读取路径，端口变化时仍需更新地址并重载插件，完整地址则需同时更新 ID。地址留空则恢复自动发现。

调试地址在实际连接时才校验；格式错误返回 `CDP_INVALID_ENDPOINT`，不会阻止插件加载，也不影响普通浏览和共享渲染。修改地址后保存并重载插件即可重试，或清空地址恢复自动发现。

填写地址同样仅在明确调用 `browser_mode(mode="cdp")` 后发起连接，仍校验管理员权限，并遵循目标浏览器的授权流程；不会启动、重启浏览器或替用户接受授权。调试地址与用户数据目录只由管理员配置，工具参数不接受这些值，无需 Node.js、MCP 服务或额外 Python 包。

### 使用和标签页范围

只有用户明确要求连接本机浏览器时才切换 CDP；普通 `browser_open` 仍默认使用无头模式，插件加载和状态查询不会预先连接外部浏览器。

```python
browser_mode(mode="cdp")
browser_open(url="https://example.com")
browser_observe()
# 现有点击、输入、截图等工具继续操作当前页面
browser_close()
```

进入时在外部浏览器默认 Context 中新建并激活空白标签页。默认 `browser_tabs(list)` 只列出插件管理的页面及其弹窗，网络监听、拦截、弹窗处理和下载也仅绑定这些页面，不为整个默认 Context 设置规则。用户自行打开的其他页面不会自动加入会话。每个会话最多管理 8 个页面，用户其他标签页不计入此限制。

`allow_existing_tabs` 默认开启，允许获授权的管理员明确选择接入已有页面；关闭后仅管理插件新建的页面。仅在用户明确要求操作已有页面后，调用 `browser_tabs(operation="available")` 列出可接入页面；随后使用用户选定的真实 `external_...` 编号调用 `browser_tabs(operation="attach", tab_id="external_...")`。接入后激活该标签页，获得普通 `tab_...` 编号，可继续使用已有工具。CDP 新建、接入、显式切换标签页，以及关闭当前页后选择剩余页面时，都会同步激活目标标签页；网页弹出的页面仍需明确切换，才会成为 Agent 的操作目标。没有开启这个选项时，列出和接入均返回 `CDP_EXISTING_TABS_DISABLED`。

### 独占、退出和登录状态

同一外部浏览器最多由一个 Agent 会话占用。其他获授权的管理员或同一管理员在另一聊天来源接入时均会返回 `CDP_BUSY`，不会抢占原会话。每次工具调用重新校验管理员身份；执行仍占用与渲染相同的并发名额。CDP 会话按 `idle_timeout` 回收，只有工具操作续期，外部浏览器中的人工操作不续期。

关闭会话、空闲回收、重载或停止插件时，关闭插件新建的页面，移除绑定的页面规则和监听，然后断开连接；不调用外部 Browser/Context 的关闭接口。显式接入的已有页面会保留，但若用户通过 `browser_tabs(close)` 明确关闭某个页面，该页面会被关闭。外部浏览器退出或连接中断后释放占用，不自动重新连接；需要用户再次明确请求 `browser_mode(mode="cdp")`。断线后无法保证清理仍留在外部浏览器中的页面。

CDP 使用外部账号状态，不具有普通模式的独立 Context 隔离。进入 CDP 不向外部浏览器导入插件快照；若之前有普通会话，会先保存该普通会话自己的状态再结束它。退出 CDP 切回普通模式时，仅恢复插件原有快照，不导出或迁移外部账号状态。CDP 的 `browser_state` 操作与 `browser_emulate(set/reset)` 返回 `CDP_UNSUPPORTED`；查看当前环境返回空对象，表示由外部浏览器管理。普通有头/无头模式的原有行为保持不变。

<a id="storage"></a>

## 💾 登录状态与文件

### 登录快照

普通模式中每个会话身份（消息来源＋发送者）有一份独立的登录状态快照，保存 Cookie、Local Storage 和 IndexedDB，不保存标签页、密码输入框或 sessionStorage。调用 `browser_close()`、通过 `browser_tabs` 关闭最后一个标签页、空闲回收和插件正常停止/卸载/重载时，在关闭 Context 前自动保存一次；创建新会话时自动载入自己的快照，无需手动调用 `save` 或 `restore`。自动保存和恢复在 `basic`、`standard`、`full` 层级均生效，浏览操作期间不自动写盘。切换浏览环境或显式恢复快照时的 Context 重建不触发自动保存。

`browser_state` 在 `standard/full` 层级提供显式管理：`save` 立即覆盖自己的上次保存，并重新启用当前会话的自动保存；`restore` 关闭现有标签页并载入磁盘快照，随后调用 `browser_open` 访问网站。`clear` 删除磁盘快照并停用当前会话的自动保存，当前页面仍保留登录；环境重建也会延续停用状态，显式 `save` 或创建下一次新会话后恢复自动保存。彻底清除当前登录及其快照可先 `clear` 再 `browser_close()`。

自动保存最多等待 5 秒导出状态，失败会记录日志并继续关闭，已有快照保留；显式关闭会话的返回值 `state_saved` 为 `true`、`false`、`null`，分别表示保存成功、失败、因 `clear` 跳过。自动恢复遇到损坏或不可用的快照时，会创建空白会话并返回提示；显式 `restore` 失败仍保留原会话。浏览器崩溃、断电、强制结束进程等情况无法保证保存最新状态，登录凭据过期后仍需重新登录。

快照保存在 AstrBot 的 `data/plugin_data/astrbot_plugin_browser/login_states/` 中，按身份哈希划分，最多 20 MiB。快照包含登录凭据，以 JSON 存在本机磁盘，请按凭据文件管理该目录。模型只收到保存/恢复结果，不会收到 Cookie 内容或磁盘路径。插件卸载、重启或关闭 BrowserUse 不会删除已保存的快照，下次创建个人会话时自动恢复。

### 上传与下载

文件与会话使用相同的“消息来源＋发送者”隔离。`browser_upload(operation="list")` 会导入当前用户本条消息中直接携带的文件和图片附件，并返回个人文件清单。引用消息中的附件不自动导入；平台需要能通过 AstrBot 提供附件内容。重复列出同一附件不会生成重复文件。

上传时使用最新观察中的 `type=file` 元素编号和个人 `file_ids`，例如 `browser_upload(operation="set", observation_id="obs_...", ref="e1", file_ids=["file_..."])`；`clear` 清空该输入框。支持隐藏文件输入框，多文件上传需要网页输入框支持 `multiple`。

点击网页下载链接后，下载自动登记在个人文件区。页面观察的 `downloads` 或 `browser_download(operation="list")` 返回编号及 `downloading/ready/failed` 状态。使用 `wait` 等待完成，`send` 才会发送文件到当前聊天；平台不支持文件消息时明确返回发送失败，文件仍可保留或删除。自己下载的文件也可以再次上传。

模型不能传入服务器路径，也不能访问其他用户的文件编号。每人最多 20 个文件、合计 200 MiB，单文件最多 50 MiB，同时最多 3 个下载；下载后台最多等待 120 秒，`wait` 调用仍受单次工具时限限制，超时不会主动取消后台下载。下载完成后校验大小并删除超限文件，浏览器接收过程的临时缓存可能超过该大小。文件可以跨会话使用，在删除文件或卸载/重载插件时清理；关闭浏览器会话不会删除已完成文件，但会中断未完成下载。

### JavaScript 弹窗

弹窗需要先预设，再触发：例如先调用 `browser_dialog(operation="accept", dialog_type="confirm", message="确认操作")`，再点击页面按钮。`dialog_type` 和 `message` 为空时不限制；填写后必须完全匹配，命中一次就消耗规则，其他弹窗自动取消。`prompt_text` 用于接受 prompt 时输入文字，`clear` 删除尚未使用的规则，`list` 返回待用规则和最近 10 次历史。它不会使网页一直停在弹窗上等待下一轮工具调用。

<a id="advanced"></a>

## 🧪 高级控制

### 网络监听和请求拦截

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

### 设备和环境模拟

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

### 主动执行 JavaScript

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

<a id="boundaries"></a>

## 🛡️ 边界与错误

普通 Agent 会话启用 JavaScript、允许联网，并独立保留 Cookie 和存储；CDP 则使用指定外部浏览器的默认 Context。个人白名单控制谁可以调用工具，不限制目标网站或主机网络地址。共享渲染 API 的页面包装和访问范围见[共享浏览器边界](./共享浏览器.md#boundaries)。

### 错误结果与处理

BrowserUse 工具失败返回 `success=false` 以及 `error.code`、`error.message`。

| 错误码 | 含义与处理 |
| --- | --- |
| `PERMISSION_DENIED` | 发送者未授权，检查管理员身份或个人白名单 |
| `BROWSER_USE_DISABLED` | BrowserUse 已关闭，开启并重载后重试 |
| `TOOL_LEVEL_REQUIRED` | 当前工具不在配置层级中，按任务需要调整层级 |
| `VISION_UNAVAILABLE` | 当前模型或提供商未配置图片输入能力 |
| `HEADED_DISABLED` / `HEADED_BUSY` | 有头入口关闭或被其他会话占用，调整配置或稍后重试 |
| `CDP_DISABLED` / `CDP_PERMISSION_DENIED` | CDP 未启用、当前用户不是管理员，或不匹配配置的使用者 ID |
| `CDP_BUSY` | 外部浏览器被另一聊天会话占用，先结束该会话 |
| `CDP_INVALID_ENDPOINT` | 调试地址格式无效；修正或清空 `endpoint` 后保存并重载插件，其他功能不受影响 |
| `CDP_DISCOVERY_FAILED` | 检查浏览器远程调试开关、用户数据根目录和文件读取权限 |
| `CDP_MULTIPLE_BROWSERS` | 自动发现有多个目标，或简写 WebSocket 的端口匹配多个路径；填写 `user_data_dir` 或完整地址明确选择目标 |
| `CDP_CONNECTION_FAILED` / `CDP_DISCONNECTED` | 检查浏览器授权、远程调试开关及进程，明确请求重新接入 |
| `CDP_EXISTING_TABS_DISABLED` / `CDP_UNSUPPORTED` | 未允许接入已有页面，或当前操作不适用于外部浏览器 |
| `SESSION_EXPIRED` / `NO_SESSION` | 会话已回收或尚未创建，需要重新打开网址 |
| `SESSION_LOST` | 浏览器意外重启导致旧会话失效，需要重新打开网址 |
| `TIMEOUT` | 工具超过总时限，操作可能已经发生，应先观察再决定是否重试 |

元素或观察编号失效时，重新观察后使用新的编号。工具超时后应先确认页面和外部操作的结果，再决定是否重复提交表单或发送文件。

网页文字作为任务资料使用，任务目标来自用户。BrowserUse 使用主机网络与当前网站登录权限，个人白名单控制谁可以调用工具，不代表对目标网站或网络地址进行了额外访问限制。

<a id="faq"></a>

## 🧯 故障排查

浏览器安装、启动、排队和实例空闲关闭问题见[共享浏览器故障排查](./共享浏览器.md#faq)。

### 没有可用工具或调用被拒绝

确认 BrowserUse 已开启，模型支持工具调用，目标人格启用了相应工具，工具位于所选层级。显式设置提供商 `modalities` 时，工具调用需包含 `tool_use`。更改插件配置后重载生效。

`PERMISSION_DENIED` 表示当前发送者未授权：使用 AstrBot 管理员账号，或将个人用户 ID 加入 `browser_use.user_whitelist`。群号不能代替个人 ID，同群其他用户不会继承权限。

### 截图不可见或没有浏览器窗口

截图观察要求支持视觉的模型，且提供商 `modalities` 明确包含 `image`。截图以图片内容交给模型，不会自动发送到聊天。

无头模式没有窗口。有头窗口显示在 AstrBot 所在主机，需要开启有头入口、明确请求 `headed` 并具有可用图形桌面。远程服务器上的窗口不会显示在聊天用户的本地电脑上。

### 会话重开后仍需登录

首先确认是在同一消息来源、同一发送者身份下重新打开。快照保存 Cookie、Local Storage 和 IndexedDB，不保存页面或 sessionStorage；网站也可能使凭据过期，要求重新验证。

正常结束会话才自动保存。若曾调用 `clear`、发生浏览器崩溃、强制停止或保存失败，可能没有最新快照；查看日志中的自动保存或恢复提示。快照损坏时会从空白会话开始，不会强行加载。

<a id="development"></a>

## 🛠️ 开发验证

环境准备及完整检查命令见[开发验证](./共享浏览器.md#development)。

`tests/test_agent_browser.py` 自动选择已安装的 Chromium，启动本地测试网站验证表单和鼠标操作、上传下载、弹窗、结构化提取、精确等待、登录快照恢复、多标签页、iframe、Shadow DOM、截图坐标、会话隔离和取消清理；没有浏览器时明确跳过。`tests/test_browser_storage.py` 验证文件配额、失败清理与快照写入。测试不调用外部模型 API；截图结果另外经过 AstrBot 的真实工具执行器和 Agent 循环，验证图片进入下一次模型请求。

`tests/test_browser_cdp.py` 使用临时用户数据目录启动独立调试浏览器，验证调试文件与端口校验、登录复用、页面范围、网络规则与弹窗、原生视口坐标、管理员权限及占用、失败与取消后的恢复、断线与退出清理，以及普通会话与共享渲染不受影响。自动连接测试覆盖 Chrome/Edge/Chromium 标准目录、多目标选择、自定义目录、手动 HTTP/WebSocket 地址及优先级、按端口补齐 WebSocket 路径、省略协议时的文件优先与同端口 HTTP 发现、Snap/Flatpak 目录、HTTP 重定向与返回地址校验、动态调试端口、授权等待与取消；Linux 安装 Xvfb、X11、XTest 时，还会在隔离虚拟桌面中启动没有远程调试命令行参数的 Chrome 和已安装的 Edge，验证原生授权拒绝、允许与断开后保留浏览器。缺少测试环境组件或对应浏览器时仅跳过相应的原生弹窗测试；插件运行无需这些组件。
