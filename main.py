"""
群聊连续消息防抖插件 v2.3.2-debug

目标：
- 不修改 Heartflow 插件。
- 在 GROUP_MESSAGE 阶段抢在 Heartflow 前面运行。
- 防抖粒度从“群 session”改为“群 session + sender_id”。
- 只合并同一个人在短时间内连续发送的碎片消息。
- 不同群员的消息互不合并、互不取消、互不覆盖，避免把回复目标带偏。
- 合并多条消息时保留 sender 身份，并注入到当前 sender 的最后一条事件。
- 保留群聊复读功能；复读仍然按群维度判断，因为复读本来就需要跨用户检测。

注意：
- 这是“发送者级碎片消息合并器”，不是群级总闸门。
- 外置插件无法精确知道 Heartflow 最后是否达到回复阈值，只能在进入后续链路前减少同一人的碎片触发。
- 复读功能不走 LLM，触发后直接发送原文，并阻止该事件继续进入后续 LLM 回复链路。
"""

import asyncio
import random
import re
import time
from typing import Dict, List, Optional, Set

from pydantic import BaseModel, Field

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain


_URL_RE = re.compile(r"(?:https?://|www\.|[A-Za-z0-9._%+-]+\.[A-Za-z]{2,})(?:\S*)", re.I)


class DebouncerConfig(BaseModel):
    """防抖插件配置模型"""

    enabled: bool = Field(default=True, description="是否启用插件")

    window_seconds: float = Field(
        default=3.0,
        description="普通防抖等待窗口，单位秒",
        ge=0.3,
        le=30.0,
    )

    direct_trigger_window_seconds: float = Field(
        default=1.5,
        description="@Bot 或疑似直接呼唤 Bot 时的较短防抖窗口，单位秒",
        ge=0.1,
        le=30.0,
    )

    reset_timer: bool = Field(
        default=True,
        description="同一发送者收到新消息时是否重置防抖计时",
    )

    max_messages: int = Field(
        default=5,
        description="同一发送者最大合并条数，达到后立即放行最后一条事件",
        ge=1,
        le=20,
    )

    first_message_no_debounce: bool = Field(
        default=False,
        description="冷场后首条群消息是否不防抖，直接放行",
    )

    idle_reset_seconds: int = Field(
        default=120,
        description="冷场判定时间，单位秒",
        ge=10,
        le=3600,
    )

    merge_messages: bool = Field(
        default=True,
        description="是否把同一发送者防抖窗口内文本合并后注入最后一条事件",
    )

    include_sender_in_merged_text: bool = Field(
        default=True,
        description="合并多条消息时是否在注入文本中保留发送者身份",
    )

    bot_aliases: str = Field(
        default="anon,Anon,ANON,爱音,小爱音",
        description="用于判断直接呼唤 Bot 的别名，逗号分隔",
    )

    bypass_command_prefixes: str = Field(
        default="/,!,！,#,＃,.,。",
        description="这些前缀开头的消息直接放行，逗号分隔",
    )

    buffer_expire_seconds: int = Field(
        default=300,
        description="缓冲区最大保留时间，单位秒",
        ge=30,
        le=3600,
    )

    # ===== 复读功能配置 =====
    repeat_enabled: bool = Field(default=True, description="是否启用群聊复读功能")

    repeat_mode: str = Field(
        default="chain",
        description="复读模式：chain=群友复读后跟读，random=低概率随机复读，both=两种都启用",
    )

    repeat_min_count: int = Field(
        default=2,
        description="连续多少个不同用户发送同一句后触发跟读判定",
        ge=2,
        le=10,
    )

    repeat_probability: float = Field(
        default=0.65,
        description="chain 模式复读概率，0.0~1.0",
        ge=0.0,
        le=1.0,
    )

    repeat_cooldown_seconds: int = Field(
        default=90,
        description="同一群 chain 复读冷却时间，单位秒",
        ge=0,
        le=3600,
    )

    repeat_min_text_length: int = Field(
        default=2,
        description="允许复读的最短文本长度",
        ge=1,
        le=100,
    )

    repeat_max_text_length: int = Field(
        default=24,
        description="允许复读的最长文本长度",
        ge=1,
        le=500,
    )

    repeat_random_probability: float = Field(
        default=0.03,
        description="random 模式随机复读概率，0.0~1.0",
        ge=0.0,
        le=1.0,
    )

    repeat_random_cooldown_seconds: int = Field(
        default=180,
        description="同一群 random 复读冷却时间，单位秒",
        ge=0,
        le=7200,
    )

    repeat_ignore_commands: bool = Field(default=True, description="复读功能是否忽略命令/前缀消息")
    repeat_ignore_at_bot: bool = Field(default=True, description="复读功能是否忽略 @Bot 的消息")
    repeat_ignore_urls: bool = Field(default=True, description="复读功能是否忽略包含 URL/域名的消息")
    repeat_text_only: bool = Field(default=True, description="复读功能是否只处理纯文本消息")
    repeat_ignore_self_message: bool = Field(default=True, description="复读功能是否忽略 Bot 自己发出的消息，防止自激循环")

    repeat_repeated_text_ttl_seconds: int = Field(
        default=600,
        description="同一句话被 Bot 复读后，多少秒内不再复读",
        ge=0,
        le=86400,
    )

    repeat_enabled_groups: str = Field(
        default="",
        description="仅在哪些群启用复读，逗号分隔；为空表示所有群",
    )

    repeat_disabled_groups: str = Field(
        default="",
        description="禁用复读的群号，逗号分隔",
    )

    repeat_debug: bool = Field(default=False, description="是否输出复读判定调试日志")

    debug_enabled: bool = Field(
        default=False,
        description="是否输出防抖诊断日志。开启后会打印事件进入、取消、放行、注入、result、stop 状态等细节",
    )

    debug_include_message_text: bool = Field(
        default=True,
        description="诊断日志是否包含消息文本预览。关闭后只打印长度和状态，避免日志过长或泄露内容",
    )

    debug_preview_chars: int = Field(
        default=180,
        description="诊断日志中文本预览最大字符数",
        ge=20,
        le=2000,
    )


