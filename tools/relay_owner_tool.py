"""[tamako-local] ``relay_to_owner`` — relay ONE message from a non-owner chat to the owner.

Context: a group chat or a non-owner DM runs its ``terminal`` / ``file`` /
``execute_code`` inside the offline docker sandbox (``tools/terminal_scope.py``
``terminal.source_policy``), so it has neither the host send machinery nor any
credential.  That correctly kills the ability to reach the host — but it also
kills the ordinary social act of "帮我给 Tom 带句话".

This tool restores exactly that one ability and nothing else:

  * It runs in the GATEWAY process, so it touches neither the sandbox terminal
    nor the network on the caller's behalf.
  * The DESTINATION IS FIXED BY CONFIG — the tool takes no target argument, so a
    sandboxed caller can never name an arbitrary recipient.  Target resolution:
    ``relay.owner_targets[platform]`` if set, else the same platform's entry in
    ``terminal.source_policy.owner_ids``.
  * Only a NON-owner source may call it (the owner has no one to relay to);
    an unreadable session source is refused (fail CLOSED).
  * Every relay is stamped ``【转告｜来自 <platform>/<name>】`` so the owner always
    sees who it came from, and is bounded in length and rate.

Sending reuses the existing machinery (``tools/send_message_tool.py`` — the same
module ``hermes send`` and cron delivery use); this file contains NO platform API
calls of its own.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from tools.registry import registry, tool_error
from tools.media_delivery import DeliveryError, DeliveryMedia, neutralize_directives

logger = logging.getLogger(__name__)

_TOOL_NAME = "relay_to_owner"
_TOOLSET = "relay"
_DEFAULT_MAX_CHARS = 500
_DEFAULT_MIN_SECONDS = 60.0
_MAX_NAME_CHARS = 64
_SOURCE_STAMP = "【转告｜来自 {source}】"

# Per-source last-send clock (process-local, intentionally not persisted — YAGNI).
_last_sent: dict[str, float] = {}


RELAY_SCHEMA = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": (
            "把一句消息转告给 owner（Tom）。只能在此会话不是 owner 本人的时候用"
            "（群聊 / 他人私聊里「帮我给 Tom 带句话」的场景）；目标固定，不接受目标参数，"
            "会自动标注来源。只发纯文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要转告的原话（会被自动加上来源标注；文本里的 MEDIA: 指令会被中和）",
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "可选：要一并带给 owner 的文件——必须是 export_file 导出到收件箱里的宿主路径。"
                        "其他宿主路径一律拒绝。只发纯文本时不要传。"
                    ),
                },
            },
            "required": ["text"],
        },
    },
}


# --------------------------------------------------------------------------- seams
# Each external dependency sits behind a tiny module-level function so tests can
# patch it without importing the gateway / config / sender machinery.

def _now() -> float:
    """Monotonic clock for the rate limiter (patchable)."""
    import time

    return time.monotonic()


def _session_source() -> dict:
    """This turn's (platform, chat_type, user_id, user_name) from the gateway ContextVars."""
    from gateway.session_context import get_session_env

    return {
        "platform": (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip(),
        "chat_type": (get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "").strip().lower(),
        "user_id": (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip(),
        "user_name": (get_session_env("HERMES_SESSION_USER_NAME", "") or "").strip(),
    }


def _read_relay_config() -> dict:
    """``relay:`` from the active profile's config.yaml ({} when absent/unreadable)."""
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
    except Exception as exc:  # pragma: no cover - config read failure
        logger.warning("relay config read failed: %s", exc)
        return {}
    relay = (cfg or {}).get("relay")
    return relay if isinstance(relay, dict) else {}


def _owner_ids_raw() -> list[str]:
    """``terminal.source_policy.owner_ids`` verbatim (case preserved — ids are case-sensitive)."""
    try:
        from tools.terminal_scope import read_source_policy

        policy = read_source_policy()
    except Exception:  # pragma: no cover - config read failure
        return []
    return [str(x).strip() for x in (policy.get("owner_ids") or []) if str(x).strip()]


def _dispatch_send(args: dict) -> str:
    """Run the real send machinery (lazy import keeps this module cheap at discovery)."""
    from tools.send_message_tool import send_message_tool

    return send_message_tool(args)


# --------------------------------------------------------------------------- helpers

def _sender_key(platform: str, user_id: str) -> str:
    return f"{platform}:{user_id}".lower()


def _is_owner(sender_key: str, owners: list[str]) -> bool:
    """Case-insensitive membership (matches terminal_scope's owner matching)."""
    return sender_key in {o.lower() for o in owners}


def _resolve_owner_target(platform: str, cfg: dict, owners: list[str]) -> str:
    """The owner's send target on *platform*: ``owner_targets[platform]``, else the same
    platform's id from ``owner_ids``.  Preserves the configured case (QQ openids are
    upper-case hex) because the target is used verbatim on the wire."""
    targets = cfg.get("owner_targets")
    if isinstance(targets, dict):
        value = targets.get(platform)
        if value is None:
            value = targets.get(str(platform).lower())
        if value:
            return str(value).strip()
    prefix = f"{platform.lower()}:"
    for entry in owners:
        if entry.lower().startswith(prefix) and len(entry) > len(prefix):
            return entry[len(prefix):].strip()
    return ""


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_send_result(raw) -> dict:
    """``send_message_tool`` returns a JSON string; normalise to a dict, fail-closed."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"error": str(raw)}


def _resolve_media(raw, cfg: dict):
    """``(list[Path], None)`` for inbox-backed attachments, or ``(None, error_text)``.

    Only files ``export_file`` put into the host inbox may be attached — never an
    arbitrary host path (that would reopen the very hole this closes)."""
    if not raw:
        return [], None
    items = [raw] if isinstance(raw, (str, bytes, bytearray)) else list(raw)
    policy = DeliveryMedia.from_config(cfg)
    resolved = []
    for item in items:
        try:
            resolved.append(policy.validate_host_media(item))
        except DeliveryError as exc:
            return None, str(exc)
        except Exception as exc:  # pragma: no cover - unexpected
            return None, str(exc)
    if policy.max_files_per_turn and len(resolved) > policy.max_files_per_turn:
        return None, f"一次最多带 {policy.max_files_per_turn} 个文件"
    return resolved, None


# --------------------------------------------------------------------------- handler

def relay_to_owner(args, **kw) -> str:
    """Tool handler: relay ``args['text']`` to the configured owner. See module docstring."""
    src = _session_source()
    platform, user_id = src["platform"], src["user_id"]
    # Fail CLOSED: no source identity means we cannot tell owner from stranger.
    if not platform or not user_id:
        return tool_error("转告不可用：读不到当前会话来源，无法确认发送方。")

    cfg = _read_relay_config()
    if cfg.get("enabled", True) is False:
        return tool_error("转告功能当前已关闭。")

    owners = _owner_ids_raw()
    sender_key = _sender_key(platform, user_id)
    if _is_owner(sender_key, owners):
        return tool_error("owner 本人无需代转，直接对自己说就行。")

    # Scrub delivery directives from untrusted text: the send machinery parses
    # ``MEDIA:<path>`` out of the body, so a caller must not be able to smuggle a file.
    text = neutralize_directives(str(args.get("text") or "")).strip()
    if not text:
        return tool_error("转告不可用：text 为空。")

    max_chars = _as_int(cfg.get("max_chars"), _DEFAULT_MAX_CHARS)
    if max_chars > 0 and len(text) > max_chars:
        return tool_error(f"转告内容太长啦（{len(text)} 字，上限 {max_chars} 字），精简一下再发。")

    target_chat = _resolve_owner_target(platform, cfg, owners)
    if not target_chat:
        return tool_error(f"转告不可用：没有配置 {platform} 上 owner 的发送目标。")

    media, media_err = _resolve_media(args.get("media"), cfg)
    if media_err:
        return tool_error(f"转告附件不可用：{media_err}")

    min_seconds = _as_float(cfg.get("min_seconds_between"), _DEFAULT_MIN_SECONDS)
    now = _now()
    last = _last_sent.get(sender_key)
    if last is not None and min_seconds > 0 and (now - last) < min_seconds:
        wait = int(min_seconds - (now - last)) + 1
        return tool_error(f"转告太频繁啦，请 {wait} 秒后再试一条。")

    name = (src["user_name"] or user_id)[:_MAX_NAME_CHARS]
    body = _SOURCE_STAMP.format(source=f"{platform}/{name}") + text
    for host_path in media:
        body += "\n" + DeliveryMedia.render_media_line(host_path)
    target = f"{platform}:{target_chat}"

    try:
        raw = _dispatch_send({"action": "send", "target": target, "message": body})
    except Exception as exc:
        logger.exception("relay_to_owner send raised")
        return tool_error(f"转告发送失败：{exc}")

    result = _parse_send_result(raw)
    detail = result.get("error") or ("" if result.get("success") else str(raw))
    if result.get("error") or not result.get("success"):
        # Never report a delivery we could not confirm (QQ/Weixin can answer
        # HTTP-200-but-business-error — see the gateway skill's "假成功" notes).
        logger.warning("relay_to_owner send reported failure for %s: %s", target, detail)
        return tool_error(f"转告发送失败：{detail or '发送器未确认成功'}")

    # Only a confirmed send consumes the rate-limit window, so a failure can be retried at once.
    _last_sent[sender_key] = now
    return json.dumps(
        {"success": True, "target": target, "delivered": True, "message_id": result.get("message_id")},
        ensure_ascii=False,
    )


registry.register(
    name=_TOOL_NAME,
    toolset=_TOOLSET,
    schema=RELAY_SCHEMA,
    handler=relay_to_owner,
    emoji="📮",
    max_result_size_chars=4000,
)
