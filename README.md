# Minimal Agent

以对话为主要入口的本地通用 Agent。支持知识交流、写作、联网查询与研究，以及调用视频总结、内容管理和定时任务。轻量 Harness 共享模型适配、工具执行、持久化、日志与通知。

完整范围与边界见 [需求文档](./需求文档.md)。

## 桌面客户端与对话入口

### 日常对话与联网研究

- 会话列表支持新建、切换、重命名、删除；删除聊天记录不会删除已生成的报告或任务。
- 模型回复逐步显示，支持停止回复、重试上一条、复制、Markdown、代码、表格和 Mermaid 图表。
- “允许联网查询”控制本次请求能否使用网页工具。普通知识问题直接回答；最新信息或资料核对可搜索并读取网页，在回答中附来源。
- “研究某个问题并生成报告”可提交 `web_research` 后台任务：规划查询、搜索、读取正文、发现资料缺口后补充搜索、比较整理、自动保存。
- 搜索使用 Bing RSS，失败时尝试 DuckDuckGo HTML，无需额外 API Key。搜索服务与目标网站可能限制访问，失败会明确返回，不冒充获取成功。网页阅读支持公开的静态 HTML/文本；不提供登录、交互式浏览器、付费内容或任意 PDF 解析。
- “把刚才的回答保存为笔记”通过保存工具写入内容库。普通聊天只保存会话记录，正式报告自动保存为文件。
- 即时任务及定时运行均可取消；取消某次定时运行不影响以后安排。取消是协作式的：模型和网页读取会中断连接，视频下载停止子进程，本地 ASR 在语音分段之间检查停止状态。
- 服务重启后，未完成的回复与任务标为中断；可以查看保留的内容并主动重试，不自动重放有副作用的操作。

可以尝试：“解释 MVVM，并与 MVC 比较”、“搜索这个问题的最新资料并给出来源”、“研究 RAG 的常见评估方法并生成报告”、“每天九点研究某个主题”、“把这段解释保存成笔记”。

