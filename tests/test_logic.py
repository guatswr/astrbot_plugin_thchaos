from logic import (
    NON_GROUP_PLATFORM_NAMES,
    backend_url_problem,
    candidate_platform_ids,
    default_umo,
    format_snapshot,
    group_id_from_message,
    normalize_group_ids,
    parse_vote_choice,
    pseudonymous_voter_id,
    resolve_umo,
    suspicious_group_ids,
    umo_platform,
    unroutable_groups,
)


def test_plugin_vote_parser_and_pseudonym():
    assert parse_vote_choice(" 2 ") == 2
    assert parse_vote_choice("12") is None
    assert parse_vote_choice("投2") is None
    first = pseudonymous_voter_id("secret", "123")
    assert first == pseudonymous_voter_id("secret", "123")
    assert first != pseudonymous_voter_id("secret", "124")
    assert len(first) <= 63 and len(first) == 63 and first.isascii() and " " not in first


def test_snapshot_format_and_umo():
    assert default_umo("123456") == "aiocqhttp:GroupMessage:123456"
    text = format_snapshot(
        {
            "round_id": 3,
            "options": [{"choice": 1, "votes": 2}, {"choice": 2, "votes": 0}, {"choice": 3, "votes": 1}],
            "paused": True,
        }
    )
    assert "#3" in text and "1:2票" in text and "冻结" in text


def test_normalize_group_ids_accepts_every_shape_a_config_can_take():
    # AstrBot 的列表编辑器存字符串，手工改 YAML 时常常写成整数。
    assert normalize_group_ids(["123456", "654321"]) == {"123456", "654321"}
    assert normalize_group_ids([123456, 654321]) == {"123456", "654321"}
    assert normalize_group_ids("123456") == {"123456"}
    assert normalize_group_ids(123456) == {"123456"}
    assert normalize_group_ids([" 123456 "]) == {"123456"}
    assert normalize_group_ids(None) == set()
    assert normalize_group_ids([]) == set()
    assert normalize_group_ids("") == set()
    assert normalize_group_ids({"unexpected": "shape"}) == set()
    # 列表里混进空值不能变成"空字符串群号"参与比较
    assert normalize_group_ids(["123456", "", None]) == {"123456"}


def test_group_id_lookup_survives_int_from_platform():
    # 平台回传的 group_id 可能是 int；不转字符串就会 123456 != "123456"，
    # 白名单静默失效——插件连得上、群里却没反应。
    assert group_id_from_message({"group_id": 123456}, None) == "123456"
    assert group_id_from_message({"group_id": "123456"}, None) == "123456"
    assert group_id_from_message({}, 123456) == "123456"
    assert group_id_from_message(None, " 123456 ") == "123456"
    # 原始消息优先于 event.get_group_id()
    assert group_id_from_message({"group_id": "111"}, "222") == "111"
    # 两者都没有：私聊或平台不带群号
    assert group_id_from_message({}, None) == ""
    assert group_id_from_message(None, None) == ""


def test_suspicious_group_ids_flags_umo_and_group_names():
    assert suspicious_group_ids({"123456", "654321"}) == []
    assert suspicious_group_ids({"aiocqhttp:GroupMessage:123456"}) == ["aiocqhttp:GroupMessage:123456"]
    assert suspicious_group_ids({"东方 THChaos 观众群"}) == ["东方 THChaos 观众群"]
    assert suspicious_group_ids({"123456", "bad:one"}) == ["bad:one"]


def test_correct_backend_urls_are_accepted():
    for url in (
        "ws://127.0.0.1:8765/ws/bot",
        "ws://192.0.2.10:9961/ws/bot",
        "wss://vote.example.com/ws/bot",
    ):
        assert backend_url_problem(url) is None, url


def test_port_written_after_a_slash_is_caught_with_the_fix():
    # ws://1.2.3.4/:9961/ws/bot 能被正常解析，端口悄悄退回 80，最后报一句
    # 没头没脑的 "404 Invalid response status"。
    problem = backend_url_problem("ws://192.0.2.10/:9961/ws/bot")
    assert problem is not None
    assert "ws://192.0.2.10:9961/ws/bot" in problem


def test_wrong_scheme_and_missing_host_are_caught():
    assert backend_url_problem("http://1.2.3.4:9961/ws/bot") is not None
    assert backend_url_problem("192.0.2.10:9961/ws/bot") is not None


def test_umo_platform_is_the_part_before_the_first_colon():
    assert umo_platform("aiocqhttp:GroupMessage:123456") == "aiocqhttp"
    assert umo_platform("atri:GroupMessage:123456") == "atri"


def test_resolve_umo_prefers_what_the_group_actually_used():
    # 群里真来过消息时 event.unified_msg_origin 是既成事实，不可能猜错。
    umo, source = resolve_umo("111", {"111": "atri:GroupMessage:111"}, [])
    assert umo == "atri:GroupMessage:111"
    assert source == "记住的"


def test_resolve_umo_recognises_the_only_group_platform():
    # 用户实际遇到的情况：平台 ID 是 atri，插件却硬拼了 aiocqhttp。
    # 排掉 WebUI 之后只剩一个群聊平台（见 candidate_platform_ids），那就是它，
    # 不需要用户填任何配置。
    umo, source = resolve_umo("123456789", {}, ["atri"])
    assert umo == "atri:GroupMessage:123456789"
    assert source == "自动识别"


def test_resolve_umo_refuses_to_guess_between_several_group_platforms():
    # 同时装着两个群聊平台时，猜错就是把消息发进另一个不相干的平台。
    # 宁可退回默认值并告警，也不要静默发错地方。
    umo, source = resolve_umo("111", {}, [])
    assert umo == "aiocqhttp:GroupMessage:111"
    assert source == "默认"


def test_webchat_is_not_treated_as_a_group_platform():
    # WebUI 的"群"是网页里的会话。AstrBot 核心自己也靠 meta().name 把它排除掉。
    assert "webchat" in NON_GROUP_PLATFORM_NAMES
    assert candidate_platform_ids(
        [("atri", "aiocqhttp"), ("webchat", "webchat")]
    ) == ["atri"]
    assert candidate_platform_ids([("webchat", "webchat")]) == []
    # 适配器类型认不出来时不能误排除
    assert candidate_platform_ids([("atri", "")]) == ["atri"]


def test_unroutable_groups_is_empty_when_the_platform_can_be_recognised():
    # 这一条就是那个"非要手动指定 UMO"的问题：atri + webchat 时插件自己认得出来，
    # 启动时不该再报警要用户去填 group_umos。
    assert unroutable_groups({"123456789"}, {}, ["atri", "webchat"], ["atri"]) == []


def test_unroutable_groups_names_a_mismatched_manual_umo():
    # 手写的 group_umos 前缀不是已加载的平台——除非群里真来过消息覆盖它，
    # 否则它永远发不出去。
    known = {"111": "napcat:GroupMessage:111"}
    assert unroutable_groups({"111"}, known, ["atri", "webchat"], ["atri"]) == [
        ("111", "napcat:GroupMessage:111")
    ]


def test_unroutable_groups_reports_genuine_ambiguity():
    # 两个群聊平台，无从判断，只能退回默认的 aiocqhttp——而它并不在已加载列表里。
    platforms = ["atri", "telegram"]
    assert unroutable_groups({"111"}, {}, platforms, platforms) == [
        ("111", "aiocqhttp:GroupMessage:111")
    ]


def test_unroutable_check_stays_quiet_without_a_platform_list():
    # 拿不到平台列表（离线 shim、精简 Context）时不下结论，免得误报。
    assert unroutable_groups({"111"}, {}, [], []) == []
