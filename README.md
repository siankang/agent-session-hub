# Agent 会话管理台

一个**纯本地**的 Windows 桌面软件：把本机各个 AI Agent 客户端散落的会话、记忆、缓存和会话数据库统一识别、索引、搜索，并提供带备份与回滚的安全清理。

双击 `AgentSessionHub.exe` 即可使用 —— **不需要浏览器、不显示网址、不监听任何端口、不需要安装 Python**。

## 功能

| 模块 | 说明 |
| --- | --- |
| 统一识别 | 启动时（或点顶栏「重新扫描」）一次识别同时建立会话索引与清理候选，覆盖 30+ 客户端 |
| 会话 | 统一列表、按来源/时间筛选、全文搜索（FTS5 + 中文子串）、逐条消息详情 |
| 来源识别 | 完整解析 / 仅清单 / 仅体量 三级标注 |
| 占用分析 | 按来源、项目、月份、最旧会话统计磁盘占用 |
| 清理 | 会话 / 记忆 / 缓存 / 会话数据库四类只读识别，可安全移入回收站并按批次还原 |
| 导出 | Markdown / JSON，输出到 `exports/` |

支持的客户端包括 WorkBuddy、Claude Code、Codex、Qoder、TRAE、CodeBuddy、Devin、Cursor、Windsurf、CatPaw、Hermes 等，完整清单见 `core/adapters.py`。

> ## ⚠️ 平台与测试范围（请先阅读）
>
> **本项目目前只在 Windows 上测试过。**
>
> - **macOS / Linux 没有测试过**：未在这些平台上做过任何安装、构建或运行验证。
> - 这些平台上的**兼容性与运行情况未经验证**，不保证可用。
> - 当前实现依赖 Windows 专有组件（WebView2 Runtime、Win32 回收站 API、pywebview 的 WinForms 后端等），**不是跨平台实现**。
>
> 请勿把本说明理解为对 macOS / Linux 的支持承诺，也不要假定它们已通过验证。

## 快速开始

### 直接使用（推荐）

从 [Releases](https://github.com/siankang/agent-session-hub/releases/tag/v0.0.1) 下载 `AgentSessionHub.exe`，双击运行。

- 首次启动会在后台做一次统一识别（约 20 秒，顶部有转圈进度）；完成后清理页直接有候选清单。
- 依赖 Windows 10/11 自带的 **WebView2 Runtime**（绝大多数机器已内置）。
- 单实例保护：重复双击会把已有窗口置前，不会开出第二个窗口。

### 从源码运行 / 构建

需要 Python 3.13：

```bash
pip install pyinstaller pywebview pythonnet
python desktop.py          # 源码方式启动桌面窗口
python build_exe.py        # 生成单文件 exe 到项目根目录
```

可选参数：

| 参数 | 作用 |
| --- | --- |
| `--data <path>` | 指定索引库路径 |
| `--scan` | 启动时强制重新扫描会话 |
| `--debug` | 打开 WebView2 调试模式 |
| `--selftest` | 内部自检，结果写入 `data/selftest.json` |

`app.py` 是可选的本地服务模式入口（调试用），日常使用不需要。

## 架构

```
web/index.html  <->  core/bridge.py  <->  core/server.py (API.dispatch)
      UI                JS 桥接                业务内核
                                               +-- core/adapters.py   来源注册表
                                               +-- core/scanner.py    会话索引扫描
                                               +-- core/store.py      SQLite / FTS5
                                               +-- core/cleanup.py    清理识别与执行
```

窗口由 pywebview + 系统 WebView2 渲染，但**不是网页应用**：没有 HTTP 服务、没有端口、没有 URL，前端通过进程内 JS 桥接直接调用 Python 的 `API.dispatch`。所有识别逻辑对源数据只读。

## 清理与数据安全

清理默认不会改动任何文件。执行流程：

1. 只读识别四类候选，标注体积、风险等级与完整路径
2. 逐项勾选 → 生成服务端计划（前端只传 id，路径由服务端取）
3. 核对完整路径 → 输入「确认清理」（**唯一确认关卡，无需额外解锁**）
4. 完整备份到 `backup/quarantine/<批次号>/` 并校验
5. 每批最多 10 项移入 **Windows 回收站**，任一失败立即停止
6. 写 manifest 与日志，可在「历史批次与还原」一键还原

额外约束：工具自身目录、系统/个人目录、身份与长期记忆文件、登录状态一律受保护；路径必须在已注册客户端数据目录内；单项超过 500 MB 拒绝执行；执行前检查磁盘剩余空间。

## 隐私

- 源文件全程只读，不上传、不联网。
- 索引库 `data/index.db` 会保存对话文本副本，**仅存本机**；本仓库不包含任何会话数据、日志或本机侦察报告。
- 导出的 `exports/`、备份的 `backup/` 同样只在本机。
- 建议清理前关闭对应客户端，并保留备份目录。

## 已知限制

- **平台与测试范围**：目前只在 Windows 上测试过；**macOS / Linux 没有测试过，兼容性与运行情况未经验证**（详见顶部醒目声明）。当前实现依赖 Windows 专有组件（WebView2 Runtime、Win32 回收站 API 等），不是跨平台实现。
- LevelDB 类客户端（ZCode、Grok Desktop、OpenCode 等）的对话正文暂未解析，仅统计目录体量。
- 云端会话（豆包、即梦、Coze 等）在本地只有缓存，无法还原对话内容。
- 清理候选是内存态，每次启动会重新识别一次（后台执行，不阻塞窗口）。

## 版本

当前 [v0.0.1](https://github.com/siankang/agent-session-hub/releases/tag/v0.0.1)。
