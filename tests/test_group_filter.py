"""白名单行为测试：只有 group_ids 里的群，投票才会被转发出去。

这些用例走的是插件真实的 on_message 路径，只把发包换成记录器，不碰网络。
main.py 依赖 astrbot 包，离线时它自带 shim；装了 AstrBot 的环境跳过本文件，
那种情况下应该在真实实例上验证。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))  # 让 astrbot_plugin_thchaos 成为可导入的包

try:
    import astrbot  # noqa: F401

    HAS_ASTRBOT = True
except ImportError:
    HAS_ASTRBOT = False

pytestmark = pytest.mark.skipif(HAS_ASTRBOT, reason="需要离线 shim；AstrBot 环境下请在真实实例上验证")

from astrbot_plugin_thchaos.main import ThChaosPlugin  # noqa: E402


class FakeEvent:
    def __init__(self, group_id, text, *, sender_id="10001", message_id="m1"):
        raw = {} if group_id is None else {"group_id": group_id, "message_id": message_id}
        self.message_obj = types.SimpleNamespace(raw_message=raw)
        self.message_str = text
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"
        self._group_id = group_id
        self._sender_id = sender_id

    def get_group_id(self):
        if self._group_id is None:
            raise RuntimeError("私聊没有群号")
        return self._group_id

    def get_sender_id(self):
        return self._sender_id


class FakeContext:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, str(chain)))


class UnroutableContext(FakeContext):
    """复刻 Context.send_message 找不到平台时的行为：返回 False，不抛异常。"""

    async def send_message(self, umo, chain):
        return False


def make_plugin(groups, *, active_round=7, game_instance="g-1"):
    config = {
        "backend_url": "ws://127.0.0.1:1/ws/bot",
        "token": "t",
        "room_id": "main",
        "group_ids": groups,
        "voter_hmac_secret": "secret",
    }
    plugin = ThChaosPlugin(FakeContext(), config)
    plugin._active_round = active_round
    plugin._game_instance_id = game_instance
    casts: list[dict] = []

    async def recorder(message_type, payload, *, include_instance=True):
        casts.append({"type": message_type, "payload": payload})

    plugin._send = recorder  # type: ignore[method-assign]
    return plugin, casts


def run(coro):
    return asyncio.run(coro)


def test_only_whitelisted_group_is_forwarded():
    plugin, casts = make_plugin(["111"])
    run(plugin.on_message(FakeEvent("111", "2")))
    run(plugin.on_message(FakeEvent("222", "2")))
    assert [cast["payload"]["choice"] for cast in casts] == [2]
    assert casts[0]["payload"]["source_group_id"] == "111"


def test_int_group_id_from_platform_matches_string_config():
    # 平台回传 int、配置是 str：不归一化就会白名单静默失效
    plugin, casts = make_plugin(["111"])
    run(plugin.on_message(FakeEvent(111, "3")))
    assert [cast["payload"]["choice"] for cast in casts] == [3]


def test_config_written_with_int_group_ids_is_accepted():
    plugin, casts = make_plugin([111])
    run(plugin.on_message(FakeEvent("111", "1")))
    assert len(casts) == 1


def test_non_vote_text_in_whitelisted_group_is_ignored():
    plugin, casts = make_plugin(["111"])
    for text in ("12", "我投2", "2 快", "投票", ""):
        run(plugin.on_message(FakeEvent("111", text)))
    assert casts == []


def test_no_active_round_means_no_cast():
    plugin, casts = make_plugin(["111"], active_round=None)
    run(plugin.on_message(FakeEvent("111", "2")))
    assert casts == []


def test_no_game_instance_means_no_cast():
    plugin, casts = make_plugin(["111"], game_instance=None)
    run(plugin.on_message(FakeEvent("111", "2")))
    assert casts == []


def test_empty_whitelist_ignores_every_group():
    plugin, casts = make_plugin([])
    run(plugin.on_message(FakeEvent("111", "2")))
    assert casts == []


def test_private_message_is_ignored():
    plugin, casts = make_plugin(["111"])
    run(plugin.on_message(FakeEvent(None, "2")))
    assert casts == []


def test_unlisted_group_is_logged_once():
    plugin, _ = make_plugin(["111"])
    run(plugin.on_message(FakeEvent("222", "2")))
    run(plugin.on_message(FakeEvent("222", "3")))
    assert plugin._reported_foreign_groups == {"222"}


def test_whitelisted_group_learns_its_umo():
    plugin, _ = make_plugin(["111"])
    run(plugin.on_message(FakeEvent("111", "hello")))
    assert plugin._umos["111"] == "aiocqhttp:GroupMessage:111"


def test_announcement_reaches_a_routable_session():
    context = FakeContext()
    plugin = ThChaosPlugin(context, {"group_ids": ["111"]})
    run(plugin._announce_group("111", "【观众投票 #1】"))
    assert context.sent == [("aiocqhttp:GroupMessage:111", "【观众投票 #1】")]


def test_unroutable_session_is_reported_instead_of_dropped_silently(caplog):
    # 平台 ID 对不上时 AstrBot 只返回 False，消息被丢掉且不报错。插件必须自己
    # 说出来，否则「已连接后端但群里没反应」是一个完全没有日志的故障。
    plugin = ThChaosPlugin(UnroutableContext(), {"group_ids": ["111"]})
    with caplog.at_level(logging.WARNING):
        run(plugin._announce_group("111", "【观众投票 #1】"))
    assert "111" in caplog.text
    assert "aiocqhttp:GroupMessage:111" in caplog.text
    assert "group_umos" in caplog.text


def test_routable_session_logs_no_failure(caplog):
    plugin = ThChaosPlugin(FakeContext(), {"group_ids": ["111"]})
    with caplog.at_level(logging.WARNING):
        run(plugin._announce_group("111", "【观众投票 #1】"))
    assert "发送消息失败" not in caplog.text


# --- 生命周期：建连必须发生在 initialize() ---------------------------------


def _initialize_with_recorder(config, context=None):
    """跑一遍 initialize()，把网络协程换成记录器，返回 (plugin, started)。"""
    plugin = ThChaosPlugin(context or FakeContext(), config)
    started: list[bool] = []

    async def fake_run():
        started.append(True)
        await asyncio.sleep(3600)

    async def main():
        plugin._run_network = fake_run  # type: ignore[method-assign]
        await plugin.initialize()
        assert plugin._network_task is not None
        await asyncio.sleep(0)  # 让协程真正跑起来再取消
        plugin._network_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await plugin._network_task

    run(main())
    return plugin, started


def test_initialize_starts_the_backend_connection():
    _, started = _initialize_with_recorder({"token": "t", "group_ids": ["111"]})
    assert started == [True]


def test_initialize_without_token_does_not_connect():
    plugin = ThChaosPlugin(FakeContext(), {"token": "", "group_ids": ["111"]})
    run(plugin.initialize())
    assert plugin._network_task is None


def test_initialize_twice_on_one_instance_opens_one_connection():
    # 第二条连接会让每个群把同一份播报收到两遍。
    plugin = ThChaosPlugin(FakeContext(), {"token": "t"})
    started: list[bool] = []

    async def fake_run():
        started.append(True)
        await asyncio.sleep(3600)

    async def main():
        plugin._run_network = fake_run  # type: ignore[method-assign]
        await plugin.initialize()
        first = plugin._network_task
        await plugin.initialize()
        assert plugin._network_task is first
        await asyncio.sleep(0)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

    run(main())
    assert started == [True]


def test_terminate_cancels_the_connection():
    """重载插件时 AstrBot 会先 terminate 旧实例；不取消就会留下一条旧连接。

    旧连接不断，后端就仍把它算作一个 bot，播报会重复发。
    """
    plugin = ThChaosPlugin(FakeContext(), {"token": "t"})

    async def fake_run():
        await asyncio.sleep(3600)

    async def main():
        plugin._run_network = fake_run  # type: ignore[method-assign]
        await plugin.initialize()
        task = plugin._network_task
        assert task is not None
        await asyncio.sleep(0)  # 让 _run_network 真正开始跑
        await plugin.terminate()
        assert task.cancelled()

    run(main())


def test_initialize_warns_about_a_typo_backend_url(caplog):
    # 端口写到斜杠后面时，aiohttp 报的是 "404 Invalid response status"，
    # 和真正的原因隔了十万八千里；插件自己在启动时把它点破。
    plugin = ThChaosPlugin(
        FakeContext(),
        {"token": "t", "backend_url": "ws://192.0.2.10/:9961/ws/bot"},
    )

    async def fake_run():
        await asyncio.sleep(3600)

    async def main():
        plugin._run_network = fake_run  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING):
            await plugin.initialize()
        task = plugin._network_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    run(main())
    assert "ws://192.0.2.10:9961/ws/bot" in caplog.text


class FakePlatform:
    """平台实例替身。id 与 adapter 是两个不同的东西：

    id 是用户在面板里给这个平台起的名字（默认 aiocqhttp，改过就是 atri 这类），
    adapter 是 AstrBot 代码里写死的适配器类型（aiocqhttp / webchat / telegram…）。
    """

    def __init__(self, platform_id, adapter="aiocqhttp"):
        self._id = platform_id
        self._adapter = adapter

    def meta(self):
        return types.SimpleNamespace(id=self._id, name=self._adapter)


class PlatformAwareContext(FakeContext):
    """带 platform_manager 的替身，能列出已加载的平台。

    platforms 里每一项可以是 "atri"（适配器类型按默认的 aiocqhttp 算），
    或者 ("webchat", "webchat") 这样的 (ID, 适配器类型) 二元组。
    """

    def __init__(self, platforms, *, routable=False):
        super().__init__()
        self._insts = [
            FakePlatform(*item) if isinstance(item, tuple) else FakePlatform(item)
            for item in platforms
        ]
        self.platform_manager = types.SimpleNamespace(platform_insts=self._insts)
        self.routable = routable

    def get_insts(self):
        return self._insts

    async def send_message(self, umo, chain):
        self.sent.append((umo, str(chain)))
        return True if self.routable else False


class LegacyPlatformManagerContext(PlatformAwareContext):
    """只有 platform_insts 字段、没有 get_insts() 的旧式 platform_manager。"""

    get_insts = None


def test_unroutable_session_names_the_available_platforms(caplog):
    # UMO 前缀是用户在面板里起的平台 ID。插件能看见实际加载了哪些平台，
    # 就应该直接报出来，而不是让人去翻配置猜。
    plugin = ThChaosPlugin(PlatformAwareContext(["napcat", "qq_official"]), {"group_ids": ["111"]})
    with caplog.at_level(logging.WARNING):
        run(plugin._announce_group("111", "【观众投票 #1】"))
    assert "napcat" in caplog.text
    assert "qq_official" in caplog.text
    assert "GroupMessage:111" in caplog.text


def test_announcement_finds_the_only_group_platform_with_no_config_at_all():
    # "非要手动指定 UMO"那个问题的核心用例：平台 ID 是 atri，插件应该自己认出来
    # 并把消息发对地方，用户什么配置都不用填。
    context = PlatformAwareContext([("atri", "aiocqhttp"), ("webchat", "webchat")], routable=True)
    plugin = ThChaosPlugin(context, {"group_ids": ["123456789"]})
    run(plugin._announce_group("123456789", "【观众投票 #1】"))
    assert context.sent == [("atri:GroupMessage:123456789", "【观众投票 #1】")]


def test_webchat_platform_is_never_picked_as_the_target(caplog):
    # 只剩 WebUI 平台时不能瞎猜——它的"群"是网页里的会话，发过去等于没发。
    context = PlatformAwareContext([("webchat", "webchat")])
    plugin = ThChaosPlugin(context, {"group_ids": ["111"]})
    with caplog.at_level(logging.WARNING):
        run(plugin._announce_group("111", "【观众投票 #1】"))
    assert context.sent[0][0] == "aiocqhttp:GroupMessage:111"
    assert "发送消息失败" in caplog.text


def test_several_group_platforms_are_not_guessed_between(caplog):
    # 两个群聊平台时无从判断，宁可发到默认值上并告警，也不要静默发错群。
    context = PlatformAwareContext([("atri", "aiocqhttp"), ("tg", "telegram")])
    plugin = ThChaosPlugin(context, {"group_ids": ["111"]})
    with caplog.at_level(logging.WARNING):
        run(plugin._announce_group("111", "【观众投票 #1】"))
    assert context.sent[0][0] == "aiocqhttp:GroupMessage:111"
    assert "发送消息失败" in caplog.text


def test_group_umos_still_wins_over_auto_detection():
    # 多平台用户仍然可以手写；配置里写了就以配置为准。
    context = PlatformAwareContext([("atri", "aiocqhttp"), ("tg", "telegram")], routable=True)
    plugin = ThChaosPlugin(
        context,
        {"group_ids": ["111"], "group_umos": {"111": "tg:GroupMessage:111"}},
    )
    run(plugin._announce_group("111", "【观众投票 #1】"))
    assert context.sent == [("tg:GroupMessage:111", "【观众投票 #1】")]


def test_platform_lookup_falls_back_to_the_platform_insts_attribute():
    # get_insts() 是公开方法，但旧版或精简实现可能只有 platform_insts 字段。
    context = LegacyPlatformManagerContext([("atri", "aiocqhttp"), ("webchat", "webchat")])
    plugin = ThChaosPlugin(context, {"group_ids": ["111"]})
    assert plugin._loaded_platforms() == [("atri", "aiocqhttp"), ("webchat", "webchat")]
    assert plugin._group_platform_ids() == ["atri"]


def test_platform_hint_survives_a_context_without_platform_manager(caplog):
    # 离线测试与精简上下文里没有 platform_manager，不能因此抛异常。
    plugin = ThChaosPlugin(UnroutableContext(), {"group_ids": ["111"]})
    with caplog.at_level(logging.WARNING):
        run(plugin._announce_group("111", "【观众投票 #1】"))
    assert "没有已加载的平台适配器" in caplog.text


def test_initialize_is_quiet_when_a_single_group_platform_can_be_recognised(caplog):
    # 0.2.7 起这种情况插件自己认得出来，不该再报警要用户去填 group_umos。
    context = PlatformAwareContext([("atri", "aiocqhttp"), ("webchat", "webchat")])
    with caplog.at_level(logging.WARNING):
        _initialize_with_recorder({"token": "t", "group_ids": ["123456789"]}, context)
    assert "发不出去" not in caplog.text


def test_initialize_names_groups_when_several_group_platforms_are_ambiguous(caplog):
    # 真正无从判断时（多个群聊平台）才该在启动时说出来。
    context = PlatformAwareContext([("atri", "aiocqhttp"), ("tg", "telegram")])
    with caplog.at_level(logging.WARNING):
        _initialize_with_recorder({"token": "t", "group_ids": ["123456789"]}, context)
    assert "123456789" in caplog.text
    assert "atri" in caplog.text
    assert "group_umos" in caplog.text


def test_initialize_is_quiet_when_the_default_platform_is_loaded(caplog):
    context = PlatformAwareContext([("aiocqhttp", "aiocqhttp"), ("webchat", "webchat")])
    with caplog.at_level(logging.WARNING):
        _initialize_with_recorder({"token": "t", "group_ids": ["123456789"]}, context)
    assert "发不出去" not in caplog.text


def test_initialize_is_quiet_when_group_umos_matches_a_loaded_platform(caplog):
    context = PlatformAwareContext([("atri", "aiocqhttp"), ("tg", "telegram")])
    config = {
        "token": "t",
        "group_ids": ["123456789"],
        "group_umos": {"123456789": "atri:GroupMessage:123456789"},
    }
    with caplog.at_level(logging.WARNING):
        _initialize_with_recorder(config, context)
    assert "发不出去" not in caplog.text
