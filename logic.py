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


def format_vote_opened(payload: dict[str, Any]) -> str:
    lines = [f"【观众投票 #{payload['round_id']}】"]
    for item in payload["options"]:
        lines.append(f"{item['choice']}. {item['name']}")
    seconds = payload.get("remaining_ms", 0) / 1000
    lines.append(f"回复 1/2/3 投票（剩余 {seconds:.1f} 秒）")
    return "\n".join(lines)


def format_snapshot(payload: dict[str, Any]) -> str:
    parts = [f"{item['choice']}:{item['votes']}票" for item in payload["options"]]
    suffix = "（游戏暂停，计时冻结）" if payload.get("paused") else ""
    return f"【票况 #{payload['round_id']}】" + "  ".join(parts) + suffix


def format_vote_closed(payload: dict[str, Any]) -> str:
    labels = {
        "winner": "最高票",
        "tie_all": "平票，三个全开",
        "no_votes_random": "无人投票，随机抽取",
        "cancelled": "已取消",
    }
    winners = ", ".join(str(item) for item in payload["winner_choices"])
    return (
        f"【投票结果 #{payload['round_id']}】{labels.get(payload['reason'], payload['reason'])}\n"
        f"选项：{winners}；最高票 {payload['winning_votes']}，总票 {payload['total_votes']}"
    )


def format_effect(payload: dict[str, Any]) -> str:
    status = "已生效" if payload["status"] == "applied" else "被游戏拒绝"
    return f"【Chaos 执行】{payload['name'] if 'name' in payload else payload['event_key']}：{status}（{payload['result_code']}）"
