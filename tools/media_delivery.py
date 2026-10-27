"""[tamako-local] Shared file-delivery helper (used by ``relay_to_owner`` and ``export_file``).

One chokepoint for three questions that must never be answered in two places:

  * **Direction.** A sandboxed chat (group / non-owner DM) runs in an offline
    docker container that must NOT be able to write the host. File delivery is
    therefore **host-pull**: the host reads a bounded slice of a container file
    (the same bounded ``head|base64`` read ``tools/image_source.py`` already uses
    to feed vision) and writes its own copy into a host-side INBOX. The caller
    only ever names a container path under a fixed root; it can never name a
    host path.
  * **What may cross.** fixed container root, extension allowlist, byte cap, and
    a per-turn count cap — all enforced here.
  * **Text hygiene.** The send machinery parses ``MEDIA:<path>`` out of a message
    body, so untrusted text must be scrubbed before it becomes a body
    (:func:`DeliveryMedia.neutralize_directives`), and the only ``MEDIA:`` a relay
    carries is one WE appended from a validated inbox file.

Reused pieces (do not rewrite): ``tools/terminal_tool_lifecycle.ensure_task_env``
/ ``get_active_env`` (bring the sandbox up and hand back its env), and the
bounded in-container read pattern from ``tools/image_source._resolve_container_fallback``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import posixpath
import re
import shlex
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Text directive hygiene
# ---------------------------------------------------------------------------

# ``MEDIA:<path>`` is parsed by BasePlatformAdapter.extract_media; the bracketed
# flags are message-global directives. Both must be stripped from untrusted text.
_MEDIA_DIRECTIVE_RE = re.compile(r"(?i)media:[^\s]+")
_FLAG_TOKENS = ("[[audio_as_voice]]", "[[as_document]]")


def neutralize_directives(text: str) -> str:
    """Remove delivery directives from UNTRUSTED text so it cannot smuggle a file.

    A relayed body must not carry a ``MEDIA:`` tag the caller wrote; the only
    ones that may appear are appended by us from a validated inbox path.
    """
    out = str(text or "")
    for token in _FLAG_TOKENS:
        out = out.replace(token, "")
    # Keep the sentence readable: drop just the directive, not the words around it.
    return _MEDIA_DIRECTIVE_RE.sub("", out)


# ---------------------------------------------------------------------------
# Policy defaults
# ---------------------------------------------------------------------------

CONTAINER_EXPORT_ROOT = "/root/export"
DEFAULT_MAX_BYTES = 25 * 1024 * 1024  # 25 MB
DEFAULT_MAX_FILES_PER_TURN = 3

# Media/document types only — never scripts, keys, or config (no .sh/.py/.env/.pem/.key).
DEFAULT_ALLOWED_EXTS = frozenset({
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    # documents / text / data
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".log",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    # archives
    ".zip", ".7z", ".tar", ".gz", ".tgz",
    # audio
    ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac",
    # video
    ".mp4", ".mov", ".mkv", ".webm",
})

_INBOX_SUBDIR = ("cache", "exports")
# Other host-side sources relay may attach from: the GATEWAY's own inbound media
# caches. A file someone sent already lives here (cached on receipt, host-side — the
# QQ adapter's cache_image/cache_document calls), so relaying it touches no sandbox at
# all. Anything OUTSIDE these roots (arbitrary host paths) stays refused.
_DEFAULT_EXTRA_MEDIA_ROOTS = (
    ("cache", "images"), ("cache", "documents"), ("cache", "audio"), ("cache", "videos"),
)
_OUTSIDE_SENTINEL = "__HERMES_OUTSIDE__"

# ---------------------------------------------------------------------------
# Per-turn attachment provenance
# ---------------------------------------------------------------------------
# The gateway binds the CURRENT message's inbound attachment paths around each turn
# (``event.media_urls`` — host paths, post ``rehome_inbound_media``). relay may attach
# a file from the shared inbound cache ONLY when it is one of these. Without this, a
# sandboxed caller could name ANY file in the profile-wide cache (every conversation's
# media lands in one flat dir) and have it handed to the owner.
#   * None       = never bound in this task (no provenance information)
#   * frozenset  = this turn's declared attachments (possibly empty)
_turn_media_paths: ContextVar = ContextVar("hermes_turn_media_paths", default=None)


@contextmanager
def bind_turn_media(paths: Optional[Iterable[str]]):
    """Declare the CURRENT turn's inbound attachment paths (resolved host paths)."""
    resolved: set[str] = set()
    for raw in (paths or ()):
        try:
            resolved.add(str(Path(raw).resolve()))
        except Exception:
            continue
    token = _turn_media_paths.set(frozenset(resolved))
    try:
        yield
    finally:
        _turn_media_paths.reset(token)


