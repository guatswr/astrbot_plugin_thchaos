# astrbot_plugin_thchaos

THChaos 的 AstrBot 插件：把游戏端的观众投票播报**到指定的 QQ 群**，并把群友回复的 `1`/`2`/`3` 转发给游戏端。

```
THChaos 游戏端 ──▶ thchaos_backend ──▶ 本插件 ──▶ 指定群（group_ids）
       ▲                                              │
       └──────────── vote.cast（1/2/3）◀──────────────┘
```

插件只做平台适配：**不计算票数、不开奖、不保存投票记录**。所有权威状态都来自后端转发的游戏端消息。

## 安装

AstrBot 插件按仓库安装到 `data/plugins/`：

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/guatswr/astrbot_plugin_thchaos.git astrbot_plugin_thchaos
```

或者直接把整个目录复制进去。依赖只有 `aiohttp`（AstrBot 自带，`requirements.txt` 里也写了一份）。放好后在 AstrBot 的插件管理里重载一次即可。

## 指定参与投票的群聊

**这是本插件的核心配置项**：只有 `group_ids` 里列出的群参与投票，其他群的消息一律忽略。

| 配置项 | 类型 | 说明 |
|---|---|---|
| `backend_url` | 字符串 | 后端 Bot 地址，例如 `ws://<服务器IP>:9961/ws/bot` |
| `token` | 字符串（密文） | 后端 `THCHAOS_BOT_TOKENS` 里的 **Bot Token**，不是游戏 Token |
| `room_id` | 字符串 | 必须等于 Bot Token 在后端映射到的房间，默认 `main` |
| **`group_ids`** | **列表** | **参与投票的 QQ 群号白名单，见下** |
| `group_umos` | 字典 | 一般不用填；插件会自动记住每个群真实的 UMO |
| `voter_hmac_secret` | 字符串（密文） | 生成不可逆 `voter_id` 的密钥，随手一串随机值 |
| `snapshot_interval_seconds` | 浮点 | 群内票况合并播报间隔，默认 2 秒（内部转发仍是实时的） |
| `announce_vote_ack` | 布尔 | 是否逐条回复"已计票/未计票"，大群建议关闭 |

### 怎么填

在 AstrBot 的插件配置界面找到 `group_ids`，逐个添加群号：

```json
["123456789", "987654321"]
```

- **填纯数字群号**（QQ 群号就是那个数字），不要填群名
- **不要填 `aiocqhttp:GroupMessage:123456` 这种 UMO**——那是 AstrBot 内部的会话标识，填进来永远不会匹配（插件启动时会检查并警告）
- 写成字符串 `"123456789"` 或整数 `123456789` 都可以，插件会统一处理
- **留空 = 不响应任何群**。插件照常连接后端，但群里发什么都没反应，启动日志会明确提示

### 不确定群号是多少

把群号加上去之前，先在目标群里随便发一条消息。插件收到**不在白名单里**的群消息时会在日志里打印一次：

```
THChaos 收到不在 group_ids 里的群消息（群号 123456789），已忽略；
若要让该群参与投票，把群号加进插件配置的 group_ids。
```

每个群只打印一次，不会刷屏。把这行里的群号抄进 `group_ids` 即可。

### 多个群一起投票的行为

- **播报广播**：投票开始/票况/结果/Chaos 执行都会同时发到所有白名单群
- **票数合并**：所有群的票汇总到同一轮里，群友看到的是总票况
- **同一人只有一票**：`voter_id` 由 QQ 号经 HMAC 生成，同一轮里同一个人在多个群里各投一次也只算第一票，后续会被判 `round.duplicate_vote`
- **改动配置后**：`group_ids` 在插件加载时读取，保存配置后重载插件才生效（AstrBot 保存插件配置时通常会重载；没生效就手动重载一次）

## 群友怎么投票

投票开放时群里会收到：

