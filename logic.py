"""AstrBot 插件的纯函数，脱离 AstrBot 运行时即可测试。"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Iterable
from urllib.parse import urlsplit


def parse_vote_choice(text: str) -> int | None:
    """只接受完整、去空白后的单字符 1/2/3。"""

    value = text.strip()
    return int(value) if re.fullmatch(r"[123]", value) else None


def pseudonymous_voter_id(secret: str, sender_id: str) -> str:
    """生成符合游戏端 ASCII/63 字节约束的稳定伪名。"""

    digest = hmac.new(secret.encode("utf-8"), f"qq:{sender_id}".encode("utf-8"), hashlib.sha256).hexdigest()
    # 游戏端 voter 缓冲最多接受 63 个 ASCII 字节；1 个前缀字符 + 62 个
    # 十六进制字符刚好保留 248 bit 的碰撞裕量。
    return f"q{digest[:62]}"


# AstrBot 的会话标识是 <平台ID>:<消息类型>:<会话ID>。平台 ID 是用户在面板里给
# 适配器起的名字，只有没改过时才叫 aiocqhttp——猜错不会报错，只会静默丢消息。
DEFAULT_UMO_PLATFORM = "aiocqhttp"

# AstrBot 自带的 WebUI 平台：它的"群"是网页里的会话，不可能是 QQ 群。
# 名字取自 AstrBot 的适配器类型（PlatformMetadata.name），不是实例 ID。
# AstrBot 核心自己也用 `meta().name != "webchat"` 来判断"这是不是一个真实群聊平台"。
NON_GROUP_PLATFORM_NAMES = frozenset({"webchat"})


def default_umo(group_id: str) -> str:
    return f"{DEFAULT_UMO_PLATFORM}:GroupMessage:{group_id}"


def umo_platform(umo: str) -> str:
    """取会话标识里的平台 ID，也就是第一个冒号之前那段。"""

    return umo.split(":", 1)[0]


def candidate_platform_ids(platforms: Iterable[tuple[str, str]]) -> list[str]:
    """从 ``[(平台ID, 适配器类型), ...]`` 里挑出可能承载群消息的平台 ID。

    平台 ID 是用户在面板里给适配器起的名字，适配器类型是 AstrBot 代码里写死的。
    用户改名字改不动类型，所以能用来判断"这个平台会不会有群"的只有类型。
    类型认不出来（空串）时不能排除，宁可多留一个让上层去判断歧义。
    """

    return [
        platform_id
        for platform_id, adapter in platforms
        if platform_id and adapter not in NON_GROUP_PLATFORM_NAMES
    ]


def resolve_umo(
    group_id: str, known_umos: dict[str, str], group_platform_ids: list[str]
) -> tuple[str, str]:
    """决定这个群的播报该发到哪个会话，返回 ``(umo, 来源)``。

    这里不靠猜平台名字，而是一层层往下退：

    1. **群里真来过消息**——``event.unified_msg_origin`` 是既成事实，不可能错。
    2. **从已加载的平台里认**——适配器类型（``meta().name``）是代码里写死的，
       用户改平台名字改不动它。把 WebUI 平台排除掉之后若只剩一个，那它就是
       这个群所在的平台，用它真实的 ID（``meta().id``）拼会话标识。
    3. **退回默认**——剩下唯一的情况是同时装着多个群聊平台，这时确实无从判断
       （猜错就是把消息发进另一个不相干的平台），留给 ``group_umos`` 或者让
       群里有人说句话来解决。
    """

    known = known_umos.get(group_id)
    if known:
        return known, "记住的"
    if len(group_platform_ids) == 1:
        return f"{group_platform_ids[0]}:GroupMessage:{group_id}", "自动识别"
    return default_umo(group_id), "默认"


def unroutable_groups(
    groups: Iterable[str],
    known_umos: dict[str, str],
    platform_ids: list[str],
    group_platform_ids: list[str],
) -> list[tuple[str, str]]:
    """挑出注定发不出去的群，返回 ``[(群号, 它会用的 UMO), ...]``。

    ``platform_ids`` 是当前**已加载**的全部平台 ID：拿不到平台列表时不下结论，
    免得误报。判断走的是与真实发送完全相同的 ``resolve_umo``，所以启动时的结论
    和运行时的行为不会各说各话。
    """

    if not platform_ids:
        return []
    loaded = set(platform_ids)
    problems: list[tuple[str, str]] = []
    for group_id in sorted(groups):
        umo, _ = resolve_umo(group_id, known_umos, group_platform_ids)
        if umo_platform(umo) not in loaded:
            problems.append((group_id, umo))
    return problems


# 端口写到斜杠后面：ws://1.2.3.4/:9961/ws/bot
_PORT_AFTER_SLASH = re.compile(r"^/(:\d+)(/.*)?$")


def backend_url_problem(url: str) -> str | None:
    """挑出几类明显写错的 backend_url；没问题时返回 None。

    最常见的是把端口写到斜杠后面：``ws://1.2.3.4/:9961/ws/bot``。这条 URL 能
    被正常解析，端口于是悄悄退回默认的 80，插件去连一个完全无关的服务，最后
    报一句没头没脑的 ``404 Invalid response status``——离真正的原因隔了很远。
    """

    parts = urlsplit(url)
    if parts.scheme not in {"ws", "wss"}:
        return f"scheme 应该是 ws 或 wss，现在是「{parts.scheme or '空'}」"
    if not parts.hostname:
        return "没有主机名"
    typo = _PORT_AFTER_SLASH.match(parts.path)
    if typo:
        fixed = f"{parts.scheme}://{parts.netloc}{typo.group(1)}{typo.group(2) or '/ws/bot'}"
        return f"端口写到了斜杠后面，应该写成 {fixed}"
    return None


# --- 参与投票的群聊白名单 ---------------------------------------------------


def normalize_group_ids(raw: Any) -> set[str]:
    """把配置里的群号统一成字符串集合。

    配置可能被写成 ``["123456"]``、``[123456]`` 或 ``"123456"``——AstrBot 的
    列表编辑器按字符串存，人工手改 YAML 时常常写成整数。平台回传的群号也
    可能是 int，两边都转成 str 再比较，否则 ``123456 != "123456"`` 会让白名单
    静默失效：插件看起来正常连接，但群里怎么发都没反应。
    """

    if raw is None:
        return set()
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    groups: set[str] = set()
    for item in raw:
        # 空值要显式跳过：str(None) 是 "None"，会在白名单里凭空多出一个
        # 永远匹配不上的"群号"。
        if item is None or isinstance(item, (dict, list, tuple, set)):
            continue
        text = str(item).strip()
        if text:
            groups.add(text)
    return groups


def group_id_from_message(raw_message: Any, fallback: Any) -> str:
    """从平台原始消息里取群号；取不到时退回 ``event.get_group_id()``。"""

    group_id = ""
    if isinstance(raw_message, dict):
        group_id = str(raw_message.get("group_id") or "").strip()
    if not group_id:
        group_id = str(fallback or "").strip()
    return group_id


def suspicious_group_ids(group_ids: Iterable[str]) -> list[str]:
    """挑出明显不像群号的条目，通常是误填了 UMO 或群名。

    QQ 群号是纯数字；``aiocqhttp:GroupMessage:123`` 这类 UMO 或带空格的中文
    群名填进来永远不会匹配，且不会报错，所以启动时单独提示一次。
    """

    return [item for item in sorted(group_ids) if ":" in item or any(char.isspace() for char in item)]


# --- 播报文案 ---------------------------------------------------------------
#
# 群友看到的每一行都从这里出。两条规矩：
#
# 1. **机器值不端给观众。** 协议里的 phase / reason / result_code / event_key
#    都是给程序看的键（``waiting``、``rejected.conflict``、``input.disable_shot``），
#    直接贴进群里和乱码没区别。这里全部查表翻成中文，查不到也不退回原文。
# 2. **不留多余空格。** 中文与数字之间不加空格、不在括号里把机器值再抄一遍、
#    倒计时按秒向上取整——``（剩余 10.0 秒）`` 这种写法一眼就是生成的。

# 游戏端 ``eventtext::DisplayName`` 的镜像，覆盖 th06nc 现役的 21 个异变。
# effect.resolved 只带 event_key、不带显示名（只有 vote.opened 的候选项带 name），
# 所以插件自己得留一份；运行时从候选项收到的 name 优先于这张表，见
# effect_display_name。上游加新异变时旧插件会退回原始键，届时更新这里即可。
EFFECT_NAMES = {
    "resource.bomb.add": "增加一个B",
    "resource.bomb.remove": "减少一个B",
    "resource.life.add": "增加一个残机",
    "resource.life.remove": "减少一个残机",
    "resource.power.set": "设置火力",
    "resource.power.lock": "限时锁定火力",
    "resource.score.add": "增减分数",
    "resource.graze.add": "增减擦弹",
    "protection.invulnerable": "限时无敌",
    "input.invert_x": "左右反转",
    "input.invert_y": "上下反转",
    "input.disable_shot": "禁止射击",
    "input.force_shot": "强制射击",
    "input.disable_bomb": "禁止使用B",
    "input.force_focus": "强制低速",
    "input.disable_focus": "禁止低速",
    "input.rotate_90": "方向顺时针旋转90度",
    "input.swap_axes": "交换水平垂直轴",
    "input.lock_direction": "锁定移动方向",
    "input.random_drift": "随机方向漂移",
    "input.no_diagonal": "禁止斜向移动",
}

EFFECT_RESULT_TEXT = {
    "ok.clamped": "数值被修正",
    "ok.unchanged": "数值不变",
    "rejected.argument": "参数不对",
    "rejected.command_id": "指令编号无效",
    "rejected.conflict": "和场上已有的异变冲突",
    "rejected.cooldown": "还在冷却",
    "rejected.duplicate": "重复指令",
    "rejected.duration": "持续时间不对",
    "rejected.image_unverified": "没认出游戏版本",
    "rejected.no_room": "房间不存在",
    "rejected.not_in_game": "不在游戏中",
    "rejected.param": "参数越界",
    "rejected.paused": "游戏暂停中",
    "rejected.queue_full": "指令排队满了",
    "rejected.replay": "正在看重放",
    "rejected.state": "当前状态不收",
    "rejected.suppressed": "被别的异变压住了",
    "rejected.unavailable": "这个异变现在拿不到",
    "rejected.unknown_event": "游戏端不认识这个异变",
    "rejected.version": "版本对不上",
    "failed.access": "执行失败",
}

# 游戏端 GamePhase / StateReason。协议把它们定义成封闭枚举，所以查不到只可能是
# 上游改了协议而插件还没跟上，这时说"状态未知"比把英文原样端出去强。
PHASE_TEXT = {
    "offline": "游戏离线",
    "title": "标题画面中~",
    "waiting": "等待中",
    "voting": "投票中",
    "replay": "重放中",
}
STATE_REASON_TEXT = {
    "stage_entered": "让我们期待机师的精彩表现",
    "stage_left": "出关卡了",
    "paused": "暂停了",
    "resumed": "继续了",
    "offline": "ATRI和游戏离线了~",
    "sync": "重新同步",
}

# vote.closed 的 reason。只有需要解释的两种才留在这里：
#
# * ``winner``——"得票最高的中选"和下一行的"2号中选"是同一句话，说两遍就成了
#   机器在念模板，索性不写。
# * ``cancelled``——单独处理：作废的一轮没有中选者，照着 winner_choices 写
#   "中选"是错的。
VOTE_CLOSE_TEXT = {
    "tie_all": "三票打平，三个一起上",
    "no_votes_random": "没人投票哦，那就随机抽一个吧~",
}

# vote.ack 的 reason。accepted 只在 counted 为真时出现，不算拒绝理由。
VOTE_ACK_TEXT = {
    "disabled": "投票功能没有开启哦~",
    "not_voting": "现在还没有投票",
    "wrong_round": "投的不是这一轮",
    "bad_choice": "只能投1、2、3",
    "bad_voter": "身份没法识别",
    "duplicate": "这一轮你已经投过了",
    "full": "票满了",
}


def _ceil_seconds(milliseconds: Any) -> int:
    """毫秒向上取整成秒。

    倒计时宁可多报一秒：还剩半秒就显示"剩0秒"，读起来像已经结束了。
    """

    try:
        value = int(milliseconds)
    except (TypeError, ValueError):
        return 0
    return -(-value // 1000)


def option_name_entries(payload: dict[str, Any]) -> dict[str, str]:
    """从任何带候选列表的协议消息里收集 ``event_key → 显示名``。

    游戏端在 vote.opened / vote.snapshot / vote.closed 里都带了每个异变的中文
    名，只有 effect.resolved 不带。把见过的名字攒起来，执行播报就不必依赖下面
    那张手抄的 EFFECT_NAMES——上游加异变时它会自己跟上。
    """

    found: dict[str, str] = {}
    sources = [payload]
    for key in ("active_vote", "latest_snapshot"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for key in ("options", "final_options"):
            items = source.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                event_key = item.get("event_key")
                name = item.get("name")
                if isinstance(event_key, str) and event_key and isinstance(name, str) and name:
                    found[event_key] = name
    return found


def effect_display_name(payload: dict[str, Any], catalogues: Iterable[dict[str, str]] = ()) -> str:
    """给一条 effect.resolved 挑显示名，可靠到不可靠依次往下退。

    1. 消息里直接带的 ``name``——后端目前不发，发了就一定对。
    2. 之前从候选项攒下来的名字——跟随游戏端，不需要改插件。
    3. 手抄的 ``EFFECT_NAMES``——覆盖现役的全部 21 个异变。
    4. 原始 event_key——上游加了插件不认识的异变时，宁可难看也别编一个。
    """

    name = payload.get("name")
    if isinstance(name, str) and name:
        return name
    event_key = str(payload.get("event_key", ""))
    for catalogue in catalogues:
        found = catalogue.get(event_key)
        if found:
            return found
    return EFFECT_NAMES.get(event_key, event_key or "未知异变")


def format_vote_opened(payload: dict[str, Any]) -> str:
    lines = [f"【异变投票#{payload['round_id']}】"]
    for item in payload["options"]:
        lines.append(f"{item['choice']}、{item['name']}")
        description = item.get("description")
        if isinstance(description, str) and description.strip():
            lines.append("   " + " ".join(description.split()))
    lines.append(f"发送1/2/3投票，每人一票｜剩{_ceil_seconds(payload.get('remaining_ms', 0))}秒")
    return "\n".join(lines)


def format_snapshot(payload: dict[str, Any]) -> str:
    parts = " ".join(f"{item['choice']}号{item['votes']}票" for item in payload["options"])
    suffix = "（暂停，计时冻结）" if payload.get("paused") else ""
    return f"【票况#{payload['round_id']}】{parts}{suffix}"


def format_vote_closed(payload: dict[str, Any], options: Iterable[dict[str, Any]] = ()) -> str:
    head = f"【投票结果#{payload['round_id']}】"
    if payload["reason"] == "cancelled":
        return head + "本轮取消，不产生异变"
    names = {item["choice"]: item.get("name", "") for item in options}
    names.update({item["choice"]: item.get("name", "") for item in payload.get("final_options", [])})
    winners = "、".join(
        f"{choice}号" + (f"·{names[choice]}" if names.get(choice) else "")
        for choice in payload["winner_choices"]
    )
    if payload["reason"] == "no_votes_random":
        return f"{head}无人投票，随机选中：{winners}"
    votes = f"{payload['winning_votes']}票" if len(payload["winner_choices"]) == 1 else f"各{payload['winning_votes']}票"
    prefix = "平票，共同中选：" if payload["reason"] == "tie_all" else "中选："
    return f"{head}{prefix}{winners}（{votes}／共{payload['total_votes']}票）"


def format_effect(payload: dict[str, Any], catalogues: Iterable[dict[str, str]] = ()) -> str:
    name = effect_display_name(payload, catalogues)
    detail = EFFECT_RESULT_TEXT.get(str(payload.get("result_code", "")))
    head = "异变生效" if payload["status"] == "applied" else "异变没生效"
    return f"【{head}】{name}" + (f"，{detail}" if detail else "")


def format_game_state(payload: dict[str, Any]) -> str:
    phase = PHASE_TEXT.get(str(payload.get("phase", "")), "状态未知")
    reason = STATE_REASON_TEXT.get(str(payload.get("reason", "")))
    # reason 是变化的原因、phase 是变完之后的样子，中文里按这个顺序说才通顺。
    return f"【游戏状态】{reason}，当前{phase}" if reason else f"【游戏状态】当前{phase}"


def format_game_offline() -> str:
    return "【投票中断】游戏连接已断开，请等待下一轮投票"


def format_vote_ack(payload: dict[str, Any]) -> str:
    choice = payload.get("choice", "?")
    if payload.get("counted"):
        return f"【已计票】{choice}号，记下了"
    reason = VOTE_ACK_TEXT.get(str(payload.get("reason", "")), "没算上")
    return f"【没算上】{choice}号，{reason}"


def format_cast_error(payload: dict[str, Any]) -> str:
    return f"【投票没送出】{payload.get('message', '后端没说原因')}"
