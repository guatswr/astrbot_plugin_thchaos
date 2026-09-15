"""白名单行为测试：只有 group_ids 里的群，投票才会被转发出去。

这些用例走的是插件真实的 on_message 路径，只把发包换成记录器，不碰网络。
main.py 依赖 astrbot 包，离线时它自带 shim；装了 AstrBot 的环境跳过本文件，
那种情况下应该在真实实例上验证。
"""

from __future__ import annotations

import asyncio
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