class MessageBuffer:
    def __init__(self, session_id: str, sender_id: str, sender_name: str):
        self.session_id = session_id
        self.sender_id = sender_id
        self.sender_name = sender_name or sender_id
        self.messages: List[str] = []
        self.components: List[object] = []
        self.last_update: float = 0.0
        self.target_time: float = 0.0
        self.direct_trigger: bool = False

    def add(self, text: str, comps: list, expire_sec: int, sender_name: str, direct_trigger: bool):
        now = time.time()

        if self.last_update > 0 and (now - self.last_update) > expire_sec:
            self.clear()

        if sender_name:
            self.sender_name = sender_name

        self.messages.append(text)
        self.direct_trigger = self.direct_trigger or direct_trigger

        # 收集非纯文本组件，但默认只把文本合并注入。
        for c in comps:
            if not isinstance(c, Plain):
                self.components.append(c)

        self.last_update = now

    def clear(self):
        self.messages.clear()
        self.components.clear()
        self.last_update = 0.0
        self.target_time = 0.0
        self.direct_trigger = False

    def get_full_text(self, include_sender: bool = True) -> str:
        valid_messages = [m for m in self.messages if m and m.strip()]
        if not valid_messages:
            return ""

        # 单条消息保持原文，避免给 Heartflow / LLM 注入额外格式噪声。
        if len(valid_messages) == 1:
            return valid_messages[0]

        if not include_sender:
            return "\n".join(valid_messages)

        sender_label = f"{self.sender_name}({self.sender_id})"
        lines = [f"[同一用户连续消息合并，共 {len(valid_messages)} 条]", f"{sender_label}:"]
        lines.extend(valid_messages)
        return "\n".join(lines)

    def get_components(self) -> list:
        return list(self.components)


