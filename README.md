# Minimal Agent

支持对话与独立功能页面并行操作的本地通用 Agent。支持知识交流、写作、联网查询与研究，以及调用视频总结、论文总结、内容管理和定时任务。轻量 Harness 共享模型适配、工具执行、持久化、日志与通知。

完整范围与边界见 [需求文档](./需求文档.md)。

## 当前框架：Agent 职责、终端式桌面与并行执行

模块边界、公开接口和扩展示例见 [架构框架](./架构框架.md)。主界面采用参考 Codex 的单侧栏对话布局：正文为主、底部紧凑输入、工具活动与来源折叠；每个 Subagent 有自己的职责、能力集合、API/模型/Skills 配置与任务页面。

当前注册视频学习、论文阅读、知识整理三个 Agent。一个 Agent 可以拥有多项能力；新增能力需要明确绑定已有 Agent 或注册新 Agent。共享执行池默认最多并行 4 项，即时任务、定时任务和归档知识整理使用同一上限；每项委派隔离历史与配置。可通过 `MINIMAL_AGENT_MAX_PARALLEL` 设置 1–16。ASR 使用独立的单实例资源限制。

主 Agent 通过 `list_subagents → delegate_subagents → read_subagent_results` 委派与读取成果。每批最多 8 项；有依赖的任务等待前置结果。长任务完成后分别回传成果，跨任务综合可在后续对话提出。

控制台支持 `/help`、`/agents`、`/agent paper_reading`、`/tasks`、`/notes`、`/knowledge`、`/schedule`、`/settings`。这些是工作台导航命令，不执行系统 Shell。

## 共享知识总结引擎

视频、论文与聊天整理现在复用 `summary_engine.py`：读取材料 → 分块提取知识与依据 → 层级整合 → 全文合并重复、统一术语、处理修正与分歧 → 按领域生成文档。短材料也经过一次全文整合；长材料逐块读取，超过预算明确报错，不静默截去尾部或无限调用模型。

来源由读取器建立：视频保留字幕位置以选择原图，论文保留可提取的 PDF 页码，对话保留消息编号与角色。引擎检查输出结构和引用编号；这不能证明知识本身正确，也不能保证模型没有遗漏细节。模型会做主题与覆盖检查，重要内容仍需结合原始材料核对。

三类任务默认组合 `skills/summary-core/SKILL.md` 与各自领域 Skills。已保存的自选 Skills 列表继续生效，可在 Agent 设置中加入或编辑 `summary-core`；任务开始时冻结规则。基础来源、预算和整合协议由引擎执行，不受输出风格影响。API 与模型继续按所属 Agent 的配置使用。

视频保留候选帧提取与实际视觉判断；论文仍只分析实际取得的文字，不声称识别 PDF 图像、复杂公式或表格；聊天整理仍按用户选择入库，一次归档保存一份含全部知识点的文档。普通对话继续由主 Agent 处理。此次只做静态语法检查，未运行测试或实际模型调用。

修正整合针对本次输入的材料；既有报告与个人知识库中的历史条目不自动重新生成或覆盖。

## 0.6：对话、独立 Agent 配置与论文登录

已取消“仅通过对话交互”的限制。侧栏恢复视频总结、论文总结、个人知识库、内容库、定时任务、运行记录和 Agent 设置。两种入口共享后台任务、配置和数据；对话调用与知识上下文继续保留。

- 打开 **Agent 设置**、点击对话页的 **配置 Agent**，或说“配置论文 Agent”，可为主 Agent、视频总结、论文总结、知识归档分别配置模型与 Skills。业务 Agent 可使用独立 API Key，也可逐项继承默认值。DeepSeek 地址固定为官方默认地址。
- 密钥只通过安全输入框保存，不发送到聊天。Skills 可以选择或逐个编辑；私有规则保存于数据目录 `skill-overrides/<角色>/`。任务开始时冻结配置，修改只影响后续执行。
- 说“查看知识库”“查看生成的文档”“安排定时任务”，相应控件会出现在当前对话中。后台任务的进度、失败原因和完成正文会返回发起对话。
- 个人知识随问题自动检索并附来源。短追问结合最近的问题继续检索；目前使用关键词匹配，不能保证检索出所有语义相关知识。
- 论文受登录限制时显示 **账号与论文材料** 卡片，在桌面客户端点击 **打开专用登录窗口**，自行完成个人或机构登录、验证码和二次认证。网站页面不会收到工作台 API 凭证或聊天上下文。
- arXiv、OpenReview 可在登录窗口点击 **读取页面提供的 PDF**。IEEE 和未知站点使用人工下载：打开 PDF 后点击 **导入当前 PDF**，或返回对话点击 **上传 PDF 继续原任务**。导入后沿用同一任务编号。
- 原文 PDF 与总结关联保存在报告的 `_assets` 目录。清除账号会话可通过对话请求，或在登录卡片/专用窗口点击 **退出并清除会话**。

