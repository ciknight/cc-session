<div align="center">

# Claude Sessions

**你的 Claude Code 会话指挥中心。**

集中监控所有项目，随时了解 Claude 正在做什么，在浏览器里处理审批并推进对话——全部汇集在一个实时网页看板中。

[在线文档](https://ciknight.github.io/cc-session/zh-CN.html) · [English](README.md) · [简体中文](README.zh-CN.md)

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-6C4CF1)](https://docs.astral.sh/uv/)
[![MIT license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](#贡献)

</div>

## 为什么选择 Claude Sessions？

Claude Code 很擅长执行任务，但同时运行多个会话后，管理本身就成了难题：哪个还在工作？哪个需要审批？那个子代理完成了什么？之前的长对话又去了哪里？

**Claude Sessions 把这些答案集中到一个地方。** 它监听已有的 Claude Code 转录文件，将它们变成按项目组织的实时看板，并让你直接在浏览器中继续对话或启动托管会话。

- **全局状态一目了然** —— 会话按项目分组，清晰区分工作中、空闲和 stale 状态。
- **真正了解执行过程** —— 查看消息、工具调用、diff、任务、子代理、模型、Git 分支和 token 用量。
- **让工作持续前进** —— 发送追问、在 Claude 忙碌时排队消息、中断当前回合，或创建新会话。
- **始终保留控制权** —— 在浏览器里处理权限请求和 `AskUserQuestion` 提问。
- **在可信网络内随处使用** —— 响应式界面同时适配桌面和手机，通过 SSE 实时更新。
- **方便二次开发** —— 通过 JSON API、SSE 事件流或可选 webhook 接入自己的工具。

## 核心功能

| 能力 | 你能获得什么 |
| --- | --- |
| **实时会话看板** | 自动发现会话、增量更新、项目分组、搜索、筛选、新回复提示、置顶、别名和归档。 |
| **完整的对话视图** | 在易读的聊天界面中查看 Markdown 回复、用户消息、工具结果、diff、任务清单和子代理活动。 |
| **常驻托管会话** | 常驻 `stream-json` Claude 进程，支持流式文本、追问排队、模型选择、思考档位和中断。 |
| **灵活的审批方式** | 每个托管会话可选择网页审批、交给 Claude CLI 权限模式，或自动放行。 |
| **大型历史记录也高效** | 增量解析 JSONL、持久化扫描缓存、子代理懒加载，以及按需分页的消息窗口。 |
| **自包含前端** | 前端依赖全部本地提供，不依赖外部 CDN；后端仅使用 Python、Jinja2 和 psutil。 |
| **系统面板** | 查看宿主机 CPU 和内存，并提供可选的睡眠、定时睡眠和重启操作。 |
| **cc-switch 集成** | 安装 `cc-switch` 后可切换项目 local 配置，带确认和备份机制。 |

## 快速开始

### 环境要求

- Python **3.9+**
- [uv](https://docs.astral.sh/uv/)
- 如果需要发送消息或托管会话，请安装并配置好 [Claude Code](https://code.claude.com/)

只监控已有转录不会启动 Claude，也不会消耗模型 token。远程发消息和托管会话沿用你现有的 Claude Code 配置。

### 启动服务

```bash
git clone https://github.com/ciknight/cc-session.git
cd cc-session

# 仅限本机使用的推荐启动方式
uv run monitor.py --host 127.0.0.1
```

打开 **http://127.0.0.1:8765**，已有的 Claude Code 会话会被自动发现。

如果需要在可信的局域网内访问看板，可以使用默认监听地址或显式指定：

```bash
uv run monitor.py --host 0.0.0.0 --port 8765
```

> [!WARNING]
> 服务**没有身份认证**。它可以向 Claude 发送消息、修改设置、批准工具调用，并执行宿主机电源操作。切勿暴露到公网或不可信网络。详见[安全说明](#安全说明)。

## 选择适合你的使用方式

| 模式 | 适用场景 | 工作方式 |
| --- | --- | --- |
| **监控** | 观察终端中已经在运行的会话 | 增量读取 Claude Code 转录文件，不修改原文件。 |
| **一次性消息** | 偶尔从浏览器发送追问 | 每条消息运行一轮 `claude -p --resume`，按会话自动排队。 |
| **托管会话** | 长时间、持续交互的任务 | 保持常驻 Claude 进程，支持流式输出、消息排队、审批和实时控制。 |

托管已有会话前，请先退出它原来的终端进程，避免两个进程同时写入同一会话。

托管会话默认使用 **`full`** 审批模式，会自动批准权限请求。如果希望逐条审查，请在会话页切换为 **`ask`**；选择 **`auto`** 则交给 Claude Code 自己的权限模式处理。

## 配置

常用示例：

```bash
# 更换端口，并将 10 分钟无更新的会话标记为 stale
uv run monitor.py --port 9000 --stale 600

# 将会话事件转发到另一个本地服务
uv run monitor.py --webhook http://localhost:3000/hook

# 向 Claude 子进程传递额外参数
uv run monitor.py --claude-args "--model sonnet"
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | HTTP 监听地址。仅限本机访问时使用 `127.0.0.1`。 |
| `--port` | `8765` | HTTP 监听端口。 |
| `--interval` | `1.0` | 转录轮询间隔，单位为秒。 |
| `--refresh` | `3.0` | 网页兜底自动刷新间隔，单位为秒。 |
| `--stale` | `300` | 多少秒没有内容事件后将会话显示为 stale。 |
| `--webhook` | 无 | 将每个发出的事件 POST 到该 URL。 |
| `--claude-bin` | `claude` | Claude Code CLI 路径。 |
| `--claude-args` | 空 | 透传给 Claude 子进程的额外参数。 |
| `--msg-timeout` | `600` | 一次性消息的最长执行时间，单位为秒。 |
| `--permission-timeout` | `300` | 托管审批等待秒数，超时拒绝；`0` 表示无限等待。 |
| `--scan-subagents` | 关闭 | 启动时扫描全部子代理转录，而不是按需加载。 |

监控其他 Claude 配置目录：

```bash
CLAUDE_CONFIG_DIR=/path/to/.claude uv run monitor.py --host 127.0.0.1
```

## JSON API、SSE 与 webhook

网页看板只是服务的一个客户端。你也可以把同样的实时数据用于脚本、通知工具或自己的界面。

| 接口 | 用途 |
| --- | --- |
| `GET /sessions` | 获取会话概览；`?full=1` 返回详情，`?include_subagents=1` 加载子代理。 |
| `GET /sessions/<id>` | 获取单个会话，包括任务、子代理、待处理交互和托管状态。 |
| `GET /sessions/<id>/messages` | 分页读取对话消息（`limit`、`before`、`from`）。 |
| `GET /events` | 订阅 SSE 事件流；`?session=<id>` 只接收单个会话。 |
| `GET /interactions` | 获取待处理的权限和提问交互。 |
| `POST /sessions/<id>/message` | 使用 `{"text":"..."}` 发送或排队一条消息。 |
| `POST /new` | 在项目目录中创建新的一次性会话。 |
| `POST /hosted` | 创建或恢复常驻托管会话。 |
| `GET /sessions/<id>/jobs` | 查看执行中、排队中和最近结束的任务。 |
| `POST /sessions/<id>/interrupt` | 中断当前托管回合并清空排队消息。 |
| `POST /sessions/<id>/approval` | 设置 `ask`、`auto` 或 `full` 审批模式。 |
| `POST /sessions/<id>/effort` | 设置托管思考档位：`low`、`medium`、`high` 或 `default`。 |
| `GET /jobs/<id>` | 查询发消息任务。 |
| `GET /healthz` | 健康检查。 |
| `GET /system` | 获取宿主机 CPU、内存和定时睡眠状态。 |

例如：

```bash
curl http://127.0.0.1:8765/sessions
curl -N http://127.0.0.1:8765/events
```

## 工作原理

```text
~/.claude/projects/*/*.jsonl
              │
              ▼
        增量轮询 + 扫描缓存
              │
              ▼
         monitor.py ───────────► JSON API / SSE / webhook
              │
              ▼
          响应式网页看板
              │
              ├──► claude -p --resume      （一次性消息）
              └──► 常驻 stream-json 进程   （托管会话）
```

Claude Sessions 读取 Claude Code 已经写入的转录文件。它维护每个文件的读取 offset，能够拼接不完整的行，通过校验扫描状态实现重启后的快速恢复，并且只在请求时解析对话消息。子代理转录默认按需加载。

监控流程不会改写你的转录。会话整理信息和网页偏好单独保存在 `<claude_dir>/claude-sessions/` 下。

## 开发

安装依赖并启动服务：

```bash
uv sync
uv run monitor.py --host 127.0.0.1
```

运行测试：

```bash
# Python
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v

# 前端控件
node --test tests/test_frontend_controls.js
```

在 Windows PowerShell 中，先为 Python 测试命令设置 `PYTHONPATH`：

```powershell
$env:PYTHONPATH = '.'
uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

项目刻意保持轻量：Python 标准库 HTTP 服务、Jinja2 模板、psutil 系统采样，以及自包含的 JavaScript 前端。

双语文档网站位于 [`docs/`](docs/)。当 `main` 分支上的站点文件发生变化时，[`.github/workflows/pages.yml`](.github/workflows/pages.yml) 会自动部署到 [GitHub Pages](https://ciknight.github.io/cc-session/zh-CN.html)。本地预览可以直接打开 `docs/index.html`，或使用任意静态 HTTP 服务托管该目录。

## 贡献

欢迎提交 issue、bug 报告、文档改进和 pull request。

1. 较大的改动请先创建 issue，以便讨论方案。
2. 保持改动聚焦，并维持项目轻量、本地优先的设计。
3. 运行相关的 Python 和前端测试。
4. 用户可见行为发生变化时，请同步更新中英文 README。

## 许可证

Claude Sessions 采用 [MIT 许可证](LICENSE)。在遵守许可证条款的前提下，你可以自由使用、修改和分发本项目。

## 安全说明

- 服务**没有身份认证，也没有多用户隔离**。只能在可信机器或私有网络中运行。
- 推荐使用 `--host 127.0.0.1`；默认的 `0.0.0.0` 会监听所有网卡。
- 远程发消息会以你的本地权限和环境执行 Claude。
- 托管 **`full`** 模式会自动批准权限请求；需要人工审查时请使用 **`ask`**。
- 系统面板可以让宿主机睡眠或重启。浏览器确认提示不是安全边界。
- 如果要通过反向代理对外开放，必须自行添加适当的身份认证、授权、传输加密和请求防护。

---

Claude Sessions 是独立的社区项目，与 Anthropic 无隶属关系，也未获得其背书。

如果它让你的 Claude Code 工作流更轻松，欢迎为仓库点星并分享给更多人。
