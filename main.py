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
    backend_url_problem,
    candidate_platform_ids,
    format_cast_error,
    format_effect,
    format_game_offline,
    format_game_state,
    format_snapshot,
    format_vote_ack,
    format_vote_closed,
    format_vote_opened,
    group_id_from_message,
    normalize_group_ids,
    option_name_entries,
    parse_vote_choice,
    pseudonymous_voter_id,
    resolve_umo,
    suspicious_group_ids,
    unroutable_groups,
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

    filter = _Filter()

    def register(*_args: Any, **_kwargs: Any):
        return lambda cls: cls

    class MessageChain:  # type: ignore[no-redef]
        """离线测试用的最小替身，只保留插件真正用到的 ``.message()``。"""

        def __init__(self) -> None:
            self._parts: list[str] = []

        def message(self, text: str) -> "MessageChain":
            self._parts.append(str(text))
            return self

        def __str__(self) -> str:
            return "".join(self._parts)


CLIENT_VERSION = "0.2.8"


@register("thchaos", "Taropoi", "THChaos 游戏观众投票桥接", CLIENT_VERSION)
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
        self._snapshot_interval = max(0.5, min(float(self._config.get("snapshot_interval_seconds", 10)), 30.0))
        self._announce_ack = bool(self._config.get("announce_vote_ack", False))
        self._announce_snapshots = bool(self._config.get("announce_vote_snapshot", False))
        self._announce_states = bool(self._config.get("announce_game_state", False))
        self._announce_effects = bool(self._config.get("announce_effect_success", False))
        self._last_opened: tuple[Any, Any] | None = None
        self._last_snapshot_text: str | None = None
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
        self._effect_names: dict[str, str] = {}
        self._cast_groups: dict[str, str] = {}
        self._reported_foreign_groups: set[str] = set()

    async def initialize(self) -> None:
        """插件每次被激活/重载时都会走到这里（与 terminate 配对）。

        **不要改用 ``@filter.on_astrbot_loaded()``。** 那个钩子只在 AstrBot 进程
        启动时触发一次（``core_lifecycle.start()`` 里那一处调用），面板里重载插件、
        保存插件配置都只走 ``plugin_manager.reload()``，不会再触发它。在那种钩子里
        建立连接的结果是：重载之后插件照常收消息、照常处理群消息，但从不连接后端，
        播报一条也发不出去——而且是连启动日志都不打的那种安静。
        """

        self._report_group_config()
        self._report_unroutable_groups()
        problem = backend_url_problem(self._backend_url)
        if problem:
            astr_logger.warning(f"THChaos backend_url 写错了（{problem}）：{self._backend_url}")
        if not self._hmac_secret:
            astr_logger.warning("THChaos voter_hmac_secret 未配置，拒绝接收 QQ 投票")
        if not self._token:
            astr_logger.error("THChaos backend token 未配置，插件不会连接后端")
            return
        if self._network_task and not self._network_task.done():
            return  # 同一个实例被重复初始化时，不要开出第二条连接
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

    def _report_unroutable_groups(self) -> None:
        """启动时就把「播报注定发不出去」的配置挑出来。

        运行时的告警只在真有播报的那一刻出现；没有投票时这种配置错误是完全
        静音的——插件连得上后端、白名单也对，等到开播才发现一条都发不出去。
        """

        platform_ids = self._loaded_platform_ids()
        problems = unroutable_groups(
            self._groups, self._umos, platform_ids, self._group_platform_ids()
        )
        if not problems:
            return
        groups = "、".join(group_id for group_id, _ in problems)
        example = "，".join(
            f'"{group_id}": "<平台ID>:GroupMessage:{group_id}"' for group_id, _ in problems
        )
        astr_logger.warning(
            f"THChaos 群 {groups} 的播报发不出去：它们用的会话不属于任何已加载的平台"
            f"（当前已加载的平台 ID 有：{', '.join(platform_ids)}）。"
            f"把该群真实的 UMO 填进 group_umos 即可，例如 {{{example}}}；"
            "或者直接在群里发一条消息——插件收到白名单群消息后会记住它真实的 UMO。"
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
                    self._cancel_snapshot()
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
        # effect.resolved 只带 event_key 不带显示名，而候选项里两个都有。
        # 每条消息路过时顺手把见过的名字攒下来，"异变生效"那条播报就不必
        # 依赖插件里手抄的那份表。
        self._effect_names.update(option_name_entries(payload))
        if message_type == "authenticated":
            self._game_instance_id = payload.get("game_instance_id") or self._game_instance_id
            # 连上后端本身是要专门说一声的：插件以前只在失败时打日志，
            # 「究竟连上没有」只能靠后端那边翻 /ws/bot 的记录。
            astr_logger.info(
                f"THChaos 已连接后端 {self._backend_url}（房间 {self._room_id}，"
                f"游戏端{'在线' if self._game_instance_id else '离线'}）"
            )
        elif message_type == "game.sync":
            self._cancel_snapshot()
            self._game_instance_id = envelope.get("game_instance_id") or self._game_instance_id
            active = payload.get("active_vote")
            self._active_round = active.get("round_id") if active else None
            self._active_options = active.get("options", []) if active else []
            self._latest_snapshot = payload.get("latest_snapshot")
            if active:
                await self._announce_opened(active)
                if self._latest_snapshot:
                    self._schedule_snapshot_announcement()
        elif message_type == "vote.opened":
            self._cancel_snapshot()
            self._game_instance_id = envelope.get("game_instance_id") or self._game_instance_id
            self._active_round = payload.get("round_id")
            self._active_options = payload.get("options", [])
            self._latest_snapshot = None
            await self._announce_opened(payload)
        elif message_type == "vote.snapshot":
            if payload.get("round_id") != self._active_round or self._active_round is None:
                return
            self._latest_snapshot = payload
            self._schedule_snapshot_announcement()
        elif message_type == "vote.ack":
            group_id = self._cast_groups.pop(str(payload.get("cast_id", "")), "")
            if self._announce_ack and group_id:
                await self._announce_group(group_id, format_vote_ack(payload))
        elif message_type == "vote.closed":
            self._cancel_snapshot()
            text = format_vote_closed(payload, self._active_options)
            self._active_round = None
            self._active_options = []
            self._latest_snapshot = None
            await self._announce_all(text)
        elif message_type == "effect.resolved":
            if self._announce_effects or payload.get("status") != "applied":
                await self._announce_all(format_effect(payload, (self._effect_names,)))
        elif message_type == "game.state_changed":
            if self._announce_states:
                await self._announce_all(format_game_state(payload))
        elif message_type == "game.offline":
            was_voting = self._active_round is not None
            self._cancel_snapshot()
            self._game_instance_id = None
            self._active_round = None
            self._active_options = []
            self._latest_snapshot = None
            if was_voting or self._announce_states:
                await self._announce_all(format_game_offline())
        elif message_type == "heartbeat.ping":
            await self._send("heartbeat.pong", {"nonce": payload.get("nonce", "heartbeat")})
        elif message_type == "error":
            astr_logger.warning(f"THChaos backend error: {payload}")
            if payload.get("cast_id"):
                group_id = self._cast_groups.pop(str(payload["cast_id"]), "")
                if self._announce_ack and group_id:
                    await self._announce_group(group_id, format_cast_error(payload))

    async def _announce_opened(self, payload: dict[str, Any]) -> None:
        key = (self._game_instance_id, payload.get("round_id"))
        if key != self._last_opened:
            self._last_opened = key
            self._last_snapshot_text = None
            await self._announce_all(format_vote_opened(payload))

    def _cancel_snapshot(self) -> None:
        if self._snapshot_task:
            self._snapshot_task.cancel()
            self._snapshot_task = None
        self._last_snapshot_text = None

    def _schedule_snapshot_announcement(self) -> None:
        if not self._announce_snapshots:
            return
        if self._snapshot_task and not self._snapshot_task.done():
            return
        self._snapshot_task = asyncio.create_task(self._flush_snapshot(), name="thchaos-snapshot-announcement")

    async def _flush_snapshot(self) -> None:
        await asyncio.sleep(self._snapshot_interval)
        if self._latest_snapshot:
            text = format_snapshot(self._latest_snapshot)
            if text != self._last_snapshot_text:
                self._last_snapshot_text = text
                await self._announce_all(text)

    async def _announce_all(self, text: str) -> None:
        for group_id in sorted(self._groups):
            await self._announce_group(group_id, text)

    async def _announce_group(self, group_id: str, text: str) -> None:
        umo = self._umo_for(group_id)
        try:
            sent = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            astr_logger.warning(f"THChaos 向群 {group_id} 发送消息失败：{exc}")
            return
        # Context.send_message 找不到匹配的平台时**不抛异常**，只返回 False，
        # 消息被直接丢弃。不检查返回值的话，「后端连上了、群里却一片安静」
        # 就没有任何线索——排查时会一路怀疑到网络上去。
        if sent is False:
            astr_logger.warning(
                f"THChaos 向群 {group_id} 发送消息失败：没有平台匹配会话 {umo}。"
                f"{self._platform_id_hint()}"
                "插件挑会话的顺序是：记住群里来过的真实会话 → 在已加载的平台里"
                "认出唯一的群聊平台 → 退回默认的 aiocqhttp:GroupMessage:<群号>。"
                "走到最后一步说明前两步都没成，通常是因为同时装着多个群聊平台、"
                "无从判断。两种修法都立刻见效：群里随便发一条消息（插件会记住"
                "那个群真实的会话），或者在 group_umos 里写死，例如 "
                f'{{"{group_id}": "<上面的平台ID>:GroupMessage:{group_id}"}}。'
            )

    def _umo_for(self, group_id: str) -> str:
        """给这个群挑一个会话标识（详见 ``logic.resolve_umo`` 的说明）。"""

        umo, _ = resolve_umo(group_id, self._umos, self._group_platform_ids())
        return umo

    def _loaded_platforms(self) -> list[tuple[str, str]]:
        """列出当前已加载的平台，每项是 ``(平台ID, 适配器类型)``。

        这两个字段不是一回事，混用就会掉进"平台 ID 猜错、消息被静默丢弃"的坑：
        ``meta().id`` 是**用户在面板里给这个平台起的名字**（默认 ``aiocqhttp``，
        改过就变成 ``atri`` 这类），``meta().name`` 是**适配器类型**，写死在
        AstrBot 代码里（``aiocqhttp``、``webchat``、``telegram``…）。
        用户改名字改不动类型，所以"这个平台会不会有群"只能看类型。

        AstrBot 核心自己也这么判断：``core/star/context.py`` 里用
        ``platform.meta().name != "webchat"`` 区分真实群聊平台和自带的 WebUI。
        """

        # get_insts() 是公开方法（第三方插件普遍用它）；platform_insts 是它背后
        # 的字段，留着兜底，免得某个版本上没有前者。
        try:
            manager = self.context.platform_manager
        except AttributeError:
            return []  # 精简 Context（离线测试）里没有 platform_manager
        get_insts = getattr(manager, "get_insts", None)
        insts = get_insts() if callable(get_insts) else getattr(manager, "platform_insts", [])
        found: list[tuple[str, str]] = []
        for platform in insts:
            # 这里只兜住"某一个平台读不出 meta"，不兜住外面整段：
            # 把整个循环包进 suppress 会让真正的编程错误也变成"没有平台"，
            # 而"没有平台"又安静地退回默认 UMO——正是这个插件一路在修的那种
            # 无声故障。
            with suppress(Exception):
                meta = platform.meta()
                platform_id = str(getattr(meta, "id", "") or "")
                adapter = str(getattr(meta, "name", "") or "")
                if platform_id and (platform_id, adapter) not in found:
                    found.append((platform_id, adapter))
        return found

    def _loaded_platform_ids(self) -> list[str]:
        return [platform_id for platform_id, _ in self._loaded_platforms()]

    def _group_platform_ids(self) -> list[str]:
        """可能承载群消息的平台 ID——排掉 WebUI 这类不可能是 QQ 群的适配器。"""

        return candidate_platform_ids(self._loaded_platforms())

    def _platform_id_hint(self) -> str:
        ids = self._loaded_platform_ids()
        if not ids:
            return "当前没有已加载的平台适配器。"
        return f"当前已加载的平台 ID 有：{', '.join(ids)}。"

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