账号权限及网站策略决定可取得的全文；登录不保证全文可用。macOS 14+ 保存独立网站存储，macOS 13 保存 Cookie 到本机钥匙串，部分依赖 localStorage 的登录需要重新认证。普通浏览器模式支持人工上传，专用登录窗口需要桌面客户端。

私有配置、上传文件、聊天、知识库、浏览器会话与运行成果均不随源码上传到 GitHub。本轮进行了语法检查与桌面构建，未执行实际模型调用及真实账号登录验收。

## 桌面客户端与对话入口

### 会话归档与个人知识库

会话工具栏提供“归档”。可以选择“仅归档”，或者“整理知识并入库”。两种方式都保留完整聊天记录，归档列表支持查看和恢复；归档会话暂停输入，恢复后继续交流。

知识提取是独立后台任务，失败不撤销归档，可以重试。仅归档不调用模型、不写入个人知识库。普通聊天保存在会话历史，只有主动选择知识提取才从该对话写入知识。

也可以在 **知识整理 Agent** 页面粘贴 ChatGPT 聊天分享链接，点击“整理并入库”；或在主对话中说“把这个分享对话归纳到知识库：https://chatgpt.com/share/…”。支持 `chatgpt.com/share/…` 和旧版 `chat.openai.com/share/…` 的公开聊天。任务复用知识整理 Agent 的 API、模型、Skills 与共享并行池。

读取到的消息按角色保留，整理结果自动保存为 `summaries/knowledge_archives/` 中的一份文档，同时建立可检索的知识索引。来源详情可查看消息摘录并打开原分享链接；不会将外部分享当成本机聊天。分享内容仅作为材料，里面的指令不会执行，聊天回答也不等同于已经核实的事实。

同一链接和归档标题重复提交时复用已有任务及成功快照，失败或中断可重试读取。当前分享更新后需要使用新的归档标题重新整理。