`conversation.py` 提供 Agent 循环；`tool_registry.py` 定义厂商无关的工具契约；`web_research.py` 提供搜索、网页读取与研究能力；`runtime_control.py` 提供共享取消控制。DeepSeek SSE 接口保留流式工具参数及内部 `reasoning_content`，按 [官方工具调用说明](https://api-docs.deepseek.com/guides/thinking_mode/)回传，页面只显示回答和执行进度。

macOS 13+ 可双击 `启动桌面客户端.command`，或打开 `dist/Agent 工作台.app`。首次构建运行 `.venv/bin/python build_desktop.py`，需要本机 Xcode Command Line Tools。应用使用原生 AppKit 窗口和 WKWebView，复用现有页面与 Python Harness，不需要打开 Chrome。

本版本的应用连接当前项目目录和 `.venv`，用于本机运行；移动项目后请重新构建。关闭主窗口会隐藏窗口，应用及其定时任务继续运行，可从 Dock 或菜单栏恢复。菜单“退出 Agent 工作台”停止由客户端启动的服务；复用的其他后台服务由其原启动方式管理。菜单“打开内容保存目录”直接打开本地内容库。

默认首页是对话。可以说“总结这个视频 https://…”、“明天九点执行视频总结”、“列出生成的文本”、“打开上一份内容”、“删除指定文本”或“恢复刚才删掉的文本”。Agent 从注册功能中选择工具，任务在后台处理，进度和结果通过对话卡片查看。对话保存在本机，创建新对话会保留旧记录。

生成文本和原图自动保存到项目 `summaries/`，不需要每次下载。内容库支持查询、打开和移入回收站；删除同步隐藏对应任务的正文与输出入口，文件暂存 `.local/trash/notes/` 并可恢复。下载用于另存或分享。

## 功能扩展与通用调度

`capabilities.py` 注册功能 ID、名称、输入 schema、校验器及执行器。对话工具、定时任务的功能列表和参数表单从同一注册表生成；调度器持久化 `capability + parameters + cadence + due_at`，不依赖视频功能。首版只有 `video_summary`。

旧视频定时任务在启动时迁移为视频能力参数并保留执行历史。新增功能只需注册一个 `Capability`，即可通过对话和通用调度调用；各项功能继续共享 Harness、日志、通知和内容保存目录。

定时轮询与执行分开：任务到期先入队，后台按队列执行，记录排队、运行与完成状态。耗时视频不会阻塞后续任务入队；因电脑休眠或服务关闭而错过计划时间的任务仍按原规则跳过。

```bash
python3 agent.py schedule add --daily 09:00 --capability video_summary --parameters '{"url":"https://www.bilibili.com/video/BV...","include_frames":true}'
```

## 本地前端

macOS 可以双击 `启动前端.command` 打开本地页面；数据保存在项目 `.local/`，保持启动窗口开启。

首次使用可双击 `配置运行环境.command`：配置程序会寻找 Python 3.10+，在项目 `.venv/` 安装视频读取、关键帧和本地转录工具，准备本地语音模型，并复用可用的 YouTube JavaScript 运行时。完成后，启动脚本自动选择该独立环境。已经保存的 API Key 无需重新填写。

在项目目录运行：

```bash
python3 agent.py web
```

打开 `http://127.0.0.1:8765`。页面提供视频总结、定时任务、运行记录和模型设置。没有 API Key 时也能打开页面，可在“模型设置”中输入并保存 Key；页面不会回传已保存的 Key。模型设置同时支持切换同一 DeepSeek API 下的模型。

视频处理在后台运行，进度和结果记录在本机。总结支持页面预览和两种下载：默认“下载笔记”生成单个 HTML 文件，内嵌原图和预览中已显示的图表，可用浏览器离线打开；“Markdown（含图片）”下载包含笔记和原图的 ZIP，解压后保持目录结构即可在支持 Markdown 的编辑器中查看，图表使用 Mermaid 源码。页面服务只监听本机地址，关闭终端中的服务后页面 API 停止运行。

如果希望把前端数据保存在当前项目内：

```bash
MINIMAL_AGENT_HOME="$PWD/.local" python3 agent.py web
```

项目的 `.local/` 和 `summaries/` 已加入 Git 忽略。可以通过 `--port 8766` 指定其他端口。

## 环境

- 核心对话可用 Python 3.9+；完整视频环境使用 Python 3.10+。
- DeepSeek API Key
- `yt-dlp` 命令行程序，用于读取公开视频信息和字幕。YouTube 支持可能还需要 Deno 等 JavaScript 运行时；以当前 yt-dlp 提示为准。
- `faster-whisper`，用于没有字幕时在本地转录音频。
- `ffmpeg` 可选，用于提取关键帧。未安装时仍会生成文本总结。

配置完整的视频环境（与双击配置脚本等价）：

```bash
python3 setup_runtime.py
```

配置使用 `yt-dlp[default]`、faster-whisper 和 imageio-ffmpeg 的本地 FFmpeg 文件。YouTube 优先使用 Deno 2.3+，也可使用 Node.js 22+；配置程序会复用本机已安装的合适版本。没有可用运行时时，按 [yt-dlp 官方配置说明](https://github.com/yt-dlp/yt-dlp/wiki/EJS)安装后重新运行配置。

命令行执行完整视频功能时使用 `.venv/bin/python agent.py ...`；页面启动脚本会自动选择该解释器。语音模型和安装缓存保存到数据目录下，不写入 Git。

## 配置

安全地输入 API Key，保存在 `~/.minimal-agent/.env`，文件权限设为仅当前用户可读写：

```bash
python3 agent.py config set-api-key
```

也可以通过 `DEEPSEEK_API_KEY` 环境变量提供 Key。默认模型是 `deepseek-flash`，可切换为账号可用的 DeepSeek 模型：

```bash
python3 agent.py config set-model deepseek-v4-pro
python3 agent.py config set-model deepseek-flash
```

也可以对单次调用使用 `--model`：

```bash
python3 agent.py --model deepseek-flash summarize "https://www.youtube.com/watch?v=..."
python3 agent.py summarize "https://www.bilibili.com/video/BV..." --model deepseek-flash
```

DeepSeek 的接口地址固定为 `https://api.deepseek.com`。模型由 API 请求的 `model` 字段选择；准确可用的模型名以 [DeepSeek 官方接口文档](https://api-docs.deepseek.com/api/create-chat-completion)为准。首版默认 `deepseek-flash`，其图像输入接口按 [DeepSeek Vision 文档](https://api-docs.deepseek.com/guides/vision/)调用。

## 使用

启动对话：

```bash
python3 agent.py
```

总结一个公开视频链接：

```bash
python3 agent.py summarize "https://www.youtube.com/watch?v=..."
python3 agent.py summarize "https://www.bilibili.com/video/BV..."
```

总结会保存到当前目录的 `summaries/`，按内容与知识结构组织中文笔记，成品不显示视频时间戳，不描述讲述者的衣着和动作。开头可有概述与框架图，正文按需要加入小图标、Mermaid 概念图/流程图，以及经视觉分析确认有知识价值的原视频画面。可用 `--no-frames` 只生成文字和图表。

字幕优先；无可用字幕时下载可访问的音频并使用本地 faster-whisper。私有、登录可见或不可访问的视频不支持。音频在临时目录处理中并在任务完成后清理；字幕/转录文字和被选中的关键帧会发送给配置的 DeepSeek 模型生成总结。

## 总结 Skill

总结标准保存在 [skills/video-summary/SKILL.md](./skills/video-summary/SKILL.md)，由共享 Harness 加载；[skills/config.json](./skills/config.json) 配置各项能力启用的 Skill 名称：

```json
{
  "version": 1,
  "capabilities": {
    "video_summary": ["video-summary"]
  }
}
```

每个视频任务开始时读取一次 Skill，文字稿压缩、知识分段和画面分析使用同一份规则。修改 Skill 文件后，新任务自动读取新内容；规则与模型厂商无关。时间位置保留在内部供文字定位与取帧，段落标题、图片说明和图片替代文字不附加时间。视觉分析未确认知识价值的图片不进入成品。

新增能力时可在 `skills/<名称>/SKILL.md` 定义规则，并在 `capabilities` 中为该能力配置一个或多个 Skill，再由能力模块调用 `load_skill_instructions` 加载。空列表表示该能力不启用 Skill；配置错误或文件缺失会明确报错。

## 定时任务

添加每天一次或单次的任务：

```bash
python3 agent.py schedule add --daily 09:00 "https://www.bilibili.com/video/BV..."
python3 agent.py schedule add --once "2026-10-20 09:00" "https://www.youtube.com/watch?v=..."
python3 agent.py schedule list
python3 agent.py schedule list --runs
python3 agent.py schedule remove <任务ID>
```

任务按电脑本地时区运行。错过超过 60 秒的计划时间后会记录为 `missed`、显示本地通知且不补跑；每日任务的下一次计划仍保留。macOS 后台服务会在登录时启动并由 launchd 保持运行：

```bash
python3 agent.py service install
python3 agent.py service uninstall
```

安装后台服务前，建议使用 `config set-api-key` 保存 Key，避免 launchd 无法读取仅在终端会话中设置的环境变量。电脑必须保持开机、醒着且联网；后台服务无法克服休眠、断电、断网或 DeepSeek 服务中断。

如果使用双击启动的前端，后台服务也应使用相同的数据目录：`MINIMAL_AGENT_HOME="$PWD/.local" python3 agent.py service install`。安装时会记录该数据目录和媒体工具搜索路径。

## 本地文件

- `~/.minimal-agent/.env`：本机 API 配置
- `~/.minimal-agent/harness.sqlite3`：任务与运行记录
- `~/.minimal-agent/agent.log`：Harness 运行日志
- `summaries/`：Markdown 总结与关键帧图片