```
【观众投票 #3】
1. 速度降低
2. 敌弹加速
3. 封锁方向
回复 1/2/3 投票（剩余 10.0 秒）
```

群友在群里回复单独一个 `1`、`2` 或 `3` 即完成投票。只认**整条消息就是一个数字**的形式，`12`、`我投2`、`2 快` 都不会被当成投票——避免群聊里的日常对话被误计。

## 排障

**重载插件后就不再连后端了（0.2.3 之前的版本）。** 0.2.2 及更早把建连放在 `@filter.on_astrbot_loaded()` 里，而这个钩子**只在 AstrBot 进程启动时触发一次**（`core_lifecycle.start()` 里那一处调用）——在面板里重载插件、保存插件配置都只走 `plugin_manager.reload()`，不会再触发它。于是插件照常收群消息、照常处理，却从不连接后端，播报一条也发不出去，而且**连启动日志都不打**。0.2.3 改用 `initialize()`（与 `terminate()` 配对，每次加载都调用）。升级到 0.2.3 后**在面板重载插件即可**，不必重启 AstrBot。

**先确认插件到底连上没有。** 插件每次连上后端都会打一行：

```
THChaos 已连接后端 ws://<服务器IP>:9961/ws/bot（房间 main，游戏端在线）
```

- **没有这一行** → 插件没连上后端，看下面「插件连不上后端」
- **写着「游戏端离线」** → 连上了，但游戏端不在这个房间：`token`/`room_id` 与后端 `.env` 里那条映射对不上
- **有这一行、游戏端也在线，群里却没动静** → 看有没有 `没有平台匹配会话` 的告警

**插件连不上后端。** 日志里 `backend token 未配置` 说明 `token` 是空的，插件会直接不连接；`连接失败：...` 说明地址不通或握手被拒。地址要填 **`ws://<服务器IP>:9961/ws/bot`**——9961 是宿主机的发布端口，不是容器里的 8765。握手被拒通常是 `token` 与 `room_id` 和后端 `.env` 的 `THCHAOS_BOT_TOKENS` 对不上，日志里会有 `THChaos backend error: ...`。

**日志报「没有平台匹配会话 aiocqhttp:GroupMessage:...」。** 播报的目标是 `<平台ID>:GroupMessage:<群号>`。AstrBot 在找不到匹配平台时**不抛异常**，只是返回 `False` 把消息丢掉；插件主动检查了这个返回值，所以这个本来无声的故障会变成一条告警。如果你在面板里给 OneBot 平台起的 ID 不是 `aiocqhttp`，在 `group_umos` 里手工填一次该群真实的 UMO 即可：

```json
{"123456789": "<你的平台ID>:GroupMessage:123456789"}
```

（正常路径不用管：只要白名单群里有群友说过话，插件就会记住该群真实的 UMO。）

**群里发 1/2/3 没反应。** 按顺序查：插件是否连接成功（上面那行 `已连接后端`）；`group_ids` 是否包含该群；`token` 与 `room_id` 是否和后端 `.env` 里的一致；当前是否真的有开放的投票（没有投票时回复数字会被静默忽略，这是设计如此）。

**日志报 `voter_hmac_secret 未配置`。** 插件会拒绝接收任何投票，填一串随机值即可。

**重启插件后群里突然能重复投票了。** 更换 `voter_hmac_secret` 会让所有群友的 `voter_id` 变化，等同于换了一批投票人。别随意改。

## 测试

纯函数（投票解析、伪名、白名单归一化等）不需要 AstrBot 运行时：

```bash
python -m pytest tests -q
```

## 来源

本插件原本位于 [thchaos_backend](https://github.com/guatswr/thchaos_backend) 仓库的 `integrations/astrbot_plugin_thchaos/`，独立成仓库后单独演进。后端协议见 [docs/protocol-v1.md](https://github.com/guatswr/thchaos_backend/blob/main/docs/protocol-v1.md)。
