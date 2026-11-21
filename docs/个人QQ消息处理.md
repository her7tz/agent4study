# 个人 QQ 消息工作区

## 指定测试会话与平台未读

在「连接本机 QQ」下勾选「指定一个会话做只读联调」，填写桥接已登录的 QQ 号、测试会话类型和编号。群聊模式只向桥接查询指定群号的资料。好友模式查询好友列表后仅保留指定好友。界面和服务端都禁止该连接发送消息。连接和读取使用本机 OneBot 11 HTTP，无需把令牌发到聊天。

选中群聊可按日期读取，也可点击「读取 QQ 平台未读」。平台未读依赖桥接的 `get_workbench_group_unread` 扩展。桥接明确返回接口未提供时，页面会提示可继续按日期读取。其他失败会提示未能验证未读范围。每次有效的新查询都会清除上一次消息、覆盖范围和待发送确认。成功后显示平台未读条数、首末消息序号和实际可整理的文字或直接 @ 条数。模型摘要仍保留原消息引用。

### 桥接扩展接口

使用与其他动作相同的本机 HTTP 端口和 Bearer 令牌，向 `POST /get_workbench_group_unread` 发送 `self_id` 和 `group_id`（十进制字符串）、`count`（每页最多 100）。下一页带上上页的 `snapshot_id` 和 `next_cursor`，后者作为请求的 `cursor`。服务需按同一登录账号与群聊提供稳定的只读快照，不触发已读状态变更。

成功沿用 OneBot 的 `status: "ok"`、`retcode: 0` 和 `data`。例如双条未读快照可返回以下数据：

```json
{
  "version": 1,
  "source": "qq_platform",
  "self_id": "123",
  "group_id": "9",
  "snapshot_id": "example",
  "unread_count": 2,
  "first_unread_seq": "97",
  "last_unread_seq": "102",
  "messages": [
    {"self_id": "123", "group_id": "9", "message_type": "group", "user_id": 55,
     "time": 100, "message_id": "example-a", "message_seq": "97", "message": "确认实验数据"},
    {"self_id": "123", "group_id": "9", "message_type": "group", "user_id": 66,
     "time": 101, "message_id": "example-b", "message_seq": "102", "message": "[CQ:at,qq=123]"}
  ],
  "next_cursor": null
}
```

平台来源、账号、群号、快照 ID、总条数和边界须在各页一致。每条消息需属于所选账号与群聊，ID 唯一，序号严格递增。序号允许有间隔。服务需要提供该平台未读集合的全部记录，包括最后不会提交给模型的图片等。最后一页用空游标，零未读必须返回空数组、空游标及两个空边界。超过 10 页或 1000 条、快照过期、串会话、缺页或条件变化时，整次未读读取失败，无部分结果可进入摘要。

这个扩展是工作台为本机桥接预留的接口，已核对的 NapCat、LLBot 和 ICQQ 发行版没有现成实现。现有测试使用合成快照核验消费端。真实联调还需要已登录的桥接、用户指定的测试群，以及能从 QQ 客户端可靠取得上述边界和全集的扩展。

## 常规连接与读取

