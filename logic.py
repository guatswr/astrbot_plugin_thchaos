"""AstrBot 插件的纯函数，脱离 AstrBot 运行时即可测试。"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Iterable


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


def default_umo(group_id: str) -> str:
    return f"aiocqhttp:GroupMessage:{group_id}"


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
