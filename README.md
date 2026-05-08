<div align="center">

# Codex AnyRoute Transfer

**面向 Codex 桌面端 / Codex CLI 的本地中转转发器**

在本机启动 `127.0.0.1:18180`，把 Codex 的 OpenAI Responses 请求转发到 AnyRouter，
并支持 `gpt-5.5` / `gpt-5.3-codex` / `claude-opus-4-7[1m]` 等模型映射与优先级编排。

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-4493f8)](#)

</div>

---

## ⬇️ 下载即用（推荐）

直接下载 Release 中的单文件 EXE，**双击即可运行**，无需安装 Python 或配置环境：

> **📦 [CodexAnyRoute.exe - 点击下载最新 Release](https://github.com/liar-ac/Codex-AnyRouter-Transfer/releases/latest)**
>
> 支持 Windows x64 · 单文件 · 无需安装 · 约 **~15 MB**

---

## ✨ 主要特性

- **本地 OpenAI Responses 兼容入口** — `http://127.0.0.1:18180/v1`，Codex 桌面端 / Codex CLI 都可以直接指向。
- **AnyRouter 上游 + 模型映射** — 在 GUI 里维护"Codex 看到的模型 → 实际上游模型"的映射关系。
- **三档模型优先级失败兜底** — 第一模型失败时自动切换到第二、第三，永不让 Codex 卡死。
- **Codex 配置一键写入 / 一键还原** — 写 `~/.codex/config.toml` 与 `auth.json`，并能干净恢复官方 Plus 订阅状态。
- **Plus / API 模式聊天记录互通** — 转发服务运行时把双侧未归档线程同步到同一份本地历史。
- **单实例运行 + 系统托盘** — 重复启动会提示已在后台，关闭按钮隐藏到托盘而不是真正退出。
- **运行日志实时显示 + 落盘** — GUI 里看实时流，文件留存在 `%APPDATA%\codex-anyroute\logs`。

## 🧭 工作原理

```
┌──────────────┐      ┌──────────────────────────┐      ┌──────────────────┐
│  Codex App   │      │  Codex AnyRoute Transfer │      │     AnyRouter     │
│  / Codex CLI │ ───▶ │  127.0.0.1:18180/v1      │ ───▶ │  上游模型供应商    │
└──────────────┘      │  · 模型映射 + 优先级       │      │  · /v1/responses  │
                      │  · Codex 配置守护          │      │  · /v1/messages   │
                      │  · 流式 SSE 透传 / 转换    │      └──────────────────┘
                      └──────────────────────────┘
```

- `gpt-5.x` 系列被视为 OpenAI Responses 原生模型，**直通** `/v1/responses`。
- `claude-*` 系列会被翻译成 Anthropic Messages 协议，发往 `/v1/messages`，并按需附带 1M 上下文 beta header。
- 任何一档模型失败、限流或 panic，转发器会自动尝试下一档；最终兜底是 `gpt-5.5`。

## 🚀 快速开始

> 仅在 Windows + Python 3.12 测试过。其它平台理论可跑，但 PyInstaller 打包脚本是 PowerShell。

### 方式一：直接运行 EXE（推荐）

从 [Release 页面](https://github.com/liar-ac/Codex-AnyRouter-Transfer/releases/latest) 下载 `CodexAnyRoute.exe`，双击运行即可。约 **~15 MB**，单文件、绿色版。

### 方式二：从源码运行

```powershell
# 1. 创建虚拟环境并安装依赖
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 直接运行（开发模式）
.\.venv\Scripts\python.exe app.py
```

### 方式三：自己打包 EXE

```powershell
.\build.ps1
# 产物：dist\CodexAnyRoute.exe（~15 MB）
```

如果想要带控制台输出的调试构建：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --workpath build-venv CodexAnyRouteConsole.spec
# 产物：dist\CodexAnyRouteConsole.exe
```

## ⚙️ 在软件中需要做什么

打开软件后，依次走完三个页面：

| 页面 | 必填项 | 说明 |
| --- | --- | --- |
| **提供商** | `API Base URL`、`API Key`、模型优先级 | URL 推荐 `https://anyrouter.top`，不带 `/v1` 后缀 |
| **模型映射** | `Codex 模型 → 上游模型` | 留空表示沿用"提供商"页的第一模型 |
| **转发服务** | 端口、网关 Key | 默认 `18180`，避开 Codex App Transfer 的 `18080` |

在"转发服务"页点击 **一键写入 Codex 配置**，软件会写入下面四份文件：

```
%USERPROFILE%\.codex\config.toml
%USERPROFILE%\.codex\auth.json
%USERPROFILE%\.codex\state_5.sqlite
%USERPROFILE%\.codex\.codex-global-state.json
```

写入的 `config.toml` 片段（managed block，软件可识别并精确清理）：

```toml
model = "gpt-5.5"
model_provider = "codex-anyroute"
model_context_window = 1000000

[model_providers.codex-anyroute]
base_url = "http://127.0.0.1:18180/v1"
wire_api = "responses"
experimental_bearer_token = "<local-gateway-key>"
```

恢复 Plus 订阅时，点击 **切回官方 Plus 配置**：软件会移除 `anyroute` managed block，删掉 `OPENAI_API_KEY`，并把 `auth_mode` 切回 `chatgpt`。**ChatGPT OAuth 登录令牌会被保留**，无需重新登录。

如果对配置状态没把握，先点 **诊断 Codex 配置** —— 软件会跑 10 项检查（端口、bearer、provider 块、auth.json、工作区信任、Codex CLI 可用性…），并把结果落到运行日志里。

## 🧱 关键路径与数据

| 路径 | 用途 |
| --- | --- |
| `%APPDATA%\codex-anyroute\config.json` | 本软件配置（API Key 只在本地保存，不会进仓库） |
| `%APPDATA%\codex-anyroute\logs\proxy-YYYY-MM-DD.log` | 每日运行日志 |
| `%USERPROFILE%\.codex\config.toml` | Codex 全局配置（managed block 由本软件维护） |
| `%USERPROFILE%\.codex\auth.json` | Codex 登录态（API key 模式 / OAuth 模式都能识别） |

## 🛡️ 隐私与安全

- API Key 只保存在本机 `config.json`，不会进入 git 仓库（`.gitignore` 已显式排除）。
- 写入 Codex 的 `experimental_bearer_token` 是本地网关 Key，仅用于 Codex 访问 `127.0.0.1`，不会发到 AnyRouter。
- 运行日志会脱敏写入文件；不会上传任何遥测。

## ❓ 常见问题

**Q：ClaudeOpus4.7 一直 503 / 429 怎么办？**
A：AnyRouter 侧需要使用 `claude-opus-4-7[1m]` 链路，并且需要在 AnyRouter 控制台开启 1M 上下文。软件会自动附加 1M beta header。如果仍报错，请确认 AnyRouter 1M 是否启用，并启用模型自动切换让转发器降级到 `gpt-5.5`。

**Q：Codex 桌面端打开后历史列表是空的？**
A：可能是 Codex 已经在后台缓存。重新进入 API/Plus 页面、或者点一次"保存并重启转发"通常能刷新出来。

**Q：双击 EXE 没反应？**
A：单实例机制会拦截重复启动 —— 软件已经在后台运行了，请去托盘里"打开窗口"；要彻底退出请右键托盘 → 退出。

**Q：端口 18180 被占用？**
A：在"转发服务"页改一个端口，然后点击"一键写入 Codex 配置"和"保存并重启转发"。

## 🧪 开发须知

- 单文件结构：所有逻辑都在 `app.py` 里。前 2700 行是 FastAPI 转发与 Codex 配置守护，后 1200 行是 Tkinter 界面（`MainWindow`）。
- 配色 / 字体 / 布局参数在 `MainWindow` 类的常量里（搜 `COLOR_` 与 `FONT_`）。
- PyInstaller spec 文件保留在仓库（`CodexAnyRoute.spec` / `CodexAnyRouteConsole.spec`），打包以 spec 为准，便于隐藏 imports 等设置统一管理。

## 📜 License

[MIT](LICENSE) © Codex AnyRoute Transfer contributors
