# 项目初始化设计：claude-sessions

日期：2026-10-16

## 背景

`本仓库目录` 是一个监控 Claude Code 会话的 Web 服务。当前目录下只有一个功能完整的 `monitor.py`（纯标准库实现，轮询 `~/.claude/projects/*/*.jsonl` 转录文件，提供 HTTP 看板、JSON API、SSE 事件流和 webhook 推送），尚未纳入任何包管理或版本控制。

## 目标

把现有脚本初始化为用 **uv** 管理、用 **git** 做版本控制的项目。已与用户确认两条约束：

1. **保持单文件**：`monitor.py` 留在根目录，不重构为包结构，代码零改动。
2. **保持纯标准库**：不引入任何第三方依赖，uv 只负责管理 Python 版本和项目元数据。

## 具体内容

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | 项目元数据：名称 `claude-sessions`，版本 `0.1.0`，`requires-python = ">=3.9"`，无依赖 |
| `.python-version` | 固定 uv 使用的 Python 版本 |
| `.gitignore` | 忽略 `.venv/`、`__pycache__/`、`*.pyc` 等 Python 常见产物 |
| `README.md` | 项目简介、运行方式（`uv run monitor.py`）、HTTP 接口列表 |
| `uv.lock` | uv 生成的锁文件，提交入库 |

`monitor.py` **不做任何修改**。

## 执行步骤

1. `uv init`（或手工编写 `pyproject.toml`）生成项目元数据
2. 编写 `.gitignore` 和 `README.md`
3. `git init`，创建首次提交（包含 `monitor.py`、设计文档及上述所有文件）

## 非目标（YAGNI）

- 不重构代码、不拆分模块
- 不引入 Web 框架或任何第三方依赖
- 不添加测试、CI、lint 配置
