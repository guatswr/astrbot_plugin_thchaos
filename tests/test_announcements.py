"""Replay backend event sequences and check audience message volume."""

import asyncio

from test_group_filter import make_plugin


OPTIONS = [{"choice": 1, "name": "强制低速", "event_key": "input.force_focus"}]
OPEN = {"round_id": 1, "options": OPTIONS, "remaining_ms": 10000}
async def drain(plugin):
    await asyncio.gather(*(queue.join() for queue in plugin._delivery_queues.values()))


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
        await drain(plugin)
        await emit("vote.closed", CLOSE)
        await emit("effect.resolved", {"status": "applied", "event_key": "input.force_focus"})
        await drain(plugin)
        assert len(plugin.context.sent) == 2
        assert "1号·强制低速" in plugin.context.sent[-1][1]
        await emit("effect.resolved", {"status": "rejected", "event_key": "input.force_focus", "result_code": "rejected.conflict"})
        await drain(plugin)
        assert "异变没生效" in plugin.context.sent[-1][1]
        await drain(plugin)
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
        await drain(plugin)
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
        await drain(plugin)
        assert len(plugin.context.sent) == 1
        await plugin._handle_backend_message({"type": "vote.snapshot", "payload": {"round_id": 6}})
        assert plugin._latest_snapshot["round_id"] == 7
    asyncio.run(scenario())


def test_slow_group_does_not_block_other_groups_or_backend():
    async def scenario():
        plugin, casts = make_plugin(["111", "222"])
        plugin._delivery_timeout = 0.02
        started = asyncio.Event()
        async def send(umo, chain):
            if "111" in umo:
                started.set()
                await asyncio.Event().wait()
            plugin.context.sent.append((umo, str(chain)))
        plugin.context.send_message = send
        await plugin._handle_backend_message({"type": "vote.opened", "payload": OPEN})
        await started.wait()
        await plugin._delivery_queues["222"].join()
        assert "异变投票" in plugin.context.sent[0][1]
        await plugin._handle_backend_message({"type": "heartbeat.ping", "payload": {"nonce": "n"}})
        assert casts[-1]["type"] == "heartbeat.pong"
        await plugin._handle_backend_message({"type": "vote.closed", "payload": CLOSE})
        assert plugin._active_round is None
        await drain(plugin)
        assert "投票结果" in plugin.context.sent[-1][1]
    asyncio.run(scenario())


def test_only_failed_group_retries_and_sync_does_not_duplicate_success():
    async def scenario():
        plugin, _ = make_plugin(["111", "222"])
        plugin._retry_delay = 0
        attempts = {"111": 0, "222": 0}
        async def send(umo, chain):
            group = umo.rsplit(":", 1)[-1]
            attempts[group] += 1
            return group == "111" or attempts[group] > 3
        plugin.context.send_message = send
        await plugin._handle_backend_message({"type": "vote.opened", "payload": OPEN})
        await drain(plugin)
        assert attempts == {"111": 1, "222": 3}
        await plugin._handle_backend_message({"type": "game.sync", "payload": {"active_vote": OPEN}})
        await drain(plugin)
        assert attempts == {"111": 1, "222": 4}
    asyncio.run(scenario())


def test_queued_opening_is_dropped_after_close_or_expiry():
    async def scenario():
        for close in (True, False):
            plugin, _ = make_plugin(["123"])
            release = asyncio.Event()
            started = asyncio.Event()
            async def send(umo, chain):
                if str(chain) == "earlier":
                    started.set()
                    await release.wait()
                plugin.context.sent.append((umo, str(chain)))
            plugin.context.send_message = send
            await plugin._announce_all("earlier")
            await started.wait()
            await plugin._handle_backend_message({"type": "vote.opened", "payload": OPEN})
            if close:
                await plugin._handle_backend_message({"type": "vote.closed", "payload": CLOSE})
            else:
                plugin._opening["deadline"] = asyncio.get_running_loop().time() - 1
            release.set()
            await drain(plugin)
            assert not any("异变投票" in text for _, text in plugin.context.sent)
    asyncio.run(scenario())


def test_backend_disconnect_warns_once_and_sync_restores_current_round():
    async def scenario():
        for active in (OPEN, dict(OPEN, round_id=2), None):
            plugin, _ = make_plugin(["123"])
            await plugin._handle_backend_message({"type": "vote.opened", "payload": OPEN})
            await drain(plugin)
            await plugin._backend_disconnected()
            await plugin._backend_disconnected()
            assert plugin._active_round is None
            assert plugin._game_instance_id is None
            await drain(plugin)
            assert sum("投票中断" in text for _, text in plugin.context.sent) == 1
            await plugin._handle_backend_message({"type": "game.sync", "game_instance_id": "g-1", "payload": {"active_vote": active}})
            await drain(plugin)
            assert plugin._active_round == (active["round_id"] if active else None)
            assert not plugin._backend_interrupted
            assert len(plugin.context.sent) == 3
            assert ("异变投票" if active else "当前无投票") in plugin.context.sent[-1][1]
    asyncio.run(scenario())


def test_terminate_cancels_delivery_and_does_not_announce_disconnect():
    async def scenario():
        plugin, _ = make_plugin(["123"])
        started = asyncio.Event()
        async def send(umo, chain):
            started.set()
            await asyncio.Event().wait()
        plugin.context.send_message = send
        await plugin._announce_all("test")
        await started.wait()
        await plugin.terminate()
        await plugin._backend_disconnected()
        assert all(task.done() for task in plugin._delivery_tasks.values())
        assert not plugin._backend_interrupted
    asyncio.run(scenario())
