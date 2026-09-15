from logic import (
    backend_url_problem,
    default_umo,
    format_snapshot,
    group_id_from_message,
    normalize_group_ids,
    parse_vote_choice,
    pseudonymous_voter_id,
    suspicious_group_ids,
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
