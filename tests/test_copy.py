"""播报文案的契约。

群里发出去的每一行都在这里定死，因为这些字是观众唯一看得见的东西：

* 不出现机器值——phase / reason / result_code / event_key 都是给程序看的键，
  贴进群里和乱码没区别。
* 不留多余空格，不写假精度（``剩余 10.0 秒``）。
* 枚举空间取自 thchaos_backend 的 ``protocol/payloads.py``，用 ``StrEnum``
  逐条抄下来；游戏端加了新取值而插件没跟上时，下面的覆盖测试会失败。
"""

from logic import (
    EFFECT_NAMES,
    EFFECT_RESULT_TEXT,
    PHASE_TEXT,
    STATE_REASON_TEXT,
    VOTE_ACK_TEXT,
    VOTE_CLOSE_TEXT,
    effect_display_name,
    format_cast_error,
    format_effect,
    format_game_offline,
    format_game_state,
    format_snapshot,
    format_vote_ack,
    format_vote_closed,
    format_vote_opened,
    option_name_entries,
)

# --- 枚举空间：后端 protocol/payloads.py 里的 StrEnum 全量 -------------------

BACKEND_PHASES = {"offline", "title", "waiting", "voting", "replay"}
BACKEND_STATE_REASONS = {"stage_entered", "stage_left", "paused", "resumed", "offline", "sync"}
BACKEND_CLOSE_REASONS = {"winner", "tie_all", "no_votes_random", "cancelled"}
BACKEND_ACK_REASONS = {
    "accepted",
    "disabled",
    "not_voting",
    "wrong_round",
    "bad_choice",
    "bad_voter",
    "duplicate",
    "full",
}

# 游戏端 eventtext::DisplayName 现役的 21 个异变键。
BACKEND_EVENT_KEYS = {
    "resource.bomb.add",
    "resource.bomb.remove",
    "resource.life.add",
    "resource.life.remove",
    "resource.power.set",
    "resource.power.lock",
    "resource.score.add",
    "resource.graze.add",
    "protection.invulnerable",
    "input.invert_x",
    "input.invert_y",
    "input.disable_shot",
    "input.force_shot",
    "input.disable_bomb",
    "input.force_focus",
    "input.disable_focus",
    "input.rotate_90",
    "input.swap_axes",
    "input.lock_direction",
    "input.random_drift",
    "input.no_diagonal",
}

# 游戏端 registry 里出现过的 result_code。
BACKEND_RESULT_CODES = {
    "ok.clamped",
    "ok.unchanged",
    "rejected.argument",
    "rejected.command_id",
    "rejected.conflict",
    "rejected.cooldown",
    "rejected.duplicate",
    "rejected.duration",
    "rejected.image_unverified",
    "rejected.no_room",
    "rejected.not_in_game",
    "rejected.param",
    "rejected.paused",
    "rejected.queue_full",
    "rejected.replay",
    "rejected.state",
    "rejected.suppressed",
    "rejected.unavailable",
    "rejected.unknown_event",
    "rejected.version",
    "failed.access",
}


def test_every_enum_value_has_chinese_copy():
    # 少一条就会把英文键端进群里，正是这次要修的东西。
    # winner 和 cancelled 不在这两张表里：前者不需要解释、后者单独处理，
    # 各自有下面的用例守着。
    assert set(PHASE_TEXT) == BACKEND_PHASES
    assert set(STATE_REASON_TEXT) == BACKEND_STATE_REASONS
    assert set(VOTE_CLOSE_TEXT) == BACKEND_CLOSE_REASONS - {"cancelled", "winner"}
    assert set(VOTE_ACK_TEXT) == BACKEND_ACK_REASONS - {"accepted"}  # accepted 即"已计票"
    assert set(EFFECT_NAMES) == BACKEND_EVENT_KEYS
    assert set(EFFECT_RESULT_TEXT) == BACKEND_RESULT_CODES