def turn_media_paths() -> Optional[frozenset]:
    """The current turn's declared attachment set, or None when never bound."""
    return _turn_media_paths.get()


def is_turn_attachment(path) -> bool:
    """True only when *path* is one of the CURRENT turn's declared attachments."""
    bound = _turn_media_paths.get()
    if not bound:
        return False
    try:
        return str(Path(path).resolve()) in bound
    except Exception:
        return False


# Per-container export counter (a fresh container per turn → the cap resets each turn).
_turn_counts: dict[str, int] = {}


class DeliveryError(Exception):
    """A file-delivery request was refused; ``str(exc)`` is safe to show the caller."""


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def local_backend() -> bool:
    """True when the terminal runs directly on the host (no sandbox to pull from)."""
    try:
        from tools.terminal_scope import terminal_env
        return (terminal_env("TERMINAL_ENV", "local") or "local").strip().lower() in ("", "local")
    except Exception:
        return True


def _active_sandbox_env(task_id: Optional[str]):
    """The active sandbox env for *task_id* (bringing it up on demand), or None."""
    if not task_id:
        return None
    try:
        from tools.terminal_tool import ensure_task_env
        from tools.terminal_tool_lifecycle import get_active_env
        ensure_task_env(task_id)
        return get_active_env(task_id)
    except Exception:
        logger.debug("could not resolve sandbox env for export", exc_info=True)
        return None


def _quota_key(env, task_id: Optional[str]) -> str:
    """Identity of the current sandbox: its container id when known, else the task id."""
    for attr in ("_container_id", "container_id", "name"):
        value = getattr(env, attr, None)
        if value:
            return str(value)
    return f"task:{task_id}"


def _read_bytes_from_env(env, path: str, max_bytes: int, root: str) -> bytes:
    """Bounded read of *path* INSIDE the sandbox, guarded by a realpath re-check.

    The ``head -c`` cap (max+1 distinguishes "at the cap" from "over") bounds host
    memory; the input redirect avoids argv (leading-dash) issues; ``tr -d`` keeps
    it BusyBox-compatible; and the ``readlink -f`` guard refuses a symlink that
    escapes the allowed root.
    """
    quoted = shlex.quote(path)
    root_q = root.rstrip("/")
    cmd = (
        f'rp=$(readlink -f {quoted} 2>/dev/null) || true; '
        f'case "$rp" in {root_q}/*) head -c {max_bytes + 1} < "$rp" | base64 | tr -d \'\\n\' ;; '
        f'*) echo {_OUTSIDE_SENTINEL} ;; esac'
    )
    last = None
    for attempt in range(2):  # Docker exec can fail once on a fresh container (#76566)
        last = env.execute(cmd)
        if isinstance(last, dict) and last.get("returncode", 1) == 0:
            break
        if attempt == 0:
            time.sleep(0.15)
    out = (last or {}).get("output", "") if isinstance(last, dict) else ""
    out = (out or "").strip()
    if out == _OUTSIDE_SENTINEL:
        raise DeliveryError("目标不在允许目录内（可能是指向别处的符号链接），拒绝导出")
    if not isinstance(last, dict) or last.get("returncode", 1) != 0:
        detail = out.splitlines()[0][:200] if out else "读取失败"
        raise DeliveryError(f"读不到容器里的文件：{detail}")
    try:
        data = base64.b64decode(out, validate=True)
    except Exception as exc:
        raise DeliveryError(f"容器返回了无法解析的数据：{exc}")
    if len(data) > max_bytes:
        raise DeliveryError(f"文件超过大小上限（{max_bytes // (1024 * 1024)}MB）")
    if not data:
        raise DeliveryError("文件为空或不存在")
    return data


