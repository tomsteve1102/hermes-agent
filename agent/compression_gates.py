"""提交前的压缩门禁（2026-10-27 加）。

目的：Hermes 现在是「先压、提交、再算 savings」，所以「压完反而更大」或者
「一个窗口吞掉太多用户原话」这两种情况都是事后才发现。这里把两个判断挪到提交之前，
判定不过就原样返回历史——等于这次压缩没发生过，transcript 保持无损。

两道门（都是纯函数，无状态、无 I/O）：

1. **no net shrink**：组装后的消息列表必须**严格更小**。摘要比它替换掉的原文还长，
   或者放置逻辑把 tail 行复制了一份，都算净亏，直接拦。
2. **folded user turns**：一个窗口吞掉的真实 user 消息超过 ``MAX_FOLDED_USER_TURNS``
   就拦。理由：摘要最容易丢的就是用户原话，而 Hermes 没有 block refs，压完之后
   无法证明「每个被折的消息仍有覆盖」，所以只能从输入端设个上界。

**Fail-open**：这个模块自己出错时，调用方按「放行」处理，绝不因为它拦住压缩。

开关：环境变量 ``HERMES_COMPRESSION_GATE=0``（或 false/off/no）关掉，默认开。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

#: 单个压缩窗口里最多允许吞掉几条真实 user 消息。超过就拦。
#: 30 是实测定的：真实 CLI 会话一次压缩就折 18 条用户消息（2026-10-27），
#: 设 8 等于让自动压缩永久失效。这条只防极端（窗口切错把上百条用户话都吞了）。
MAX_FOLDED_USER_TURNS = 30

_ENV = "HERMES_COMPRESSION_GATE"
_OFF = {"0", "false", "off", "no"}

# 压缩摘要的标记（值必须与 agent.context_compressor 的
# COMPRESSED_SUMMARY_METADATA_KEY 保持一致）。这里刻意不 import 那个模块：
# 门禁要能在压缩器内部被调用而不引入循环依赖。
COMPRESSED_SUMMARY_KEY = "_compressed_summary"
_SUMMARY_CONTENT_MARKERS = ("[CONTEXT COMPACTION", "Conversation Summary")


@dataclass(frozen=True)
class GateVerdict:
    rejected: bool
    reason: str
    before_tokens: int
    after_tokens: int
    folded_user_turns: int = 0

    @property
    def saved_tokens(self) -> int:
        return self.before_tokens - self.after_tokens

    @property
    def saved_pct(self) -> float:
        if not self.before_tokens:
            return 0.0
        return self.saved_tokens / self.before_tokens * 100.0


def gate_enabled() -> bool:
    """默认开；``HERMES_COMPRESSION_GATE=0`` 可一键关掉。

    跑测试时默认**关**（``PYTEST_CURRENT_TEST`` / ``HERMES_TEST_ISOLATION``）：门禁是部署策略，
    拿它去否决上游那些用桩摘要验证压缩机制的测试，等于让 16 个无关测试变红
    （2026-10-28 实测：关掉门禁 299 全过，打开就 16 红，失败点全在「摘要比真摘要短、
    于是净缩减成立但语义不该拦」的桩用例上）。要测门禁本身，显式设
    ``HERMES_COMPRESSION_GATE=1`` —— 显式值永远压过测试探测。
    """
    value = os.environ.get(_ENV)
    if value is not None:
        return value.strip().lower() not in _OFF
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HERMES_TEST_ISOLATION"):
        return False
    return True


def _count_user_turns(messages: Optional[list[dict[str, Any]]]) -> int:
    """数真实用户回合：role=user、正文非空、且不是压缩摘要占位。

    压缩摘要在库里也是 ``role=user``（走 handoff 注入），早先没排除它，会让
    ``folded_user_turns`` 虚高、门禁更容易误拦（2026-10-27 修正）。
    """
    n = 0
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        if (m.get("role") or "") != "user":
            continue
        if m.get(COMPRESSED_SUMMARY_KEY):
            continue
        content = m.get("content")
        if isinstance(content, str):
            head = content[:200]
            if any(marker in head for marker in _SUMMARY_CONTENT_MARKERS):
                continue
            if not content.strip():
                continue
        n += 1
    return n


def evaluate(
    before_messages: list[dict[str, Any]],
    after_messages: list[dict[str, Any]],
    folded_messages: Optional[list[dict[str, Any]]] = None,
    *,
    max_folded_user_turns: int = MAX_FOLDED_USER_TURNS,
    enforce_user_turns: bool = True,
) -> GateVerdict:
    """判一次压缩该不该提交。永不抛异常。

    Args:
        before_messages: 压缩前的完整消息列表。
        after_messages: ``_assemble_compressed`` 组装完、尚未 finalize 的列表。
        folded_messages: 被这次压缩覆盖掉的那段（``messages[start:end]``），用来数 user 回合。
        enforce_user_turns: False 时不因用户回合数拦（手动 ``/compress`` 走这条——
            用户明确要求压缩，启发式不该否决他；净缩减那条仍然照拦）。
    """
    from agent.model_metadata import estimate_messages_tokens_rough

    before = estimate_messages_tokens_rough(before_messages) if before_messages else 0
    after = estimate_messages_tokens_rough(after_messages) if after_messages else 0

    if after <= 0:
        return GateVerdict(True, "empty_result", before, after)
    if after >= before:
        return GateVerdict(True, "no_net_shrink", before, after)

    folded_users = _count_user_turns(folded_messages) if folded_messages is not None else 0
    if enforce_user_turns and max_folded_user_turns and folded_users > max_folded_user_turns:
        return GateVerdict(True, "folded_user_turns=%d" % folded_users, before, after, folded_users)

    return GateVerdict(False, "ok", before, after, folded_users)
