"""THChaos AstrBot 插件。

把 THChaos 游戏端的观众投票同步到 QQ 群，并把群友回复的 1/2/3 转发给游戏端。
插件只做平台适配：不计算票数、不开奖、不保存投票记录，所有权威状态都来自
后端转发的游戏端消息；网络协程断线后按指数退避重连。

参与投票的群由配置项 ``group_ids`` **静态指定**（见 README），其他群的消息
一律忽略，这样同一个 QQ 账号上的无关群不会被误计入投票。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .logic import (
    default_umo,
    format_effect,
    format_snapshot,
    format_vote_closed,
    format_vote_opened,
    group_id_from_message,
    normalize_group_ids,
    parse_vote_choice,
    pseudonymous_voter_id,
    suspicious_group_ids,
)

try:  # AstrBot 运行时提供；纯函数测试不需要安装 AstrBot。
    from astrbot.api import logger as astr_logger
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.star import Context, Star, register
except ImportError:  # pragma: no cover - 仅允许离线语法/纯函数测试导入
    astr_logger = logging.getLogger(__name__)

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any) -> None:
            self.context = context

    class Context:  # type: ignore[no-redef]
        pass

    class AstrMessageEvent:  # type: ignore[no-redef]
        pass

    class _Filter:  # type: ignore[no-redef]
        class EventMessageType:
            ALL = "all"

        def event_message_type(self, _value: Any):
            return lambda fn: fn

        def on_astrbot_loaded(self):
            return lambda fn: fn

    filter = _Filter()
    MessageChain = Any  # type: ignore[misc,assignment]

    def register(*_args: Any, **_kwargs: Any):
        return lambda cls: cls


CLIENT_VERSION = "0.2.0"


@register("thchaos", "THChaos", "THChaos 游戏观众投票桥接", CLIENT_VERSION)
class ThChaosPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        raw = self.config
        self._config = raw if isinstance(raw, dict) else {}
        self._backend_url = str(self._config.get("backend_url", "ws://127.0.0.1:8765/ws/bot"))
        self._token = str(self._config.get("token", ""))
        self._room_id = str(self._config.get("room_id", "main"))
        self._groups = normalize_group_ids(self._config.get("group_ids", []))
        self._umos = {str(k): str(v) for k, v in (self._config.get("group_umos", {}) or {}).items()}
        self._hmac_secret = str(self._config.get("voter_hmac_secret", ""))
        self._snapshot_interval = max(0.5, min(float(self._config.get("snapshot_interval_seconds", 2)), 30.0))
        self._announce_ack = bool(self._config.get("announce_vote_ack", False))
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._network_task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._seq = 0
        self._game_instance_id: str | None = None
        self._active_round: int | None = None
        self._active_options: list[dict[str, Any]] = []
        self._latest_snapshot: dict[str, Any] | None = None
        self._cast_groups: dict[str, str] = {}
        self._reported_foreign_groups: set[str] = set()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self._report_group_config()
        if not self._hmac_secret:
            astr_logger.warning("THChaos voter_hmac_secret 未配置，拒绝接收 QQ 投票")
        if not self._token:
            astr_logger.error("THChaos backend token 未配置，插件不会连接后端")
            return
        self._network_task = asyncio.create_task(self._run_network(), name="thchaos-backend-ws")

    def _report_group_config(self) -> None:
        """启动时把白名单状态说清楚，避免「配置了但没生效」无声无息。"""

        if not self._groups:
            astr_logger.warning(
                "THChaos 未配置任何参与投票的群（group_ids 为空），插件不会响应任何群消息。"
            )
            return
        astr_logger.info(f"THChaos 参与投票的群共 {len(self._groups)} 个：{', '.join(sorted(self._groups))}")
        odd = suspicious_group_ids(self._groups)
        if odd:
            astr_logger.warning(
                f"THChaos group_ids 里有不像 QQ 群号的条目：{', '.join(odd)}。"
                "群号是纯数字，若填成了 UMO 或群名将永远不会匹配。"
            )

    async def terminate(self) -> None:
        if self._snapshot_task:
            self._snapshot_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._snapshot_task
        if self._network_task:
            self._network_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._network_task
        if self._session:
            await self._session.close()

    # --- 与后端的连接 -------------------------------------------------------

    async def _run_network(self) -> None:
        delay = 1.0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            while True:
                try:
                    async with self._session.ws_connect(self._backend_url, heartbeat=20) as ws:
                        self._ws = ws
                        delay = 1.0
                        self._seq = 0
                        await self._send_hello()
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_backend_message(json.loads(message.data))
                            elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED}:
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    astr_logger.warning(f"THChaos backend 连接失败：{exc}")
                finally:
                    self._ws = None
                    self._game_instance_id = None
                    self._active_round = None
                    self._active_options = []
                    self._latest_snapshot = None
                    # Casts that were waiting for an ACK belong to the old
                    # WebSocket session. Do not retain their group mapping
                    # forever when a reconnect races with the game response.
                    self._cast_groups.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        finally:
            self._ws = None
            await self._session.close()
            self._session = None

    async def _send_hello(self) -> None:
        await self._send(
            "hello",
            {
                "role": "bot",
                "client_id": "astrbot-plugin-thchaos",
                "client_version": CLIENT_VERSION,
                "token": self._token,
                "room_id": self._room_id,
                "game_instance_id": None,
            },
            include_instance=False,
        )

    async def _send(self, message_type: str, payload: dict[str, Any], *, include_instance: bool = True) -> None:
        if self._ws is None:
            raise ConnectionError("backend WebSocket 未连接")
        async with self._send_lock:
            self._seq += 1
            envelope = {
                "version": 1,
                "type": message_type,
                "message_id": str(uuid.uuid4()),
                "room_id": self._room_id,
                "game_instance_id": self._game_instance_id if include_instance else None,
                "seq": self._seq,
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "payload": payload,
            }
            await self._ws.send_str(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))

    # --- 后端 → 群 ----------------------------------------------------------

    async def _handle_backend_message(self, envelope: dict[str, Any]) -> None:
        message_type = envelope.get("type")
        payload = envelope.get("payload") or {}
        if message_type == "authenticated":
            self._game_instance_id = payload.get("game_instance_id") or self._game_instance_id
        elif message_type == "game.sync":
            self._game_instance_id = envelope.get("game_instance_id") or self._game_instance_id
            active = payload.get("active_vote")
            self._active_round = active.get("round_id") if active else None
            self._active_options = active.get("options", []) if active else []
            self._latest_snapshot = payload.get("latest_snapshot")
            if active:
                await self._announce_all(format_vote_opened(active))
                if self._latest_snapshot:
                    self._schedule_snapshot_announcement()
        elif message_type == "vote.opened":
            self._game_instance_id = envelope.get("game_instance_id") or self._game_instance_id
            self._active_round = payload.get("round_id")
            self._active_options = payload.get("options", [])
            self._latest_snapshot = None
            await self._announce_all(format_vote_opened(payload))
        elif message_type == "vote.snapshot":
            self._latest_snapshot = payload
            self._schedule_snapshot_announcement()
        elif message_type == "vote.ack":
            group_id = self._cast_groups.pop(str(payload.get("cast_id", "")), "")
            if self._announce_ack:
                if group_id:
                    status = "已计票" if payload.get("counted") else f"未计票（{payload.get('reason', 'rejected')}）"
                    await self._announce_group(group_id, f"【投票】{status}：选项 {payload.get('choice', '?')}。")
        elif message_type == "vote.closed":
            self._active_round = None
            self._active_options = []
            self._latest_snapshot = None
            await self._announce_all(format_vote_closed(payload))
        elif message_type == "effect.resolved":
            await self._announce_all(format_effect(payload))
        elif message_type == "game.state_changed":
            reason = payload.get("reason", "state_changed")
            phase = payload.get("phase", "unknown")
            await self._announce_all(f"【游戏状态】{phase}（{reason}）")
        elif message_type == "game.offline":
            self._active_round = None
            self._active_options = []
            await self._announce_all("【THChaos】游戏端已断开，暂时无法投票。")
        elif message_type == "heartbeat.ping":
            await self._send("heartbeat.pong", {"nonce": payload.get("nonce", "heartbeat")})
        elif message_type == "error":
            astr_logger.warning(f"THChaos backend error: {payload}")
            if payload.get("cast_id"):
                group_id = self._cast_groups.pop(str(payload["cast_id"]), "")
                if self._announce_ack and group_id:
                    await self._announce_group(group_id, f"【投票】提交失败：{payload.get('message', 'backend error')}。")

    def _schedule_snapshot_announcement(self) -> None:
        if self._snapshot_task and not self._snapshot_task.done():
            return
        self._snapshot_task = asyncio.create_task(self._flush_snapshot(), name="thchaos-snapshot-announcement")

    async def _flush_snapshot(self) -> None:
        await asyncio.sleep(self._snapshot_interval)
        if self._latest_snapshot:
            await self._announce_all(format_snapshot(self._latest_snapshot))

    async def _announce_all(self, text: str) -> None:
        for group_id in sorted(self._groups):
            await self._announce_group(group_id, text)

    async def _announce_group(self, group_id: str, text: str) -> None:
        umo = self._umos.get(group_id) or default_umo(group_id)
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            astr_logger.warning(f"THChaos 向群 {group_id} 发送消息失败：{exc}")

    # --- 群 → 后端 ----------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """接收所有消息，但只处理白名单群里的纯文本 1/2/3。"""

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            raw = {}
        fallback: Any = None  # 私聊等场景 get_group_id() 会抛异常
        with suppress(Exception):
            fallback = event.get_group_id()
        group_id = group_id_from_message(raw, fallback)
        if not group_id:
            return

        if group_id not in self._groups:
            self._report_foreign_group(group_id)
            return

        # 记住这个群真实的 unified_msg_origin：跨平台或同账号多连接时，
        # 拼出来的默认 UMO 可能不对，用它发播报才稳。
        with suppress(Exception):
            umo = event.unified_msg_origin
            if umo:
                self._umos[group_id] = str(umo)

        choice = parse_vote_choice(str(getattr(event, "message_str", "")))
        if choice is None or self._active_round is None or not self._game_instance_id or not self._hmac_secret:
            return
        sender_id = str(event.get_sender_id())
        source_message_id = str(raw.get("message_id", getattr(event, "message_id", uuid.uuid4().hex)))
        payload = {
            "cast_id": str(uuid.uuid4()),
            "round_id": self._active_round,
            "choice": choice,
            "voter_id": pseudonymous_voter_id(self._hmac_secret, sender_id),
            "source_group_id": group_id,
            "source_message_id": source_message_id,
        }
        self._cast_groups[payload["cast_id"]] = group_id
        try:
            await self._send("vote.cast", payload)
        except Exception as exc:
            self._cast_groups.pop(payload["cast_id"], None)
            astr_logger.debug(f"THChaos 投票发送失败：{exc}")

    def _report_foreign_group(self, group_id: str) -> None:
        """白名单之外的群报一次日志，方便把群号抄进配置。

        每个群只报一次，不会刷屏；这是拿到"该填哪个群号"最省事的办法。
        """

        if group_id in self._reported_foreign_groups:
            return
        self._reported_foreign_groups.add(group_id)
        astr_logger.info(
            f"THChaos 收到不在 group_ids 里的群消息（群号 {group_id}），已忽略；"
            "若要让该群参与投票，把群号加进插件配置的 group_ids。"
        )