def test_no_copy_value_contains_ascii_spaces_or_leaked_english():
    # "少一些 AI 味"的具体形状：中文之间不塞空格，也不夹着机器键混进来。
    # 允许大写 B——那是游戏端自己写的中文名（"增加一个B"），弹幕圈就叫它 B。
    tables = {
        "PHASE_TEXT": PHASE_TEXT,
        "STATE_REASON_TEXT": STATE_REASON_TEXT,
        "VOTE_CLOSE_TEXT": VOTE_CLOSE_TEXT,
        "VOTE_ACK_TEXT": VOTE_ACK_TEXT,
        "EFFECT_NAMES": EFFECT_NAMES,
        "EFFECT_RESULT_TEXT": EFFECT_RESULT_TEXT,
    }
    for table_name, table in tables.items():
        for key, value in table.items():
            assert " " not in value, f"{table_name}[{key}] 里有空格：{value!r}"
            assert not any("a" <= ch <= "z" for ch in value), f"{table_name}[{key}] 夹了英文：{value!r}"


# --- 开票 -------------------------------------------------------------------


def test_vote_opened_lists_options_and_counts_down_in_whole_seconds():
    text = format_vote_opened(
        {
            "round_id": 3,
            "options": [
                {"choice": 1, "name": "禁止射击"},
                {"choice": 2, "name": "强制低速"},
                {"choice": 3, "name": "左右反转"},
            ],
            "remaining_ms": 9500,
        }
    )
    assert text == (
        "【异变投票#3】\n"
        "1、禁止射击\n"
        "2、强制低速\n"
        "3、左右反转\n"
        "发送1/2/3投票，每人一票｜剩10秒"
    )


def test_countdown_rounds_up_so_it_never_says_zero_while_time_remains():
    options = [{"choice": i, "name": "x"} for i in (1, 2, 3)]
    assert "剩1秒" in format_vote_opened({"round_id": 1, "options": options, "remaining_ms": 1})
    assert "剩1秒" in format_vote_opened({"round_id": 1, "options": options, "remaining_ms": 1000})
    assert "剩2秒" in format_vote_opened({"round_id": 1, "options": options, "remaining_ms": 1001})
    # 字段缺失或类型不对时不能抛，倒计时退化成 0 秒。
    assert "剩0秒" in format_vote_opened({"round_id": 1, "options": options})


# --- 票况 -------------------------------------------------------------------


def test_snapshot_is_compact_and_marks_a_frozen_clock():
    payload = {
        "round_id": 8,
        "options": [{"choice": 1, "votes": 0}, {"choice": 2, "votes": 1}, {"choice": 3, "votes": 0}],
        "paused": True,
    }
    assert format_snapshot(payload) == "【票况#8】1号0票 2号1票 3号0票（暂停，计时冻结）"
    payload["paused"] = False
    assert format_snapshot(payload) == "【票况#8】1号0票 2号1票 3号0票"


def test_snapshot_has_no_space_before_the_round_number():
    # 旧文案是「【票况 #8】」，中文和 # 之间的空格就是那股生成味。
    payload = {
        "round_id": 8,
        "options": [{"choice": 1, "votes": 0}, {"choice": 2, "votes": 0}, {"choice": 3, "votes": 0}],
    }
    assert "#8】" in format_snapshot(payload)
    assert " #8" not in format_snapshot(payload)


# --- 结果 -------------------------------------------------------------------


def test_vote_closed_names_the_winner_and_the_totals():
    # "得票最高的中选"和"2号中选"是同一句话，所以 winner 只出一行。
    text = format_vote_closed(
        {
            "round_id": 7,
            "reason": "winner",
            "winner_choices": [2],
            "winning_votes": 4,
            "total_votes": 9,
        }
    )
    assert text == "【投票结果#7】中选：2号（4票／共9票）"