class RepeatState:
    def __init__(self):
        self.last_text: str = ""
        self.last_sender_id: str = ""
        self.repeat_count: int = 0
        self.last_chain_repeat_at: float = 0.0
        self.last_random_repeat_at: float = 0.0
        self.repeated_texts: Dict[str, float] = {}

    def cleanup(self, ttl_seconds: int):
        if ttl_seconds <= 0:
            return
        now = time.time()
        expired = [text for text, ts in self.repeated_texts.items() if now - ts > ttl_seconds]
        for text in expired:
            self.repeated_texts.pop(text, None)


class GroupDebouncer(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = DebouncerConfig(**(config or {}))

        # 发送者级：key = session_id + sender_id。
        self.buffers: Dict[str, MessageBuffer] = {}
        self.counters: Dict[str, int] = {}
        self.locks: Dict[str, asyncio.Lock] = {}

        # 群级状态：冷场和复读必须按群维度计算。
        self.last_group_message_time: Dict[str, float] = {}
        self.repeat_states: Dict[str, RepeatState] = {}
        self.repeat_locks: Dict[str, asyncio.Lock] = {}

        logger.info(
            f"[GroupDebouncer] V2.3.2-debug 初始化完成，模式=GROUP_MESSAGE 发送者级防抖，配置: {self.config}"
        )

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        session_id = getattr(msg_obj, "session_id", None)
        return str(session_id or event.unified_msg_origin or "unknown_session")

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        for attr in ("get_sender_id", "get_sender_uid", "get_user_id"):
            func = getattr(event, attr, None)
            if callable(func):
                try:
                    value = func()
                    if value is not None:
                        return str(value)
                except Exception:
                    pass

        msg_obj = getattr(event, "message_obj", None)
        for attr in ("sender_id", "user_id", "sender"):
            value = getattr(msg_obj, attr, None)
            if value is None:
                continue
            if isinstance(value, (str, int)):
                return str(value)
            for nested_attr in ("user_id", "id", "uin", "qq"):
                nested = getattr(value, nested_attr, None)
                if nested is not None:
                    return str(nested)

        return "unknown_sender"

    def _get_sender_name(self, event: AstrMessageEvent, sender_id: str) -> str:
        for attr in ("get_sender_name", "get_sender_nickname"):
            func = getattr(event, attr, None)
            if callable(func):
                try:
                    value = func()
                    if value:
                        return str(value)
                except Exception:
                    pass

        for attr in ("sender_name", "sender_nickname", "nickname", "user_name"):
            value = getattr(event, attr, None)
            if value:
                return str(value)

        msg_obj = getattr(event, "message_obj", None)
        for attr in ("sender_name", "sender_nickname", "nickname", "user_name"):
            value = getattr(msg_obj, attr, None)
            if value:
                return str(value)

        sender = getattr(msg_obj, "sender", None)
        if sender is not None:
            for attr in ("card", "nickname", "name", "user_name"):
                value = getattr(sender, attr, None)
                if value:
                    return str(value)

        return sender_id

    def _get_debounce_key(self, session_id: str, sender_id: str) -> str:
        return f"{session_id}::{sender_id}"

    def _get_buffer(self, key: str, session_id: str, sender_id: str, sender_name: str) -> MessageBuffer:
        if key not in self.buffers:
            self.buffers[key] = MessageBuffer(session_id, sender_id, sender_name)
        return self.buffers[key]

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self.locks:
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    def _get_repeat_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self.repeat_locks:
            self.repeat_locks[session_id] = asyncio.Lock()
        return self.repeat_locks[session_id]

    def _get_repeat_state(self, session_id: str) -> RepeatState:
        if session_id not in self.repeat_states:
            self.repeat_states[session_id] = RepeatState()
        return self.repeat_states[session_id]

    def _parse_csv_set(self, value: str) -> Set[str]:
        return {item.strip() for item in (value or "").split(",") if item.strip()}

    def _get_command_prefixes(self) -> List[str]:
        return [p.strip() for p in self.config.bypass_command_prefixes.split(",") if p.strip()]

    def _get_bot_aliases(self) -> List[str]:
        return [a.strip() for a in self.config.bot_aliases.split(",") if a.strip()]

    def _is_bypass_message(self, text: str) -> bool:
        return any(text.startswith(prefix) for prefix in self._get_command_prefixes())

    def _inject_merged_text(self, event: AstrMessageEvent, merged_text: str):
        if not merged_text:
            return

        event.message_str = merged_text

        try:
            event.message_obj.message = [Plain(merged_text)]
        except Exception as e:
            logger.warning(f"[GroupDebouncer] 注入合并消息链失败: {e}")

    def _get_self_id(self, event: AstrMessageEvent) -> Optional[str]:
        for attr in ("get_self_id", "get_bot_id"):
            func = getattr(event, attr, None)
            if callable(func):
                try:
                    value = func()
                    if value is not None:
                        return str(value)
                except Exception:
                    pass

        msg_obj = getattr(event, "message_obj", None)
        for attr in ("self_id", "bot_id"):
            value = getattr(msg_obj, attr, None)
            if value is not None:
                return str(value)

        return None

    def _has_non_plain_component(self, event: AstrMessageEvent) -> bool:
        try:
            comps = event.message_obj.message or []
        except Exception:
            return False
        return any(not isinstance(c, Plain) for c in comps)

    def _is_at_bot_message(self, event: AstrMessageEvent, text: str) -> bool:
        self_id = self._get_self_id(event)

        try:
            comps = event.message_obj.message or []
        except Exception:
            comps = []

        for c in comps:
            name = c.__class__.__name__.lower()
            if name != "at":
                continue
            value = getattr(c, "qq", None) or getattr(c, "target", None) or getattr(c, "user_id", None)
            if self_id is None or value is None or str(value) == self_id:
                return True

        return self_id is not None and (f"@{self_id}" in text)

    def _is_direct_trigger_message(self, event: AstrMessageEvent, text: str) -> bool:
        if self._is_at_bot_message(event, text):
            return True

        lowered = text.lower()
        for alias in self._get_bot_aliases():
            alias_lower = alias.lower()
            if not alias_lower:
                continue
            if alias_lower in lowered:
                return True

        return False

    def _normalize_repeat_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def _repeat_group_allowed(self, session_id: str) -> bool:
        enabled_groups = self._parse_csv_set(self.config.repeat_enabled_groups)
        disabled_groups = self._parse_csv_set(self.config.repeat_disabled_groups)

        if session_id in disabled_groups:
            return False
        if enabled_groups and session_id not in enabled_groups:
            return False
        return True

    def _repeat_candidate_allowed(self, event: AstrMessageEvent, session_id: str, text: str) -> bool:
        if not self.config.repeat_enabled:
            return False

        if not self._repeat_group_allowed(session_id):
            return False

        mode = (self.config.repeat_mode or "chain").strip().lower()
        if mode not in {"chain", "random", "both"}:
            if self.config.repeat_debug:
                logger.info(f"[GroupDebouncer:Repeat] 非法 repeat_mode={self.config.repeat_mode!r}，跳过复读")
            return False

        if self.config.repeat_text_only and self._has_non_plain_component(event):
            return False

        if self.config.repeat_ignore_commands and self._is_bypass_message(text):
            return False

        if self.config.repeat_ignore_urls and _URL_RE.search(text):
            return False

        if self.config.repeat_ignore_at_bot and self._is_at_bot_message(event, text):
            return False

        text_len = len(text)
        if text_len < self.config.repeat_min_text_length:
            return False
        if text_len > self.config.repeat_max_text_length:
            return False

        return True

    def _try_build_repeat_response(
        self,
        event: AstrMessageEvent,
        session_id: str,
        text: str,
        now: float,
    ) -> Optional[str]:
        if not self._repeat_candidate_allowed(event, session_id, text):
            return None

        sender_id = self._get_sender_id(event)
        self_id = self._get_self_id(event)

        if self.config.repeat_ignore_self_message and self_id is not None and sender_id == self_id:
            return None

        mode = (self.config.repeat_mode or "chain").strip().lower()
        state = self._get_repeat_state(session_id)
        state.cleanup(self.config.repeat_repeated_text_ttl_seconds)

        normalized = self._normalize_repeat_text(text)

        if normalized == state.last_text:
            if sender_id != state.last_sender_id:
                state.repeat_count += 1
                state.last_sender_id = sender_id
            # 同一用户连续发同一句不增加 repeat_count，避免刷屏自触发。
        else:
            state.last_text = normalized
            state.last_sender_id = sender_id
            state.repeat_count = 1

        already_repeated = normalized in state.repeated_texts

        if mode in {"chain", "both"}:
            cooldown_ok = (now - state.last_chain_repeat_at) >= self.config.repeat_cooldown_seconds
            count_ok = state.repeat_count >= self.config.repeat_min_count
            probability_hit = random.random() < self.config.repeat_probability

            if self.config.repeat_debug:
                logger.info(
                    f"[GroupDebouncer:Repeat] group={session_id} mode=chain text={normalized[:40]!r} "
                    f"sender={sender_id} repeat_count={state.repeat_count} count_ok={count_ok} "
                    f"cooldown_ok={cooldown_ok} already_repeated={already_repeated} probability_hit={probability_hit}"
                )

            if count_ok and cooldown_ok and not already_repeated and probability_hit:
                state.last_chain_repeat_at = now
                state.repeated_texts[normalized] = now
                return normalized

        if mode in {"random", "both"}:
            cooldown_ok = (now - state.last_random_repeat_at) >= self.config.repeat_random_cooldown_seconds
            probability_hit = random.random() < self.config.repeat_random_probability

            if self.config.repeat_debug:
                logger.info(
                    f"[GroupDebouncer:Repeat] group={session_id} mode=random text={normalized[:40]!r} "
                    f"sender={sender_id} cooldown_ok={cooldown_ok} already_repeated={already_repeated} "
                    f"probability_hit={probability_hit}"
                )

            if cooldown_ok and not already_repeated and probability_hit:
                state.last_random_repeat_at = now
                state.repeated_texts[normalized] = now
                return normalized

        return None

    def _clear_session_debounce_buffers(self, session_id: str):
        prefix = f"{session_id}::"
        for key in list(self.buffers.keys()):
            if key.startswith(prefix):
                self.buffers[key].clear()
                self.buffers.pop(key, None)
                self.counters.pop(key, None)

    def _clear_event_result_safely(self, event: AstrMessageEvent):
        """
        清理事件上可能残留的发送结果。

        用于旧防抖任务取消场景：旧事件只应静默丢弃，不能携带空 result 进入发送阶段。
        不同 AstrBot 版本的 Event API 可能略有差异，所以这里做兼容调用。
        """
        for method_name in ("clear_result", "set_result"):
            method = getattr(event, method_name, None)
            if not callable(method):
                continue
            try:
                if method_name == "set_result":
                    method(None)
                else:
                    method()
                return
            except Exception as e:
                logger.debug(f"[GroupDebouncer] 清理事件 result 失败 method={method_name}: {e}")

    def _debug_preview(self, value: object) -> str:
        if value is None:
            return "None"
        text = str(value).replace("\n", "\\n")
        limit = max(20, int(self.config.debug_preview_chars or 180))
        if len(text) > limit:
            text = text[:limit] + "..."
        return repr(text)

    def _safe_call_event_method(self, event: AstrMessageEvent, method_name: str, default=None):
        method = getattr(event, method_name, None)
        if not callable(method):
            return default
        try:
            return method()
        except Exception as e:
            return f"<error {method_name}: {e!r}>"

    def _safe_get_result_repr(self, event: AstrMessageEvent) -> str:
        result = self._safe_call_event_method(event, "get_result", None)
        if result is None:
            return "None"
        try:
            chain = getattr(result, "chain", None)
            if chain is not None:
                return self._debug_preview(f"{result!r}, chain={chain!r}")
        except Exception:
            pass
        return self._debug_preview(repr(result))

    def _safe_is_stopped_repr(self, event: AstrMessageEvent) -> str:
        value = self._safe_call_event_method(event, "is_stopped", None)
        if value is None:
            # 某些 AstrBot 版本没有 is_stopped，尝试看常见内部字段。
            for attr in ("_stopped", "stopped", "is_stop"):
                if hasattr(event, attr):
                    try:
                        return repr(getattr(event, attr))
                    except Exception:
                        pass
            return "<unknown>"
        return repr(value)

    def _component_summary(self, event: AstrMessageEvent) -> str:
        try:
            comps = event.message_obj.message or []
        except Exception as e:
            return f"<message_obj error: {e!r}>"
        parts = []
        for c in comps:
            cls = c.__class__.__name__
            if isinstance(c, Plain):
                text = getattr(c, "text", "")
                if self.config.debug_include_message_text:
                    parts.append(f"Plain(len={len(str(text))}, text={self._debug_preview(text)})")
                else:
                    parts.append(f"Plain(len={len(str(text))})")
            else:
                attrs = []
                for attr in ("qq", "target", "user_id", "url", "file", "text"):
                    if hasattr(c, attr):
                        try:
                            attrs.append(f"{attr}={self._debug_preview(getattr(c, attr))}")
                        except Exception:
                            pass
                parts.append(f"{cls}({', '.join(attrs)})" if attrs else cls)
        return "[" + ", ".join(parts) + "]"

    def _debug_event_snapshot(self, tag: str, event: AstrMessageEvent, **extra):
        if not self.config.debug_enabled:
            return
        try:
            raw_text = getattr(event, "message_str", None)
            text_part = ""
            if self.config.debug_include_message_text:
                text_part = f" | message_str={self._debug_preview(raw_text)}"
            else:
                text_part = f" | message_len={len(raw_text or '')}"
            extra_part = ""
            if extra:
                extra_part = " | " + " | ".join(f"{k}={self._debug_preview(v)}" for k, v in extra.items())
            logger.info(
                f"[GroupDebouncer:DEBUG] {tag}"
                f" | session={self._get_session_id(event)}"
                f" | sender={self._get_sender_name(event, self._get_sender_id(event))}({self._get_sender_id(event)})"
                f" | stopped={self._safe_is_stopped_repr(event)}"
                f" | result={self._safe_get_result_repr(event)}"
                f"{text_part}"
                f" | components={self._component_summary(event)}"
                f"{extra_part}"
            )
        except Exception as e:
            logger.info(f"[GroupDebouncer:DEBUG] {tag} | snapshot_failed={e!r}")

    def _debug_buffer_snapshot(self, tag: str, key: str, buffer: Optional[MessageBuffer] = None, **extra):
        if not self.config.debug_enabled:
            return
        try:
            if buffer is None:
                buffer = self.buffers.get(key)
            if buffer is None:
                base = "buffer=None"
            else:
                previews = []
                if self.config.debug_include_message_text:
                    previews = [self._debug_preview(m) for m in buffer.messages]
                base = (
                    f"buffer_count={len(buffer.messages)} | direct={buffer.direct_trigger} | "
                    f"target_in={max(0.0, buffer.target_time - time.time()):.2f}s | "
                    f"last_update_age={time.time() - buffer.last_update:.2f}s | "
                    f"components={len(buffer.components)}"
                )
                if previews:
                    base += f" | messages={previews}"
            extra_part = ""
            if extra:
                extra_part = " | " + " | ".join(f"{k}={self._debug_preview(v)}" for k, v in extra.items())
            logger.info(f"[GroupDebouncer:DEBUG] {tag} | key={key} | {base}{extra_part}")
        except Exception as e:
            logger.info(f"[GroupDebouncer:DEBUG] {tag} | key={key} | buffer_snapshot_failed={e!r}")

    # 关键：priority 必须高于 Heartflow。Heartflow 当前常见为 priority=1000，所以这里用 2000。
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2000)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.enabled:
            return

        if event.is_private_chat():
            return

        raw_text = event.message_str or ""
        text = raw_text.strip()
        self._debug_event_snapshot("event_enter", event, raw_len=len(raw_text), stripped_len=len(text))

        # 空文本/纯图片消息不处理，避免误伤图片类插件。
        if not text:
            self._debug_event_snapshot("skip_empty_or_non_text", event)
            return

        # 命令类消息直接放行，避免防抖插件拦截管理指令。
        if self._is_bypass_message(text):
            logger.info(f"[GroupDebouncer] 命令/前缀消息直接放行: {text[:40]!r}")
            self._debug_event_snapshot("bypass_command", event, prefix_matched=True)
            return

        session_id = self._get_session_id(event)
        sender_id = self._get_sender_id(event)
        sender_name = self._get_sender_name(event, sender_id)
        debounce_key = self._get_debounce_key(session_id, sender_id)
        now = time.time()
        self._debug_event_snapshot("identity_resolved", event, session_id=session_id, sender_id=sender_id, sender_name=sender_name, key=debounce_key)

        # 复读按群维度判断，需要跨 sender 检测；但不参与 LLM 防抖缓冲。
        async with self._get_repeat_lock(session_id):
            repeat_text = self._try_build_repeat_response(event, session_id, text, now)
            if repeat_text:
                logger.info(
                    f"[GroupDebouncer:Repeat] session={session_id}, sender={sender_id}, 触发复读 | text={repeat_text[:60]!r}"
                )
                self._debug_event_snapshot("repeat_before_yield", event, repeat_text=repeat_text)
                # 先产出复读结果，再停止事件传播。
                # 避免 stop_event 状态被后续 result 设置/清理流程冲掉。
                yield event.plain_result(repeat_text)
                self._debug_event_snapshot("repeat_after_yield_before_stop", event, repeat_text=repeat_text)
                event.stop_event()
                self._debug_event_snapshot("repeat_after_stop", event, repeat_text=repeat_text)
                return

        lock = self._get_lock(debounce_key)

        async with lock:
            last_seen = self.last_group_message_time.get(session_id, 0.0)
            idle_gap = now - last_seen if last_seen else 999999.0
            self.last_group_message_time[session_id] = now

            # 冷场后的第一条消息是否直通由配置决定。
            # 若开启，它会牺牲“首条+后续碎片”的合并能力，换取冷场后更快响应。
            if self.config.first_message_no_debounce and idle_gap >= self.config.idle_reset_seconds:
                self._clear_session_debounce_buffers(session_id)
                logger.info(
                    f"[GroupDebouncer] session={session_id}, sender={sender_id}, 冷场 {idle_gap:.1f}s 后首条群消息，直接放行"
                )
                self._debug_event_snapshot("first_message_no_debounce_release", event, idle_gap=f"{idle_gap:.1f}")
                return

            direct_trigger = self._is_direct_trigger_message(event, text)
            window_seconds = (
                self.config.direct_trigger_window_seconds
                if direct_trigger
                else self.config.window_seconds
            )

            buffer = self._get_buffer(debounce_key, session_id, sender_id, sender_name)
            self._debug_buffer_snapshot(
                "before_buffer_add",
                debounce_key,
                buffer,
                idle_gap=f"{idle_gap:.2f}",
                direct_trigger=direct_trigger,
                window_seconds=window_seconds,
            )
            buffer.add(text, event.message_obj.message, self.config.buffer_expire_seconds, sender_name, direct_trigger)

            current_id = self.counters.get(debounce_key, 0) + 1
            self.counters[debounce_key] = current_id

            if not buffer.target_time or not self.config.reset_timer:
                if not buffer.target_time:
                    buffer.target_time = now + window_seconds
            else:
                buffer.target_time = now + window_seconds

            target_time = buffer.target_time
            current_count = len(buffer.messages)

            logger.info(
                f"[GroupDebouncer] key={debounce_key}, session={session_id}, sender={sender_name}({sender_id}), "
                f"进入发送者级防抖 | id={current_id} | count={current_count} | "
                f"direct_trigger={direct_trigger} | target_in={max(0.0, target_time - now):.2f}s"
            )
            self._debug_buffer_snapshot(
                "after_buffer_add",
                debounce_key,
                buffer,
                current_id=current_id,
                latest_counter=self.counters.get(debounce_key),
                reset_timer=self.config.reset_timer,
            )
            self._debug_event_snapshot("after_buffer_add_event_state", event, current_id=current_id)

            if current_count >= self.config.max_messages:
                merged_text = buffer.get_full_text(self.config.include_sender_in_merged_text)
                merged_count = len(buffer.messages)
                buffer.clear()
                self.buffers.pop(debounce_key, None)
                logger.info(
                    f"[GroupDebouncer] key={debounce_key}, 达最大条数 {self.config.max_messages}，立即放行 | "
                    f"merged_count={merged_count} | preview={merged_text[:80]!r}"
                )
                self._debug_event_snapshot("max_messages_before_inject", event, merged_count=merged_count, merged_text=merged_text)
                if self.config.merge_messages:
                    self._inject_merged_text(event, merged_text)
                self._debug_event_snapshot("max_messages_release", event, merged_count=merged_count)
                return

        # 不持锁 sleep，避免阻塞同一群其他 sender 的消息。
        self._debug_event_snapshot("sleep_start", event, current_id=current_id, target_in=f"{max(0.0, target_time - time.time()):.2f}")
        while True:
            sleep_time = max(0.0, target_time - time.time())
            if sleep_time <= 0:
                break
            await asyncio.sleep(min(0.2, sleep_time))

        async with lock:
            latest_id = self.counters.get(debounce_key)
            self._debug_event_snapshot("wakeup_check", event, current_id=current_id, latest_id=latest_id)
            self._debug_buffer_snapshot("wakeup_buffer_state", debounce_key, current_id=current_id, latest_id=latest_id)

            if latest_id != current_id:
                logger.info(
                    f"[GroupDebouncer] key={debounce_key}, 旧事件取消 | current={current_id}, latest={latest_id}, "
                    f"sender={sender_name}({sender_id})"
                )
                self._debug_event_snapshot("cancel_before_clear_stop", event, current_id=current_id, latest_id=latest_id)
                # 旧定时任务只应静默丢弃，不能留下任何发送结果。
                self._clear_event_result_safely(event)
                self._debug_event_snapshot("cancel_after_clear_before_stop", event, current_id=current_id, latest_id=latest_id)
                event.stop_event()
                self._debug_event_snapshot("cancel_after_stop", event, current_id=current_id, latest_id=latest_id)
                return

            buffer = self._get_buffer(debounce_key, session_id, sender_id, sender_name)
            merged_text = buffer.get_full_text(self.config.include_sender_in_merged_text)
            merged_count = len(buffer.messages)
            merged_direct = buffer.direct_trigger
            buffer.clear()
            self.buffers.pop(debounce_key, None)

            self._debug_event_snapshot("release_before_inject", event, merged_count=merged_count, merged_direct=merged_direct, merged_text=merged_text)
            if self.config.merge_messages and merged_text:
                self._inject_merged_text(event, merged_text)
            self._debug_event_snapshot("release_after_inject", event, merged_count=merged_count, merged_direct=merged_direct)

            logger.info(
                f"[GroupDebouncer] key={debounce_key}, 防抖完成，放行该 sender 最后事件 | "
                f"session={session_id} | sender={sender_name}({sender_id}) | 合并={merged_count} 条 | "
                f"direct_trigger={merged_direct} | preview={merged_text[:80]!r}"
            )
            return