def _safe_name(path: str) -> str:
    base = posixpath.basename(path) or "export"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[-80:] or "export"


class DeliveryMedia:
    """Validate + move + render file deliveries. See the module docstring."""

    def __init__(self, *, container_root: str = CONTAINER_EXPORT_ROOT,
                 allowed_exts: Optional[Iterable[str]] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 max_files_per_turn: int = DEFAULT_MAX_FILES_PER_TURN,
                 inbox: Optional[Path] = None,
                 extra_media_roots: Optional[Iterable[Iterable[str]]] = None) -> None:
        self.container_root = (str(container_root or CONTAINER_EXPORT_ROOT).rstrip("/")) or CONTAINER_EXPORT_ROOT
        self.allowed_exts = frozenset(
            str(e).lower() for e in (allowed_exts if allowed_exts is not None else DEFAULT_ALLOWED_EXTS))
        self.max_bytes = int(max_bytes)
        self.max_files_per_turn = int(max_files_per_turn)
        self._inbox = Path(inbox) if inbox is not None else None
        self._extra_media_roots = (
            tuple(tuple(str(part) for part in sub) for sub in extra_media_roots)
            if extra_media_roots is not None else _DEFAULT_EXTRA_MEDIA_ROOTS)

    # -- construction -------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Optional[dict] = None) -> "DeliveryMedia":
        """Build from ``config.yaml -> relay.export`` (falls back to the built-in defaults)."""
        if cfg is None:
            try:
                from hermes_cli.config import read_raw_config
                cfg = (read_raw_config() or {}).get("relay") or {}
            except Exception:
                cfg = {}
        export = cfg.get("export") if isinstance(cfg, dict) else None
        export = export if isinstance(export, dict) else {}
        exts = export.get("allowed_exts")
        # Set accept_inbound_media: false to let relay attach ONLY files export_file put
        # in the inbox (no gateway inbound-cache reads at all).
        accept_inbound = export.get("accept_inbound_media", True)
        return cls(
            container_root=str(export.get("container_root") or CONTAINER_EXPORT_ROOT),
            allowed_exts=[str(e).lower() for e in exts] if isinstance(exts, list) and exts else None,
            max_bytes=_as_int(export.get("max_bytes"), DEFAULT_MAX_BYTES),
            max_files_per_turn=_as_int(export.get("max_files_per_turn"), DEFAULT_MAX_FILES_PER_TURN),
            extra_media_roots=_DEFAULT_EXTRA_MEDIA_ROOTS if accept_inbound is not False else (),
        )

    # -- inbox (host side) --------------------------------------------------
    def inbox_dir(self) -> Path:
        """The host-side inbox every exported file lands in (and relay may attach from)."""
        if self._inbox is not None:
            return self._inbox
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).joinpath(*_INBOX_SUBDIR)

    # -- container path (export_file) --------------------------------------
    def validate_container_path(self, raw) -> str:
        """Normalise + validate a CONTAINER path; returns the normalised path or raises."""
        path = str(raw or "").strip()
        if not path:
            raise DeliveryError("没有给文件路径")
        if "\x00" in path:
            raise DeliveryError("路径非法")
        norm = posixpath.normpath(path if path.startswith("/") else "/" + path)
        root = self.container_root
        if norm != root and not norm.startswith(root + "/"):
            raise DeliveryError(f"只能导出 {root}/ 底下的文件")
        ext = posixpath.splitext(norm)[1].lower()
        if ext not in self.allowed_exts:
            raise DeliveryError(f"不允许的文件类型：{ext or '（无扩展名）'}")
        return norm

    def pull(self, task_id: Optional[str], container_path) -> Path:
        """Host-pull a container file into the inbox; returns the host path. Raises DeliveryError."""
        norm = self.validate_container_path(container_path)
        env = _active_sandbox_env(task_id)
        if env is None:
            raise DeliveryError("当前没有可用的沙箱会话，取不到这个文件")
        key = _quota_key(env, task_id)
        used = _turn_counts.get(key, 0)
        if self.max_files_per_turn and used >= self.max_files_per_turn:
            raise DeliveryError(f"本轮导出已达上限（{self.max_files_per_turn} 次）")
        data = _read_bytes_from_env(env, norm, self.max_bytes, self.container_root)
        host_path = self._write_inbox(norm, data)
        if len(_turn_counts) > 512:  # cheap bound; keys are per-container so entries go stale fast
            _turn_counts.clear()
        _turn_counts[key] = used + 1
        return host_path

    def _write_inbox(self, container_path: str, data: bytes) -> Path:
        inbox = self.inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(container_path.encode("utf-8")).hexdigest()[:8]
        target = inbox / f"export_{digest}_{_safe_name(container_path)}"
        target.write_bytes(data)
        return target

    # -- host path (relay attachment) --------------------------------------
    def media_roots(self) -> list[Path]:
        """Host roots relay may attach from: the inbox, then the gateway's inbound caches."""
        roots = [self.inbox_dir()]
        try:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
        except Exception:  # pragma: no cover
            return roots
        for sub in self._extra_media_roots:
            try:
                roots.append(home.joinpath(*sub))
            except Exception:  # pragma: no cover
                continue
        return roots

    def classify_media_root(self, real: Path) -> str:
        """'inbox' (export_file's own), 'inbound' (gateway inbound caches), or 'outside'."""
        try:
            inbox = self.inbox_dir().resolve()
        except Exception:  # pragma: no cover
            inbox = None
        if inbox is not None and real.is_relative_to(inbox):
            return "inbox"
        for root in self.media_roots():
            try:
                rr = root.resolve()
            except Exception:  # pragma: no cover
                continue
            if inbox is not None and rr == inbox:
                continue
            if real.is_relative_to(rr):
                return "inbound"
        return "outside"

    def validate_host_media(self, raw, *, require_turn_provenance: bool = True) -> Path:
        """Validate a HOST path offered for relay: inside a media root, and (for an inbound
        cache file) one of THIS turn's own attachments. Raises DeliveryError.

        The inbox (export_file) and the gateway's inbound caches are the only roots. A file
        someone sent is host-side in the SHARED cache; requiring it to be one of this turn's
        ``event.media_urls`` stops a sandboxed caller naming ANOTHER conversation's cached
        file. Arbitrary host paths are refused outright."""
        text = str(raw or "").strip()
        if not text:
            raise DeliveryError("空的媒体路径")
        # Accept the container-visible form too (``…/cache/…`` ↔ host cache path); a
        # plain host path passes through unchanged.
        candidate = text
        try:
            from tools.credential_files import from_agent_visible_cache_path
            candidate = from_agent_visible_cache_path(text)
        except Exception:  # pragma: no cover
            candidate = text
        try:
            real = Path(candidate).resolve()
        except Exception as exc:
            raise DeliveryError(f"媒体路径无法解析：{exc}")
        kind = self.classify_media_root(real)
        if kind == "outside":
            raise DeliveryError("只能转交入站媒体缓存（对方刚发的附件）或 export_file 收件箱里的文件")
        if kind == "inbound" and require_turn_provenance and not is_turn_attachment(real):
            raise DeliveryError("只能转交本会话这条消息里刚收到的附件；别的会话缓存里的文件不行")
        if not real.is_file():
            raise DeliveryError("文件不存在")
        if real.suffix.lower() not in self.allowed_exts:
            raise DeliveryError(f"不允许的文件类型：{real.suffix.lower() or '（无扩展名）'}")
        if real.stat().st_size > self.max_bytes:
            raise DeliveryError(f"文件超过大小上限（{self.max_bytes // (1024 * 1024)}MB）")
        return real

    @staticmethod
    def render_media_line(host_path) -> str:
        """The ``MEDIA:<path>`` directive the agent puts in its reply/send body."""
        return f"MEDIA:{host_path}"