def test_a_tie_says_each_winner_got_that_many():
    text = format_vote_closed(
        {
            "round_id": 7,
            "reason": "tie_all",
            "winner_choices": [1, 2, 3],
            "winning_votes": 2,
            "total_votes": 6,
        }
    )
    assert text == "【投票结果#7】平票，共同中选：1号、2号、3号（各2票／共6票）"


def test_no_votes_random_is_not_phrased_as_a_win():
    text = format_vote_closed(
        {
            "round_id": 7,
            "reason": "no_votes_random",
            "winner_choices": [1],
            "winning_votes": 0,
            "total_votes": 0,
        }
    )
    assert "无人投票，随机选中" in text
    assert text.endswith("1号")
    assert "0票" not in text


def test_cancelled_round_never_claims_a_winner():
    # 作废的一轮协议里仍带着 winner_choices，照着写"中选"就是撒谎。
    text = format_vote_closed(
        {
            "round_id": 7,
            "reason": "cancelled",
            "winner_choices": [1],
            "winning_votes": 3,
            "total_votes": 5,
        }
    )
    assert text == "【投票结果#7】本轮取消，不产生异变"
    assert "中选" not in text


# --- 异变执行 ---------------------------------------------------------------


def test_effect_shows_the_chinese_name_and_no_machine_values():
    # 旧文案是「【Chaos 执行】input.disable_shot：已生效（applied）」，
    # 内部键和英文状态双双漏给了群友。
    text = format_effect({"status": "applied", "event_key": "input.disable_shot", "result_code": "ok.clamped"})
    assert text == "【异变生效】禁止射击，数值被修正"
    assert "input.disable_shot" not in text
    assert "applied" not in text


def test_rejected_effect_explains_itself_in_chinese():
    text = format_effect({"status": "rejected", "event_key": "input.force_shot", "result_code": "rejected.conflict"})
    assert text == "【异变没生效】强制射击，和场上已有的异变冲突"


def test_an_unknown_result_code_drops_the_detail_instead_of_leaking_it():
    text = format_effect({"status": "rejected", "event_key": "input.force_shot", "result_code": "rejected.brand_new"})
    assert text == "【异变没生效】强制射击"


def test_an_unknown_event_key_falls_back_to_the_raw_key_rather_than_inventing_a_name():
    # 上游加异变时这里的原始键会露出来一次——难看，但比编一个名字诚实。
    text = format_effect({"status": "applied", "event_key": "input.brand_new", "result_code": "ok.unchanged"})
    assert text == "【异变生效】input.brand_new，数值不变"


# --- 异变显示名：优先用游戏端给的名字 ---------------------------------------


def test_names_are_harvested_from_every_payload_that_carries_options():
    assert option_name_entries(
        {"options": [{"event_key": "input.invert_x", "name": "左右反转"}]}
    ) == {"input.invert_x": "左右反转"}
    # vote.closed 用的是 final_options
    assert option_name_entries(
        {"final_options": [{"event_key": "input.invert_x", "name": "左右反转"}]}
    ) == {"input.invert_x": "左右反转"}
    # game.sync 把候选项嵌在 active_vote / latest_snapshot 里
    assert option_name_entries(
        {"active_vote": {"options": [{"event_key": "a.b", "name": "甲"}]}, "latest_snapshot": {"options": [{"event_key": "c.d", "name": "乙"}]}}
    ) == {"a.b": "甲", "c.d": "乙"}
    # 残缺的条目不能变出一个空名字
    assert option_name_entries({"options": [{"event_key": "a.b"}, {"name": "乙"}, "x", None]}) == {}
    assert option_name_entries({}) == {}


def test_a_name_seen_at_runtime_beats_the_hand_copied_table():
    # 游戏端改了名字、或者插件表里还没有这个异变时，以收到过的名字为准。
    harvested = {"input.disable_shot": "游戏端改过的新名字", "input.brand_new": "新异变"}
    assert effect_display_name({"event_key": "input.disable_shot"}, (harvested,)) == "游戏端改过的新名字"
    assert effect_display_name({"event_key": "input.brand_new"}, (harvested,)) == "新异变"
    # 攒到的表里没有，才退回手抄的那份
    assert effect_display_name({"event_key": "input.invert_x"}, (harvested,)) == "左右反转"


