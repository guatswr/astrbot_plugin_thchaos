"""Replay backend event sequences and check audience message volume."""

import asyncio

from test_group_filter import make_plugin


OPTIONS = [{"choice": 1, "name": "强制低速", "event_key": "input.force_focus"}]
OPEN = {"round_id": 1, "options": OPTIONS, "remaining_ms": 10000}
CLOSE = {"round_id": 1, "reason": "winner", "winner_choices": [1], "winning_votes": 1, "total_votes": 1}


def test_normal_round_only_sends_open_and_named_result():
    async def scenario():
        plugin, _ = make_plugin(["123"])
        async def emit(kind, payload):
            await plugin._handle_backend_message({"type": kind, "payload": payload})
        await emit("vote.opened", OPEN)
        await emit("game.sync", {"active_vote": OPEN})
        for reason in ["paused", "resumed"] * 3:
            await emit("game.state_changed", {"phase": "voting", "reason": reason})
            await emit("vote.snapshot", {"round_id": 1, "options": [{"choice": 1, "votes": 1}]})
        await emit("vote.closed", CLOSE)
        await emit("effect.resolved", {"status": "applied", "event_key": "input.force_focus"})
        assert len(plugin.context.sent) == 2
        assert "1号·强制低速" in plugin.context.sent[-1][1]
        await emit("effect.resolved", {"status": "rejected", "event_key": "input.force_focus", "result_code": "rejected.conflict"})
        assert "异变没生效" in plugin.context.sent[-1][1]
        assert len(plugin.context.sent) == 3
    asyncio.run(scenario())


def test_offline_cancels_pending_snapshot_and_only_warns_once():
    async def scenario():
        plugin, _ = make_plugin(["123"])
        plugin._announce_snapshots = True
        plugin._snapshot_interval = 0
        await plugin._handle_backend_message({"type": "vote.snapshot", "payload": {"round_id": 7, "options": [{"choice": 1, "votes": 1}]}})
        for _ in range(2):
            await plugin._handle_backend_message({"type": "game.offline"})
        await asyncio.sleep(0)
        assert len(plugin.context.sent) == 1
        assert "投票中断" in plugin.context.sent[0][1]
        assert plugin._active_round is None
        assert plugin._latest_snapshot is None
    asyncio.run(scenario())


def test_optional_snapshots_skip_unchanged_counts_and_stale_rounds():
    async def scenario():
        plugin, _ = make_plugin(["123"])
        plugin._snapshot_interval = 0
        plugin._latest_snapshot = {"round_id": 7, "options": [{"choice": 1, "votes": 1}]}
        await plugin._flush_snapshot()
        await plugin._flush_snapshot()
        assert len(plugin.context.sent) == 1
        await plugin._handle_backend_message({"type": "vote.snapshot", "payload": {"round_id": 6}})
        assert plugin._latest_snapshot["round_id"] == 7
    asyncio.run(scenario())