入口：桌面或本地网页的 **Agent 设置 → 打开 QQ 消息工作区**。使用个人账号，工作台没有 QQ 登录或扫码功能。需要用户在本机运行已登录的 NapCat 桥接服务，开启带访问令牌的 OneBot HTTP 接口。没有桥接服务时可继续使用其他 Agent 功能。参考：[NapCat 接口兼容说明](https://napneko.github.io/develop/api)、[OneBot 公共接口](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)。

1. 填写本机端口和桥接服务的访问令牌。可选填要连接的 QQ 号，工作台先核对账号，相符后才请求好友和群聊列表。留空时，连接后可以查看实际使用的 QQ 号。工作台只联系 127.0.0.1，不收取 QQ 密码、不跟随重定向、不自动重试。令牌只在运行期间使用，不写入配置文件。
2. 选择一个好友或群聊，指定起止时间，点击读取。连接只取身份和会话列表。读取时每页最多 100 条，按当前会话最早消息的编号向前翻页，最多 10 页。桥接为同一秒的消息提供原始序号时，按该序号确定翻页起点和展示顺序。短页仍会继续向前查询。若桥接端停止返回数据、重复返回同一页或达到预算，结果会说明停止原因。仅处理文字与直接 @，向 NapCat 请求跳过媒体链接解析与合并转发展开。
   桥接可返回消息段数组，也可返回 CQ 字符串。工作台识别两种格式的文字与直接 @。若后续历史页格式无效或超出每页数量上限，页面仍显示此前已取得的消息，并在摘要中标明只读到部分历史。 [OneBot 字符串格式](https://github.com/botuniverse/onebot-11/blob/master/message/string.md)
   分页读取时仍可点击“断开”。工作台立即清除当前连接和材料，丢弃已在途的响应，并停止后续翻页。重新连接后只展示新连接的消息。
3. 读取结束后，可查看实际取得的首末消息时间、消息编号、发送者与正文。点击总结会把当前读取的消息交给主 Agent 配置的模型。结果保留会话、请求时间、实际取得的首末消息和停止原因，并将摘要、待办及直接 @ 对应到可展开的原消息。点击拟回复只提交所选一条。等待模型时仍可断开。模型返回后工作台复核登录账号和读取范围，QQ 客户端切换了登录账号时，页面清除旧材料并要求重新连接。已经提交给模型服务的请求可能继续完成。外部消息不进入主会话、知识库或工具循环。
4. 编辑回复，预览发送账号、好友/群号和全文，确认后提交普通文字。取消只作用于对应的预览。确认票据五分钟有效，只能使用一次；切换会话、重新读取或重新连接后，旧票据失效。
5. 收到完整且带有效消息编号的成功回执时，工作台显示桥接已确认提交。回执中断或格式无效时显示结果未知，请先在 QQ 客户端核对。点击断开清除工作台内的连接与消息；个人 QQ 保持登录。工作台当前只有一份连接，多个页面重新连接会使旧页面的会话失效。

工作台重启或连接被另一页面替换后，旧页面会在收到失效提示时清除原会话材料。点击连接，重新选择会话并读取。若此前有发送结果未知，先在 QQ 中核对。 [浏览器联调](acceptance/2026-11-09-qq-browser-reconnect.json)

连接后显示桥接实际返回的 QQ 登录账号和会话列表。好友和群聊列表独立显示取得数量或无法读取；一类列表失败时，仍可使用已取得的另一类会话。页面说明可读取文字与直接提及、可确认提交文字回复。历史读取状态会显示尚未验证、查询结果或失败；更改会话、日期或断开时清除上一次状态。

好友历史会排除明确标记为群临时私聊、其他私聊类型或收件人不属于当前会话的消息。部分桥接返回的私聊历史中，本账号发出的普通好友记录没有收件人。这类记录可在同一会话请求中辅助向前翻页，只有归属确认的消息才进入正文或摘要。页面注明因缺收件人而排除的数量。 [OneBot 私聊类型](https://github.com/botuniverse/onebot-11/blob/master/event/message.md#私聊消息) · [NapCat 私聊历史实现](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/go-cqhttp/GetFriendMsgHistory.ts) · [消息转换实现](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L1122)

合成材料已覆盖本机 HTTP 连接、筛选、草稿、单次确认、失败与断开。用户安装的 NapCat 已完成指定好友和群聊历史验收。测试的好友回复由发送方按编号核对正文，由用户在收件账号确认只收到一条。QQ 平台未读仍待验证。 [好友历史与发送回执](实施与验收记录.md#186-qq-好友历史与实号发送回执2026-11-20) · [收件端验收](实施与验收记录.md#191-qq-收件端确认收到一条测试消息2026-11-20)

2026-11-08 核对的 NapCat OneBot 版本中，最近会话提供最后一条消息，群历史提供按游标读取的消息列表。这两个接口均未提供用于确认完整未读集合的边界。FR-18 的平台未读验收仍缺桥接能力和实号验证。[最近会话源码](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/user/GetRecentContact.ts)、[群历史源码](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts)。

2026-11-10 复查项目文档和当时的源码，最近会话响应与群历史仍未给出用于划定未读集合的字段。工作台据此继续按选定时间范围读取。 [NapCat 接口文档](https://napneko.github.io/develop/api/doc) · [群历史实现](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts)

2026-11-15 在 NapCat 内部消息服务看到按会话读取未读计数的方法声明。公开 OneBot 最近会话返回最后一条消息，历史接口返回按游标读取的消息列表。当前核对未找到公开的未读消息边界，按时间读取也无法确认平台未读范围。 [内部消息服务](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-core/services/NodeIKernelMsgService.ts) · [最近会话返回字段](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/user/GetRecentContact.ts) · [公开 OneBot 接口](https://napneko.github.io/onebot/api)

2026-11-17 复查发现内部另有 `getFirstUnreadMsgSeq` 和 `getFirstUnreadCommonMsg` 的声明，返回数据和普通群聊的适用性仍未验证。公开 OneBot 接口中尚未找到可取回同一登录账号、指定普通群聊首条未读边界的已核实接口。 [内部消息服务声明](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-core/services/NodeIKernelMsgService.ts) · [公开 OneBot 接口](https://napneko.github.io/onebot/api)

2026-11-18 核对了 NapCatQQ Desktop 的「只看未读会话」。这是桌面端的会话筛选功能。项目把普通未读和托盘提醒状态分开，公开接口文档仍未提供可读取普通群聊首条未读边界的操作。工作台继续以选定会话和时间范围呈现实际取得的历史。 [NapCatQQ Desktop 聊天说明](https://napneko.github.io/NapCatQQ-Desktop/docs/chat/) · [桌面端状态说明](https://github.com/NapNeko/NapCatQQ-Desktop/blob/main/docs/context/codemap.md) · [公开 OneBot 接口](https://napneko.github.io/onebot/api)

2026-11-19 直接读取 GitHub 上游，NapCatQQ 的默认分支 README 已发布结束说明，Mac 安装器仓库查询返回 404。本机检查仅发现已安装的 QQ，未在默认位置找到 Mac 安装器或 NapCat 配置目录。历史固定提交仍可读取，经核对最近会话和群历史的返回字段仍缺未读边界。实号验收需要可用的桥接实例及用户指定测试会话。 [NapCatQQ 上游说明](https://github.com/NapNeko/NapCatQQ/blob/main/README.md)

2026-11-19 核查 LLBot 作为实号验收候选。项目提供 Apple 芯片 Mac 的命令行版本及 OneBot 11 HTTP 服务。文档中的群历史通过消息序号分页，返回历史消息；读取未读边界的接口未找到。也核对了 Milky，其历史接口返回历史消息及下一页序号。LLBot 的 Mac 版本、账号登录、分页参数及回执都需要实机验证。当前工作台仍使用原 NapCat 接入范围。 [LLBot 发布页](https://github.com/LLOneBot/LuckyLillia.CLI/releases) · [LLBot HTTP 配置](https://luckylillia.com/guide/config) · [LLBot 群历史响应](https://api.luckylillia.com/api-156808591) · [Milky 消息接口](https://milky.ntqqrev.org/api/message)

另查了适用于 macOS 的 ICQQ OneBot 和 NapLink。ICQQ 的历史查询使用消息序号和独立的字符串消息 ID，公开的群已读接口会把会话标记已读。NapLink 是 NapCat 的 SDK，公开消息接口也只有历史查询和标记已读。两套文档均未给出可由工作台查询当前账号、指定普通群聊首条未读消息的接口。 [ICQQ 消息接口](https://icqqjs.zhin.dev/rust/api/message) · [NapLink OneBot 接口](https://naplink.github.io/api/onebot.html)

核对 dsh-qq 对未读数的实现后发现，它读取 NapCat 最近会话响应中的可选计数字段，字段缺失时显示为零。已核对的 NapCat 版本的这一接口没有返回该计数，dsh-qq 也没有取得首条未读的位置。工作台仍将未读范围标为待验证。 [dsh-qq 历史工具](https://github.com/ReWebaiyu/dsh-qq/blob/8d98eb814efc1f5614df4a6301ec01e09ff477a1/lib/tools.js#L275-L293) · [NapCat 最近会话实现](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/user/GetRecentContact.ts#L57-L79)

### Apple 芯片 Mac 上的 LLBot 试接入

**2026-11-19 实机状态**：CLI 已完成组件安装，启动时要求 LLBot 的 Auth Token。授权页显示「新用户注册已关闭」，并说明已有账号仍可使用原登录方式。没有既有 LLBot 授权账号时，先暂停以下步骤。启动凭证由 LLBot 授权站签发，OneBot HTTP 访问令牌在桥接启动后另外设置。 [LLBot 授权页](https://auth.luckylillia.com/login)

已核对 v2.1.4 的下载文件是 arm64 Mach-O，SHA-256 与 GitHub 发布清单一致。文件使用临时签名，没有 Apple 开发者团队标识。校验期间未运行该程序。

1. 从发布页下载 `LuckyLillia-macos-arm64`。进入下载文件所在目录，执行 `chmod u+x ./LuckyLillia-macos-arm64` 和 `./LuckyLillia-macos-arm64 --headless`。在终端显示二维码后，用单独的 QQ 测试账号扫码。 [v2.1.4 发布页](https://github.com/LuckyLilliaDev/LuckyLillia.CLI/releases/tag/v2.1.4) · [启动参数](https://github.com/LuckyLilliaDev/LuckyLillia.CLI/blob/main/README.md)
2. 打开 [LLBot 本地管理界面](http://127.0.0.1:3080)，启用 **OneBot 11 → HTTP**。监听地址填写 `127.0.0.1`，保留默认端口 `3000` 或选择一个空闲端口，设置非空访问令牌。 [LLBot HTTP 配置](https://luckylillia.com/guide/config) · [默认配置](https://github.com/LuckyLilliaDev/LuckyLilliaBot/blob/main/src/main/config/default_config.json)
3. 在 **Agent 设置 → 打开 QQ 消息工作区** 填同一个 OneBot HTTP 端口及令牌，在「要连接的 QQ 号」填测试账号。连接后选择一个测试会话和日期，点击读取。首次只验证历史读取与消息归属。真实账号兼容性和 QQ 平台未读边界仍待验收。

### Apple 芯片 Mac 上的 NapCat Docker 实例

已在本机通过 Colima 启动 NapCat v4.18.42 的 Linux ARM64 镜像。原 Docker Hub 仓库已无法拉取，从社区归档恢复的镜像清单及全部镜像层均与 Docker Hub 记录的原版摘要匹配。WebUI 和 OneBot HTTP 都只绑定到本机回环地址。 [ARM64 归档](https://github.com/XingYunStar/NapCatQQ/releases/tag/v4.18.42) · [Docker Hub 镜像摘要](https://hub.docker.com/layers/mlikiowa/napcat-docker/latest/images/sha256-149fae81e2ccec10e5d1be2f9f86a1a2d6e1d5c70691f892dce00b7328f2ca72)

1. 打开 [NapCat WebUI](http://127.0.0.1:6099/webui/)。本机 Compose 项目的 `.local/napcat/.env` 保存 `WEBUI_TOKEN`。用手机 QQ 扫码并确认。
2. 在工作台选择 NapCat，填写 OneBot 端口 `5700`。OneBot 访问令牌来自 `.local/napcat/config/onebot11.json` 中 `network.httpServers[0].token`，与 WebUI 令牌独立。
3. 勾选「指定一个会话做只读联调」，指定测试 QQ 号及一个测试群或好友，再核对实际读到的消息、顺序和来源。平台未读另需符合上文契约的桥接扩展。

实机已通过 OneBot `get_login_info` 核对扫码账号，带无效访问令牌的请求被拒绝。首次指定的好友不在桥接好友列表中，工作台拒绝建立只读会话。随后使用用户指定的可见群聊，实测最近 24 小时筛出六条可整理消息，并核对了会话隔离、消息去重和时间范围。此版本的 OneBot 明确拒绝工作台未读扩展动作，实际工作台也返回失败并清空了上一次读取结果。 [实机验收](实施与验收记录.md#181-napcat-实号群聊历史只读验收2026-11-20)

重启后用户已再次扫码，原时间窗的六条历史仍可读取。NapCat 4.18.42 在调用前拒绝了未列入白名单的临时探测插件。已撤回该插件的容器文件和启用项，真实群未读边界仍需由支持该能力的桥接提供。 [实机装载检查](实施与验收记录.md#183-napcat-拒绝加载临时的未读探测插件2026-11-20)

### 已有 ICQQ OneBot 实例的接入

如果已有能登录测试 QQ 的 ICQQ OneBot 实例，在工作区选 **ICQQ OneBot（试用）**，填入监听在 `127.0.0.1` 的 HTTP 端口和访问令牌。默认示例端口是 `5700`。此模式用真实消息序号为同一好友或群翻页，每页最多 20 条、每次最多 10 页。原消息及发送回执保留桥接返回的字符串 ID。此适配按 ICQQ 公开接口验证，实号登录需要另备兼容的签名服务。 [ICQQ 配置](https://icqqjs.zhin.dev/rust/guide/config) · [历史与发送接口](https://icqqjs.zhin.dev/rust/api/message)

ICQQ 的本机 HTTP 服务可以在 QQ 未登录时启动。工作台在查询会话列表前检查 `get_status.online`，并在公布列表前复核。分页、模型处理和发送确认也会检查在线状态。确认掉线时，工作台清除该连接的会话列表、消息与待发送确认，并提示重新登录。 [ICQQ 在线状态接口](https://icqqjs.zhin.dev/rust/api/meta#获取运行状态)

已在这台 Apple 芯片 Mac 上用 v0.6.15 实际验证离线启动和工作台连接提示。发行包内附隔离所需的 `config.example.yaml`。实号仍需匹配的签名服务与测试 QQ。 [发行包](https://github.com/icqqjs/icqq-onebot/releases/tag/v0.6.15)

连接后，工作台根据 ICQQ 公布的动作列表分别显示好友和群聊历史接口的支持状态。若某一类明确缺少历史接口，该类读取会在工作台停止。动作列表不可用时显示「待验证」，仍可手动查询指定会话。能否取得所选时间内的消息，由实际读取结果说明。 [动作列表接口](https://icqqjs.zhin.dev/rust/api/meta#获取支持的动作列表)