def test_a_name_in_the_payload_wins_over_every_table():
    payload = {"event_key": "input.invert_x", "name": "消息自带的名字"}
    assert effect_display_name(payload, ({"input.invert_x": "攒下来的"},)) == "消息自带的名字"
    # 空字符串等同于没带
    assert effect_display_name({"event_key": "input.invert_x", "name": ""}) == "左右反转"


# --- 游戏状态 ---------------------------------------------------------------


def test_game_state_reads_as_a_sentence_and_leaks_no_english():
    assert format_game_state({"phase": "waiting", "reason": "resumed"}) == "【游戏状态】继续了，当前等待中"
    assert format_game_state({"phase": "waiting", "reason": "paused"}) == "【游戏状态】暂停了，当前等待中"
    assert format_game_state({"phase": "offline", "reason": "stage_left"}) == "【游戏状态】出关卡了，当前游戏离线"


def test_game_state_without_a_reason_still_reads():
    assert format_game_state({"phase": "voting"}) == "【游戏状态】当前投票中"


def test_unknown_state_values_do_not_leak_english():
    # 协议把它们定义成封闭枚举，走到这里只可能是上游改了协议。
    assert format_game_state({"phase": "brand_new"}) == "【游戏状态】当前状态未知"
    assert format_game_state({"phase": "waiting", "reason": "brand_new"}) == "【游戏状态】当前等待中"
    assert format_game_state({}) == "【游戏状态】当前状态未知"


# --- 计票回执与故障 ---------------------------------------------------------


def test_vote_ack_says_it_back_in_one_line():
    assert format_vote_ack({"counted": True, "choice": 2}) == "【已计票】2号，记下了"
    assert format_vote_ack({"counted": False, "choice": 2, "reason": "duplicate"}) == "【没算上】2号，这一轮你已经投过了"
    assert format_vote_ack({"counted": False, "choice": 3, "reason": "full"}) == "【没算上】3号，票满了"


def test_vote_ack_never_shows_a_raw_reason():
    text = format_vote_ack({"counted": False, "choice": 1, "reason": "rejected"})
    assert text == "【没算上】1号，没算上"
    assert "rejected" not in text


def test_offline_and_cast_error_are_plain_chinese():
    assert format_game_offline() == "【投票中断】游戏连接已断开，请等待下一轮投票"
    assert format_cast_error({"message": "round 已关闭"}) == "【投票没送出】round 已关闭"
    assert format_cast_error({}) == "【投票没送出】后端没说原因"


# --- 通用：整套文案里不留 ASCII 空格（除了选项名与实际数据之间） ------------


def test_announcements_have_no_double_spaces():
    texts = [
        format_vote_opened(
            {
                "round_id": 3,
                "options": [{"choice": i, "name": "禁止射击"} for i in (1, 2, 3)],
                "remaining_ms": 10000,
            }
        ),
        format_snapshot(
            {"round_id": 8, "options": [{"choice": i, "votes": 0} for i in (1, 2, 3)], "paused": True}
        ),
        format_vote_closed(
            {"round_id": 7, "reason": "winner", "winner_choices": [2], "winning_votes": 4, "total_votes": 9}
        ),
        format_effect({"status": "applied", "event_key": "input.invert_x", "result_code": "ok.clamped"}),
        format_game_state({"phase": "waiting", "reason": "resumed"}),
        format_game_offline(),
        format_vote_ack({"counted": False, "choice": 1, "reason": "duplicate"}),
        format_cast_error({}),
    ]
    for text in texts:
        assert "  " not in text, text
        assert "\n " not in text, text
        assert " \n" not in text, text