分享读取针对无需登录的公开页面。失效链接、登录要求、访问验证、图片或附件正文不可读时会明确报错或说明范围，不用页面标题代替聊天内容。仅归纳分享中实际可读的文字；页面格式变化可能需要更新读取适配器。[ChatGPT 官方分享说明](https://help.openai.com/en/articles/7925741-sharing-conversations-and-scheduled-tasks-in-chatgpt)列出了个人分享快照及工作空间访问限制。

分享读取支持复用本机已有的无凭证 HTTP 系统代理；此路径限定在 ChatGPT HTTPS 分享域名，保持 TLS 证书校验。2026-10-15 实际测试修复了网页读取绕过 macOS 系统代理造成的超时，随后完成 8 条分享消息的读取、23 条知识入库及单份文档保存。图片、音频和附件未下载识别。分享解析、传输、入库及相关诊断共 69 项回归测试通过。

“个人知识库”提供关键词搜索、知识正文、主题标签、来源消息摘录与对话引用、相关条目、编辑以及可恢复的删除。完全相同的知识关联新增来源；相近主题且内容不同的知识保留为独立条目，不自动覆盖观点。编辑使用版本检查并保留历史与原始来源。

个人知识单独保存在数据目录 `knowledge.sqlite3`，聊天与提取任务在 `harness.sqlite3`。删除聊天或报告不会自动删除已经提取的知识；来源摘录仍然保留。后续对话可检索相关知识作为引用背景，模型也能通过工具查询、编辑、删除与恢复知识。首版检索使用关键词与中文词片段，尚未使用向量语义检索。

提取规则位于 `skills/knowledge-extraction/SKILL.md`。模型根据已提供的消息编号与网址整理知识，系统检查来源编号，并移除未提供的引用链接。未完成或失败的回复不参与知识提取。较长对话分块处理；超过30万字会明确提示拆分。知识来自对话整理，事实准确性仍取决于原始资料与模型判断。

### 共享与业务专有 Harness

- `harness.py` 和 `harness_runtime.py`：共享模型适配、取消、参数校验入口、执行轨迹、进度与成果保存；队列、数据库与定时服务继续由现有 WebRuntime/Scheduler 管理。
- `business_harnesses.py`：`VideoHarness` 管理视频输入、字幕/转录/原图流程与图片完整性；`PaperHarness` 管理论文材料、概述与知识提取及阅读范围检查。每项业务有专属 Skills、工具清单、轮数和时间预算。
- `subagents.py`：业务 Agent 使用独立的消息历史，在专有 Harness 内执行 `produce_result → inspect_result → finish_task` 循环。完成前由程序检查成果文件、正文一致性，以及业务所需的图片或来源。专有处理步骤内部继续复用已有工作流；检查不等于自动证明内容正确。
- 主 Agent 直接完成普通对话，只有视频总结、论文总结等业务请求才进入相应 Subagent。知识归档使用独立整理流程，不强制进入业务 Subagent。

新增业务时可以注册新的 `BusinessHarness` 子类和 `Capability`；基础模型、存储与调度保持共享。当前隔离范围为上下文、工具清单及执行预算，仍在同一进程运行；尚未实现按费用上限控制运行。

### 日常对话与联网研究

- 会话列表支持新建、切换、重命名、删除；删除聊天记录不会删除已生成的报告或任务。
- 模型回复逐步显示，支持停止回复、重试上一条、复制、Markdown、代码、表格和 Mermaid 图表。
- “允许联网查询”控制本次请求能否使用网页工具。普通知识问题直接回答；最新信息或资料核对可搜索并读取网页，在回答中附来源。
- Web research 是主 Agent 的内置能力：在对话中规划查询、搜索、读取正文、补充检索并比较整理。正式研究报告通过保存工具写入普通笔记目录；它没有独立页面、业务 Subagent 或定时功能入口。旧独立研究定时计划会停用，历史报告继续可读。
- 搜索使用 Bing RSS，失败时尝试 DuckDuckGo HTML，无需额外 API Key。搜索服务与目标网站可能限制访问，失败会明确返回，不冒充获取成功。普通网页阅读支持公开的静态 HTML/文本，论文功能另行支持 PDF 文字提取；普通联网工具不使用登录会话；论文获取可通过下述专用桌面浏览器完成认证和材料导入。
- “把刚才的回答保存为笔记”通过保存工具写入内容库。普通聊天只保存会话记录，正式报告自动保存为文件。
- 即时任务及定时运行均可取消；取消某次定时运行不影响以后安排。取消是协作式的：模型和网页读取会中断连接，视频下载停止子进程，本地 ASR 在语音分段之间检查停止状态。
- 服务重启后，未完成的回复与任务标为中断；可以查看保留的内容并主动重试，不自动重放有副作用的操作。

可以尝试：“解释 MVVM，并与 MVC 比较”、“搜索这个问题的最新资料并给出来源”、“研究 RAG 的常见评估方法并生成报告”、“把这段解释保存成笔记”。

`conversation.py` 提供 Agent 循环；`tool_registry.py` 定义厂商无关的工具契约；`web_research.py` 提供搜索、网页读取与研究能力；`runtime_control.py` 提供共享取消控制。DeepSeek SSE 接口保留流式工具参数及内部 `reasoning_content`，按 [官方工具调用说明](https://api-docs.deepseek.com/guides/thinking_mode/)回传，页面只显示回答和执行进度。

macOS 13+ 可双击 `启动桌面客户端.command`，或打开 `dist/Agent 工作台.app`。首次构建运行 `.venv/bin/python build_desktop.py`，需要本机 Xcode Command Line Tools。应用使用原生 AppKit 窗口和 WKWebView，复用现有页面与 Python Harness，不需要打开 Chrome。

本版本的应用连接当前项目目录和 `.venv`，用于本机运行；移动项目后请重新构建。关闭主窗口会隐藏窗口，应用及其定时任务继续运行，可从 Dock 或菜单栏恢复。菜单“退出 Agent 工作台”停止由客户端启动的服务；复用的其他后台服务由其原启动方式管理。菜单“打开内容保存目录”直接打开本地内容库。

默认首页是对话。可以说“总结这个视频 https://…”、“明天九点执行视频总结”、“列出生成的文本”、“打开上一份内容”、“删除指定文本”或“恢复刚才删掉的文本”。Agent 从注册功能中选择工具，任务在后台处理，进度和结果通过对话卡片查看。对话保存在本机，创建新对话会保留旧记录。

生成文本和原图自动保存到项目 `summaries/`，不需要每次下载。内容库支持查询、打开和移入回收站；删除同步隐藏对应任务的正文与输出入口，文件暂存 `.local/trash/notes/` 并可恢复。下载用于另存或分享。

## 功能扩展与通用调度

`capabilities.py` 注册功能 ID、名称、输入 schema、校验器及执行器。对话工具、定时任务的功能列表和参数表单从同一注册表生成；调度器持久化 `capability + parameters + cadence + due_at`，不依赖视频功能。业务功能目前包含 `video_summary` 和 `paper_summary`；网页搜索、阅读与研究属于主 Agent 内置工具。

旧视频定时任务在启动时迁移为视频能力参数并保留执行历史。新增功能需注册 `Capability` 并明确绑定 Subagent 的 Harness，再通过对话委派和通用调度调用；各项功能继续共享 Harness、日志、通知和内容保存目录。

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

打开 `http://127.0.0.1:8765`。页面默认打开对话，侧栏提供独立功能和管理入口；管理操作也可在对话内展示。没有 API Key 时也能打开页面，可点击“配置 Agent”输入并保存 Key；页面不会回传已保存的 Key。模型设置同时支持切换同一 DeepSeek API 下的模型。

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

模型连接优先直连官方 API。TCP/TLS 建连失败时，在共享超时预算内最多尝试第二条连接（已有无凭证 HTTP 系统代理，或再建一次直连）；TLS 证书校验失败直接报错。请求发出后发生的连接重置、响应读取失败和流式中断会保留失败状态，由用户主动重试任务，避免自动重复提交生成请求。`Connection reset by peer` 表示连接被中断，日志会区分建连与请求后的阶段；读取分享网页的代理策略独立保留。

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

视频总结会保存到当前目录的 `summaries/video_summary/`，按内容与知识结构组织中文笔记，成品不显示视频时间戳，不描述讲述者的衣着和动作。开头可有概述与框架图，正文按需要加入小图标、Mermaid 概念图/流程图，以及经视觉分析确认有知识价值的原视频画面。可用 `--no-frames` 只生成文字和图表。

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
- `summaries/`：按功能分目录的 Markdown 文档与关键帧图片


## 论文总结与分类内容库

对话中的论文材料卡片支持三种输入：粘贴论文原文、公开论文链接、上传 PDF。也可以在主对话中附加 PDF 并说“总结这篇论文，提取方法和关键知识”，或直接提供论文链接。定时任务的功能列表同样包含论文总结，支持链接、原文和已上传附件。

结果包含简短概述、研究问题、贡献、核心方法、实验依据、局限与可复用知识点。需要解释关系或步骤时可生成概念图/流程图。规则位于 [论文总结 Skill](skills/paper-summary/SKILL.md)，由 `skills/config.json` 关联能力。

公开链接无需平台账号。arXiv 摘要链接会尝试获取 PDF；页面提供 `citation_pdf_url` 时尝试读取全文。仅能获取页面文字或摘要时明确说明，不当作全文分析。PDF 由 [pypdf](https://pypdf.readthedocs.io/en/stable/) 提取文字，扫描版暂不支持 OCR；图像、公式和复杂表格不做视觉识别。PDF 上限 20 MB、200 页，原文上限 30 万字。长材料逐片处理，不静默省略尾部。

首次更新请运行 `配置运行环境.command` 安装新增的 `pypdf` 依赖，之后运行 `启动桌面客户端.command`。模型凭证继续使用原有设置。

输出根目录默认是项目的 `summaries/`（可以通过 `MINIMAL_AGENT_OUTPUT` 调整）：

```text
summaries/
  knowledge_archives/  # 每次归档一份完整知识文档
  video_summary/       # 视频笔记与对应 _assets 图片
  paper_summary/       # 论文笔记
  web_research/        # 历史联网研究报告，新报告保存为普通笔记
  notes/               # 保存的普通回答、文稿
```

选择“整理知识并入库”时，全部知识点保存在同一归档文档中，同时写入个人数据库的独立索引，方便搜索、编辑和继续对话。知识页面先显示归档文档，再提供可展开的知识索引。删除文档仅将文件移入内容库回收站，索引仍保留；编辑或删除索引不改写归档快照。仅归档对话时不提取知识、不调用模型。

既有知识归档会补存整份文档，无需重新调用模型。已有根目录笔记会按功能迁移，图片与导出副本一起移动，旧链接和内容 ID 保留兼容。原始 PDF 保存在 `.local/uploads/papers/`，以便重试和定时执行；移除聊天中的附件选择不会删除已上传的本机原件。`.local/` 和 `summaries/` 均不推送到 GitHub。

扩展一个新功能时，注册 `Capability` 和业务 Harness 即可复用对话工具、后台队列、定时调度、进度、取消、日志和保存目录。论文的读取与材料范围检查属于 `PaperHarness`，共用的模型、轨迹与文件操作属于 `SharedHarness`。

论文网站可能返回登录、访问验证或动态加载页面。例如 IEEE Xplore 链接 `document/11029963` 在游客读取测试中返回 HTTP 202 和需要 JavaScript 的人机验证页面，未取得正文。现在此类任务进入等待材料状态，提示打开专用登录窗口下载 PDF，或导入可读取的 PDF 后继续原任务。输入无法读取时不反复调用模型，不将验证页面总结为论文；失败原因保留在运行记录与日志中。

离线回归检查可运行 `.venv/bin/python -m unittest discover -s tests -v`，不调用模型 API。

## 已安装的外部论文 Skill

论文 Agent 默认加载 `academic-paper-review` 与原有 `paper-summary` 规则。前者来自 [ByteDance DeerFlow](https://github.com/bytedance/deer-flow/tree/76102fab0b3a7f3d84b0479d72cf3e13dc21242f/skills/public/academic-paper-review)，使用 MIT 许可；原文件、许可与来源记录保存在 `skills/academic-paper-review/`，固定提交避免上游变化影响当前版本。

通过该 Skill 加强贡献与证据对应、方法合理性、实验设计、可复现性和局限分析。项目适配规则保留中文知识总结与实际阅读范围，不强制评分、录用建议或凑够固定数量的优缺点；它不增加论文下载权限、OCR 或网页工具。仍由现有论文 Harness 读取材料、保存结果并检查完成状态。

可在 Agent 设置的论文总结对象中启用、停用或编辑 Skill。新增任务使用当前配置，运行中的任务沿用开始时的快照。本次仅检查安装完整性与语法，未执行模型调用验收。


## 主控制台布局（2026-10-15）

侧栏集中放 Agent 入口和会话列表；Subagents、资料与任务可折叠。对话正文占据主区，输入框保持在底部。会话操作收进右上角菜单，工具活动与资料来源按需展开。正文使用常规系统字体，代码和执行状态保留等宽字体。

Enter 发送，Shift + Enter 换行；中文输入法候选确认不会触发发送。后台任务仍可在控制台展开查看，各 Subagent 的独立页面和配置继续使用同一注册与运行接口。


## Typora 底层适配（2026-10-15）

已增加可扩展的本地应用注册和调用服务，首版接入 macOS Typora：查询安装状态、打开内容库文档与分类目录。使用文档编号，不接受任意路径或命令；图片继续引用原目录中的资源。

接口与扩展方式见 [本地应用适配](./本地应用适配.md)。本轮只接入 Python 和本地 HTTP API，对话入口及页面交互留待下一轮调整。后续 WPS 等应用可注册独立适配器；不同文档格式需同时扩展内容库解析。


## B 站游客读取修复与验证（2026-10-15）

视频处理现在复用一次成功取得的公开媒体信息，字幕、音频和关键帧通过临时私有缓存下载，避免每一步重新请求游客播放接口。元数据／字幕读取允许暂时没有媒体流；弹幕不作为字幕。实际媒体不存在时，转录仍会明确失败，关键帧可降级；缓存下载失败不会自动重新提取原 URL。

本轮对 `BV1it411g7CX` 默认 P1 完成实际验证：本地转录、5 个知识分段、8 张相关原图、自动保存；检查 Markdown 与数据库正文一致、8 个图片引用有效，下载压缩包包含正文和全部图片。`tests.test_video_metadata` 与 `tests.test_video_formats` 合计 12 个回归测试通过。此记录证明本次处理成功，不保证平台游客访问始终可用。
