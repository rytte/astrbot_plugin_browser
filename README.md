# 🌐 浏览器服务

![rytte-browser](https://count.moeyy.cn/@rytte-browser?name=rytte-browser&theme=booru-yuyuyui&padding=7&offset=0&align=top&scale=1.0&pixelated=1&darkmode=auto)

<p align="center">
  <a href="https://github.com/rytte/astrbot_plugin_browser"><img src="https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2Frytte%2Fastrbot_plugin_browser%2Fmain%2Fmetadata.yaml&amp;query=%24.version&amp;label=version&amp;color=blue" alt="Version" /></a>
  <a href="https://github.com/AstrBotDevs/AstrBot"><img src="https://img.shields.io/badge/AstrBot-%3E%3D4.27%2C%3C5-5965F2" alt="AstrBot" /></a>
  <a href="https://playwright.dev/python/"><img src="https://img.shields.io/badge/Playwright-%3E%3D1.58%2C%3C2-2EAD33" alt="Playwright" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue" alt="License" /></a>
</p>


让 AstrBot 浏览真实网页，也为其他插件提供共享的截图和 PDF 渲染服务。

`astrbot_plugin_browser` 提供 **23 个 BrowserUse 模型工具**，覆盖网页阅读、鼠标与表单操作、文件上传下载、登录状态保存和高级控制；同时提供 **HTML → PNG / PDF** 接口，让 Markdown 渲染、网页卡片和报告导出等插件共用浏览器。

**支持三种浏览器类型**

* 内置无头浏览器（无窗口）
* 内置有头浏览器（有窗口）
* 本地浏览器（接管你已打开的浏览器）

[功能亮点](#features) · [快速开始](#quick-start) · [能力概览](#capabilities) · [常用配置](#configuration) · [BrowserUse 文档](./docs/BrowserUse.md) · [共享浏览器文档](./docs/共享浏览器.md) · [开发接入](./docs/共享浏览器.md#integration)

<a id="features"></a>

## 🌟 功能亮点

### 🤖 BrowserUse 网页操作

通过自然语言提出任务，模型根据页面观察连续完成浏览操作。

| 能力 | 可以做什么 |
| --- | --- |
| 网页阅读与观察 | 打开网页、读取页面文字和交互元素，查看页面截图 |
| 鼠标与表单 | 点击、双击、右键、悬停、拖拽、输入、按键、选择与滚动 |
| 多轮操作 | 保留个人浏览会话，在多个标签页间继续任务 |
| 文件与内容 | 上传消息附件、下载并发送文件，提取表格、链接、列表和正文 |
| 登录状态记录 | 会话结束时保存登录状态，避免每次重新登录，登录状态由无头和有头模式共用 |
| 有头模式支持 | 明确请求时打开有头窗口，方便人工登录和操作 |
| 本地浏览器控制（CDP） | 接管已打开的本地浏览器窗口，复用登录状态 |
| 高级控制 | 网络监听、请求拦截、设备环境模拟与页面 JavaScript 执行 |

默认使用无头浏览器，不显示窗口。使用有头浏览器或本地浏览器需要明确指示。

### 🧩 共享渲染服务

为其他 AstrBot 插件提供统一的浏览器接口，减少各插件重复启动 Chromium 的资源开销。

| 能力 | 可以做什么 |
| --- | --- |
| HTML 渲染 | 将 HTML 渲染为 PNG，支持整页、当前视口和指定元素截图 |
| PDF 导出 | 设置纸张、方向、边距与打印背景，生成 PDF 文件内容 |
| 自定义页面操作 | 获取临时页面，自行测量内容、分页或控制截图细节 |
| 资源管理 | 共用执行并发与排队限制，可选空闲关闭，下次调用自动启动 |

默认使用无头浏览器，不显示窗口。联网浏览会话与离线渲染任务分别隔离，渲染完成后释放临时页面。[其他插件接入参考](./docs/共享浏览器.md#integration)

<a id="quick-start"></a>

## ⚡ 快速开始

### 1. 准备环境

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>=4.27,<5` |
| Python 依赖 | `playwright>=1.58,<2`，安装在运行 AstrBot 的同一环境中 |
| 浏览器 | Playwright Chromium 或本机 Chromium 系浏览器，缺失时会自动下载 |
| 模型 | 使用 BrowserUse 时需要支持工具调用；截图观察还需要支持图片输入 |
| 图形桌面 | 仅有头模式需要，桌面位于 AstrBot 所在主机 |

### 2. 安装插件

插件市场搜索 `浏览器服务` 或 `astrbot_plugin_browser`，点击安装即可。

启用 BrowserUse 或共享渲染服务时，插件加载会自动按以下顺序准备 Chromium：

1. 使用 Playwright 已安装的 Chromium。
2. 查找本机可用的 Chromium、Chrome、Edge、Brave、Vivaldi 或 Opera。
3. 如果仍未找到，自动执行 `python -m playwright install chromium`。

因此通常不需要手动执行浏览器安装命令。自动下载需要网络和当前 Python 环境的写入权限。

> [!TIP]
>
> Linux 如果自动下载后仍缺少浏览器系统依赖，可以手动安装：
>
> ```bash
> python -m playwright install --with-deps chromium
> ```

### 3. 启用浏览工具

在插件配置的 **BrowserUse** 组中，选择所需的“模型工具层级”，然后在 AstrBot 工具管理中为目标人格启用相应的 `browser_*` 工具。

默认使用“基础浏览”，适合先体验阅读、搜索和表单操作。需要上传下载等能力时，再选择“完整交互”或“高级控制”。保存配置后重载插件生效。

### 4. 试着说一句

按当前会话的唤醒方式与机器人对话，例如在群聊中先 @ 机器人，再提出请求。

| 场景 | 示例请求 | 工具层级 |
| --- | --- | --- |
| 阅读网页 | “打开 https://example.com，概括页面内容。” | 基础浏览 |
| 搜索与整理 | “打开我提供的商品网站，搜索无线键盘，按价格排序，整理前三项结果。” | 基础浏览 |
| 上传附件 | “把这条消息附带的文件上传到网页的附件栏。” | 完整交互 |
| 下载文件 | “下载这份报告，完成后把文件发到当前聊天。” | 完整交互 |
| 人工登录 | “打开有头浏览器，我来手动登录。” | 基础浏览，需图形桌面 |

截图用于模型观察页面，不会自动作为图片消息发送到聊天中。上传下载还取决于当前平台的附件与文件消息支持。

### 5. 本地浏览器接管（可选）

通过 CDP 可以连接 **AstrBot 所在主机**上正在使用的 Chrome / Edge 等 Chromium 内核浏览器，操作新页面或已有页面。

此功能默认关闭，仅允许 AstrBot 管理员使用。

1. 在插件配置中打开 **允许连接本地浏览器（CDP）**，保存并重载。
2. 在已打开的浏览器地址栏输入 `chrome://inspect/#remote-debugging` 并回车，在页面中启用远程调试（`Allow remote debugging for this browser instance`），无需重启浏览器。
3. 对机器人说：“连接本地浏览器”，连接时在浏览器弹出的**授权窗口中点击允许**。

**可选配置说明**

* **本地浏览器调试地址（可选）**：在 `chrome://inspect/#remote-debugging` 页面中勾选启用远程调试时下面会弹出一行 **Server running at: `127.0.0.1:9222`**，插件通常会优先连接该地址，如果显示的端口并非 `9222`（例如 `127.0.0.1:41063`），并且出现连不上的情况，可以尝试将该地址填入该配置。
* **浏览器用户数据根目录（可选）**：对于 Chrome、Edge、Chromium ，插件会默认查找该标准数据目录。对于其他浏览器/ 查找失败 / 发现多个浏览器调试信息的情况，可以手动填写该配置。从地址栏进入 `chrome://version` 页面，找到 `用户配置路径（Edge） / 个人资料路径（Chrome）`，复制路径，去掉最后的 `/Default`，填入该配置。

> Docker 中的本机地址指容器自身，默认无法直接连接宿主机 Chrome。
>
> 详细说明见 [本地浏览器控制（CDP）](./docs/BrowserUse.md#cdp)。

<a id="capabilities"></a>

## 🧬 BrowserUse 网页操作

### 工具层级

高层包含低层的全部工具，未启用的工具定义不占模型上下文。

| 配置值 | WebUI 选项 | 工具数量 | 适用范围 |
| --- | --- | ---: | --- |
| `basic`（默认） | 基础浏览 | 13 | 网页观察、截图、点击与表单、标签页、等待、模式切换 |
| `standard` | 完整交互 | 19 | 增加拖拽、上传下载、结构化提取、弹窗和登录状态管理 |
| `full` | 高级控制 | 23 | 增加网络监听、请求拦截、设备模拟和 JavaScript 执行 |

完整工具清单与参数见 [BrowserUse 工具](./docs/BrowserUse.md#tools)。

### 优化功能

| 功能 | 行为说明 |
| --- | --- |
| [页面观察精简](./docs/BrowserUse.md#history) | 用于优化网页工具返回的工具结果（主要是页面内容），造成的大量上下文占用。整轮最终回复后、保存历史前，精简本轮全部观察的网页正文、元素清单和截图。会保留网址、标题、正文提取、脚本执行结果、文件信息和错误。 |
| [CDP 地址与目录发现](./docs/BrowserUse.md#cdp) | 自动查找 Chrome、Edge、Chromium 的标准用户数据目录并读取调试地址，重新连接时重新读取 |
| [登录状态保存与恢复](./docs/BrowserUse.md#storage) | 正常结束、关闭最后一个标签页、空闲回收或正常停止插件时尝试保存，下次创建同一身份的会话时自动恢复；崩溃或强制终止可能丢失最新状态；CDP 使用外部浏览器自身的登录状态 |
| [空闲会话回收](./docs/BrowserUse.md#lifecycle) | 自动回收长时间未操作的 Agent 会话，释放页面资源 |
| [无头浏览器空闲关闭](./docs/共享浏览器.md#lifecycle) | 没有任务和保留的无头会话后计时关闭实例，下次浏览或渲染请求自动启动 |

### 权限与规则

| 功能                                              | 行为说明                                                     |
| ------------------------------------------------- | ------------------------------------------------------------ |
| [使用权限](./docs/BrowserUse.md#permissions)      | 管理员与白名单用户可在私聊和任意群聊使用；白名单仅匹配发送者 ID，同 ID 跨平台通用 |
| [会话隔离](./docs/BrowserUse.md#permissions)      | 页面、文件和登录快照按聊天来源、发送者隔离；不同上下文不共享 Cookie、Local Storage 和页面状态 |
| [有头浏览与模式切换](./docs/BrowserUse.md#headed) | 默认后台浏览；明确请求后在 AstrBot 所在主机显示窗口，会话结束后释放；最多一个有头实例和一个有头会话；切换有头/无头模式保留登录存储并重建页面，需重新打开网址 |

## 🧬 共享渲染服务

### 一个浏览器，多个插件共享

浏览器是比较占资源的组件。如果 Markdown 渲染、网页卡片、报告导出等插件各自启动一套 Chromium，很快就会出现多个浏览器进程、重复的 Playwright 驱动和重复的缓存。

默认只维护一个共享的无头 Chromium 实例；额外的有头实例仅在明确请求后存在：

```text
AstrBot
  ├─ Markdown 渲染插件 ─┐
  ├─ 报告导出插件      ─┼─> 共享 Browser(Chromium) ─> 独立 BrowserContext
  └─ 网页卡片插件      ─┘
```

这样可以减少重复的浏览器进程和基础开销，通常比每个插件各自启动浏览器更省内存。还可以在配置中限制同时运行的页面任务数量，避免并发渲染把内存一次性推高。

#### 浏览器内存结构示例

```bash
Chromium（浏览器实例）									# 150～300 MB
└── BrowserContext（独立会话：Cookie、登录状态、缓存等）  # 10～60 MB
    ├── Page（标签页）								 # 10～80 MB
    └── Page（标签页）								 # 10～80 MB
```

### HTML 截图和 PDF 一套接口

- HTML 渲染为 PNG 字节，可直接交给 `Image.fromBytes()`。
- HTML 渲染为 PDF 字节，可直接保存或交给文件消息组件。
- 支持完整 CSS、字体等待、元素选择器截图和整页截图。

### 离线渲染接口

- `render_html()`、`render_pdf()` 和 `session()` 默认禁用 JavaScript。
- 这些渲染接口的页面请求全部拦截，不访问外部网站、外部图片或本地文件。
- Agent 工具使用独立的联网会话，不改变渲染接口的离线约定。

如果业务确实需要脚本，可以只对单个 `session()` 调用设置 `javascript_enabled=True`。这不会改变其他任务的默认设置。

### 其他插件如何接入

通过 AstrBot 的插件上下文获取已注册的受限 `service`，再调用以下接口。生命周期控制和底层 Playwright 对象不会作为插件间接口暴露：

| 接口 | 返回或提供的内容 |
| --- | --- |
| `render_html()` | PNG 图片字节，可交给 AstrBot 的图片消息组件 |
| `render_pdf()` | PDF 字节，可保存或交给文件消息组件 |
| `session()` | 自动清理的受限页面，供调用方排版、测量和截图 |

`session()` 返回 `RenderPage`，`locator()` 返回 `RenderLocator`；它们只公开必要的渲染方法，不提供原生 Page、Context 或 Browser 的关联访问，也不接受本机文件路径。支持操作超时、截图质量与裁剪、元素链式定位等常用渲染能力；需要文件时由调用方保存返回的字节。

渲染接口拦截页面的外部资源请求，HTML 中的 CSS、图片和字体应内联。开启空闲关闭后，这些接口会自动唤醒浏览器。

完整示例和参数见 [其他插件接入](./docs/共享浏览器.md#integration)。

<a id="configuration"></a>

## ⚙️ 常用配置

配置在 AstrBot WebUI 中调整，保存后重载插件生效。

**会话回收与实例关闭分别计时。** 只有没有保留的无头会话、执行中或排队的无头任务时，才开始实例空闲计时。例如会话回收设为 600 秒、实例超时设为 1200 秒，最后一次操作后约 1800 秒释放无头实例。

共用实例的字段、范围与资源生命周期见[共享浏览器配置](./docs/共享浏览器.md#configuration)，模型工具专用设置见[BrowserUse 配置](./docs/BrowserUse.md#configuration)。

<a id="faq"></a>

## 🧯 常见问题

| 现象 | 检查方向 |
| --- | --- |
| 提示没有权限 | 确认发送者是 AstrBot 管理员，或已将其个人 ID 加入 BrowserUse 白名单并重载插件 |
| 模型没有使用浏览工具 | 检查模型的工具调用能力、BrowserUse 开关、目标人格工具启用情况与所选工具层级 |
| 没有浏览器窗口 | 默认是无头模式；只有明确请求有头模式且主机有可用图形桌面时才显示窗口 |
| 模型无法查看截图 | 使用支持图片输入的模型，并在 AstrBot 提供商配置中启用 `image` 能力 |
| 渲染缺少图片或字体 | 离线渲染不加载外部资源，请将资源内联到 HTML 中 |

浏览器安装与排队超时见[共享浏览器故障排查](./docs/共享浏览器.md#faq)，工具权限与登录恢复见[BrowserUse 故障排查](./docs/BrowserUse.md#faq)。

## 🤝 反馈与贡献

遇到问题或有改进建议，欢迎提交 [Issue](https://github.com/rytte/astrbot_plugin_browser/issues)；想一起完善功能，也欢迎提交 [Pull Request](https://github.com/rytte/astrbot_plugin_browser/pulls)。

开发环境与验证方式见 [开发验证](./docs/共享浏览器.md#development)。

## 📄 开源协议

本项目采用 GNU Affero General Public License v3.0（`AGPL-3.0`），详见 [LICENSE](./LICENSE)。
