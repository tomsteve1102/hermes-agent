"""Per-turn terminal scope: profile-scoped TERMINAL_* policy.

Multiplexed surfaces (gateway, dashboard/TUI, cron) serve several profiles from one process;
mirroring terminal settings into ``os.environ`` let the first profile pin its backend onto
everyone else (sandbox escape). Like ``agent/secret_scope.py``, a ContextVar holds the active
profile's COMPLETE ``TERMINAL_*`` policy; while bound, ``terminal_env`` resolves ONLY from it
(omitted keys -> defined default, never ambient env). If the policy cannot be resolved a
*refusal* scope is installed and terminal execution raises :class:`TerminalPolicyUnavailable`.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# None = no scope bound (process-env behavior); dict = complete policy; Refusal = resolution failed.
_terminal_scope_var: ContextVar = ContextVar("hermes_terminal_scope", default=None)

# Keys whose default lives in terminal_tool.py, not DEFAULT_CONFIG (which wins on overlap);
# without them the projection is not total.
_TOOL_LEVEL_DEFAULTS: dict[str, Any] = {
    "cwd": ".", "ssh_host": "", "ssh_user": "", "ssh_port": 22, "ssh_key": "",
    "docker_orphan_reaper": True, "docker_persist_across_processes": True,
    "sandbox_dir": "", "lifetime_seconds": 300, "docker_shared_container_key": "",
    "home_mode": "auto",
}


class TerminalPolicyUnavailable(Exception):
    """The routed profile's ``.env``/``config.yaml`` exists but cannot be read/parsed."""


class TerminalPolicyRefusal(dict[str, str]):
    """Marker scope (empty dict subclass) installed when policy resolution failed."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


def set_terminal_scope(mapping: Optional[dict[str, str]]) -> Token:
    """Install *mapping* as the current context's terminal policy."""
    return _terminal_scope_var.set(mapping)


def reset_terminal_scope(token: Token) -> None:
    _terminal_scope_var.reset(token)


def get_terminal_scope() -> Optional[dict[str, str]]:
    """The active scope mapping/refusal, or ``None`` when no scope is bound."""
    return _terminal_scope_var.get()


def enforce_no_refusal() -> None:
    """Raise when the active scope is a refusal scope (fail closed).

    Execution paths (terminal tool, execute_code) call this before spawning anything: under a refusal scope
    the profile's terminal policy could not be resolved, and running with the launch process's ambient
    policy is exactly the authority leak this module closes (#68559 requires refusal, not fallback).
    Non-scoped and policy-scoped contexts pass silently.
    """
    scope = _terminal_scope_var.get()
    if isinstance(scope, TerminalPolicyRefusal):
        raise TerminalPolicyUnavailable(
            f"terminal policy unavailable for this profile: {scope.reason}")


def terminal_env(name: str, default: str = "") -> str:
    """Authoritative read of a ``TERMINAL_*`` variable.

    No scope: process env, then *default*. Refusal scope: raise. Policy scope: ONLY the
    policy; a missing key yields *default*, never os.environ.
    """
    scope = _terminal_scope_var.get()
    if scope is None:
        return os.environ.get(name, default)
    enforce_no_refusal()
    value = scope.get(name)
    return default if value is None else str(value)


def _apply_terminal_mapping(scope: dict[str, str], mapping: dict[str, Any]) -> None:
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP, _terminal_env_value

    for cfg_key, value in mapping.items():
        # cwd placeholders are resolved per-surface later; not a policy value.
        if value is None or (cfg_key == "cwd" and str(value).strip() in {".", "auto", "cwd"}):
            continue
        env_var = TERMINAL_CONFIG_ENV_MAP.get(cfg_key)
        if env_var:
            # List/dict config values must be JSON (same contract as
            # apply_terminal_config_to_env). str() yields Python repr, which
            # json.loads in terminal_tool rejects.
            scope[env_var] = _terminal_env_value(value)


def default_terminal_scope() -> dict[str, str]:
    """Code-default ``TERMINAL_*`` policy: what a profile-bypassing session freezes instead of
    reading ``.env``/``config.yaml``. Total like the profile projection, minus the profile."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    scope: dict[str, str] = {}
    _apply_terminal_mapping(scope, {**_TOOL_LEVEL_DEFAULTS, **(DEFAULT_CONFIG.get("terminal") or {})})
    return scope


def build_profile_terminal_scope(
    hermes_home: Any, *, env_overlay: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Build the COMPLETE effective ``TERMINAL_*`` policy for a profile home.

    Projection: ``DEFAULT_CONFIG['terminal']`` <- profile ``.env`` TERMINAL_* <- *env_overlay*
    <- profile ``config.yaml`` ``terminal:``. Total by construction, so a bound scope never
    widens back to ambient authority. Raises :class:`TerminalPolicyUnavailable` if a present
    file is unreadable.

    *env_overlay* is a TRUSTED ``TERMINAL_*`` mapping captured from the launch process before
    multiplexing began (``tui_gateway/launch_profile_policy.py``): the launch profile's
    env-only policy (``TERMINAL_ENV=ssh`` from systemd, ``op run``, a launcher bridge) has no
    file to rebuild it from, and reading live ``os.environ`` here is the leak this module
    closes. It sits where the process env sits in the standalone bridge — explicit YAML keys
    still win (``apply_terminal_config_to_env``).
    """
    from hermes_cli.config_backend import config_exists

    home = Path(hermes_home)
    scope = default_terminal_scope()
    default_image = scope.get("TERMINAL_DOCKER_IMAGE")
    env_path = home / ".env"
    if env_path.exists():
        # load_env_file swallows OSError by design (secret scope fails soft); an unreadable
        # profile .env must fail closed here.
        try:
            env_path.read_bytes()
        except Exception as exc:
            raise TerminalPolicyUnavailable(f"cannot read {env_path}: {exc}") from exc
        from agent.secret_scope import load_env_file

        profile_env = load_env_file(env_path)
        scope.update((k, str(v)) for k, v in profile_env.items() if k.startswith("TERMINAL_"))
        # Provenance, not value: an image WRITTEN in the profile's .env is the user's choice even when
        # it spells the default (same rule apply_terminal_config_to_env applies to a set env var).
        image_pinned = "TERMINAL_DOCKER_IMAGE" in profile_env
    else:
        image_pinned = False
    if env_overlay:
        scope.update((k, str(v)) for k, v in env_overlay.items()
                     if k.startswith("TERMINAL_") and k != "TERMINAL_DOCKER_IMAGE_PINNED")
        # The overlay is the LAUNCHER's environment: its pin verdict is about the launcher's profile and
        # is never inherited, and the bridge backfills TERMINAL_DOCKER_IMAGE for defaults too, so only a
        # value that differs from the default can prove a choice made there.
        if env_overlay.get("TERMINAL_DOCKER_IMAGE") not in (None, default_image):
            image_pinned = True
    # Read config.yaml directly, not via read_raw_config() (which collapses "missing" and
    # "unparseable" into {}): present-but-unparseable must fail closed.
    config_path = home / "config.yaml"
    try:
        has_config = config_exists(config_path)
    except Exception as exc:
        raise TerminalPolicyUnavailable(f"cannot resolve terminal config in {home}: {exc}") from exc
    if has_config:
        from hermes_cli.config_backend import read_config_doc_readonly

        try:
            # Signature-cached: a scope is rebuilt per routed turn/poll, the file rarely changes.
            raw = read_config_doc_readonly(config_path)
        except Exception as exc:
            raise TerminalPolicyUnavailable(f"cannot parse {config_path}: {exc}") from exc
        raw_terminal = raw.get("terminal") if isinstance(raw, dict) else None
        if isinstance(raw_terminal, dict):
            _apply_terminal_mapping(scope, raw_terminal)
            image_pinned = image_pinned or "docker_image" in raw_terminal
    scope["TERMINAL_DOCKER_IMAGE_PINNED"] = "1" if image_pinned else "0"
    _resolve_scope_cwd_placeholder(scope)
    return scope


def _resolve_scope_cwd_placeholder(scope: dict[str, str]) -> None:
    """Give a scope with no explicit ``terminal.cwd`` the same resolved ``TERMINAL_CWD`` a standalone
    gateway computes at import (``gateway/run.py``: local backend → ``$HOME``; docker with the
    workspace mount → the host cwd signal; other backends → unset). Without it a routed turn's
    ``resolve_agent_cwd()`` falls back to the multiplexer PROCESS cwd (wherever ``hermes gateway``
    was launched), so the system prompt, context-file discovery and the local terminal all run in
    a directory the profile's standalone gateway would never have used."""
    if scope.get("TERMINAL_CWD"):
        return
    from gateway.cwd_placeholder import resolve_placeholder_terminal_cwd

    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd="", terminal_backend=scope.get("TERMINAL_ENV", ""),
        messaging_cwd=None,
        docker_mount_cwd_to_workspace=scope.get(
            "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").strip().lower() in {"true", "1", "yes"},
        home_fallback=str(Path.home()),
    )
    if resolved:
        scope["TERMINAL_CWD"] = resolved


def install_profile_terminal_scope(
    hermes_home: Any, *, env_overlay: Optional[dict[str, str]] = None) -> Token:
    """Build AND install a profile's policy; on failure install the refusal scope. Never raises."""
    try:
        return set_terminal_scope(build_profile_terminal_scope(hermes_home, env_overlay=env_overlay))
    except TerminalPolicyUnavailable as exc:
        logger.warning("terminal policy unavailable: %s", exc)
        return _terminal_scope_var.set(TerminalPolicyRefusal(str(exc)))


@contextmanager
def install_and_reset_profile_terminal_scope(hermes_home: Any) -> Iterator[None]:
    """Install the profile's terminal policy for a bounded turn/fire. Never raises."""
    token = install_profile_terminal_scope(hermes_home)
    try:
        yield
    finally:
        reset_terminal_scope(token)


# ---------------------------------------------------------------------------
# [tamako-local] Per-source sandbox policy (2026-10-09)
#
# Hermes resolves the terminal backend per PROFILE, so one gateway serving one
# profile gives every chat the same execution backend. This adds a per-TURN
# override on top: when the turn's source is a group chat (always) or a sender
# that is not a configured owner, the turn runs under a sandbox policy — by
# default the docker backend on the group sandbox image — instead of the
# profile's host policy. terminal, file and execute_code all read this scope, so
# the three move into the sandbox together.
#
# Fail-closed: the sandbox baseline below is hardcoded (config only overrides
# keys), and a policy that cannot be built installs a REFUSAL scope (terminal
# refuses to run) instead of silently inheriting host authority. A source-less
# turn (CLI, cron, TUI, dashboard) is never touched.
# ---------------------------------------------------------------------------

_GROUP_CHAT_TYPES = frozenset({"group", "guild", "channel", "forum"})

# Hardcoded sandbox baseline; ``terminal.source_policy.sandbox`` overrides any key.
_SANDBOX_POLICY_DEFAULTS: dict[str, Any] = {
    "backend": "docker",
    "docker_image": "hermes-sandbox:group",
    "docker_network": False,                 # --network=none
    "docker_mount_cwd_to_workspace": False,  # no host cwd inside the container
    "docker_volumes": [],                    # no host paths at all
    "docker_forward_env": [],
    "docker_env": {},
    "container_persistent": False,           # one fresh container per session
    "cwd": "/root",
}

_SOURCE_POLICY_KEY = "source_policy"


def read_source_policy(config: Optional[dict] = None) -> dict:
    """``terminal.source_policy`` from the active profile's config.yaml ({} when absent)."""
    if config is None:
        try:
            from hermes_cli.config import read_raw_config

            config = read_raw_config()
        except Exception as exc:  # pragma: no cover - config read failure
            logger.warning("source sandbox policy: cannot read config (%s)", exc)
            return {}
    terminal = (config or {}).get("terminal")
    policy = terminal.get(_SOURCE_POLICY_KEY) if isinstance(terminal, dict) else None
    return policy if isinstance(policy, dict) else {}


def _source_sender_key(source: Any) -> str:
    """``"<platform>:<user_id>"`` for owner matching; "" when either side is missing."""
    platform = getattr(source, "platform", None)
    platform_value = str(getattr(platform, "value", platform) or "").strip().lower()
    user_id = str(getattr(source, "user_id", "") or "").strip()
    return f"{platform_value}:{user_id}".lower() if platform_value and user_id else ""


def _policy_id_set(policy: Optional[dict], key: str) -> set[str]:
    """Lower-cased ``"<platform>:<user_id>"`` entries under *policy[key]* ([] when absent).

    Tolerates a list OR a list-literal/space/comma string (``hermes config set`` can
    persist a list value as a string), so a hand-set entry never silently fails open.
    """
    raw = (policy or {}).get(key)
    if not raw:
        return set()
    if isinstance(raw, str):
        items = [p for p in re.split(r"[,\s]+", raw.strip().strip("[]").replace("'", "").replace('"', "")) if p]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = []
    return {str(x).strip().lower() for x in items if str(x).strip()}


def source_needs_sandbox(source: Any, policy: Optional[dict]) -> bool:
    """Whether this turn must run in the sandbox.

    Group/guild chats → always (no member, owner included, runs on the host).
    Any other chat    → unless the sender is a configured OWNER or a TRUSTED id;
                        fail-closed, so an unclassifiable or unknown sender lands
                        in the sandbox.
    No source at all  → never (CLI, cron, TUI, dashboard keep the profile policy).

    ``trusted_ids`` ([tamako-local]) is the same host treatment as ``owner_ids`` for a
    named friend who needs real host access; groups stay sandboxed regardless.
    """
    if source is None or not (policy or {}).get("enabled"):
        return False
    try:
        chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
        if chat_type in _GROUP_CHAT_TYPES:
            return True
        allowed_host = _policy_id_set(policy, "owner_ids") | _policy_id_set(policy, "trusted_ids")
        return _source_sender_key(source) not in allowed_host
    except Exception:
        return True  # fail closed: an unclassifiable source is a sandbox source


def trusted_sender(source: Any, policy: Optional[dict] = None) -> bool:
    """True when *source*'s sender is named in ``terminal.source_policy.trusted_ids``.

    A host-trusted DM runs on the host; callers use this to drop the sandbox-era
    ``relay`` toolset (terminal already covers sending/file access there). Fail-closed:
    an unreadable policy or an unclassifiable source is NOT trusted.
    """
    try:
        policy = read_source_policy() if policy is None else policy
        key = _source_sender_key(source)
        return bool(key) and key in _policy_id_set(policy, "trusted_ids")
    except Exception:
        return False


def owner_sender(source: Any, policy: Optional[dict] = None) -> bool:
    """True when *source*'s sender is named in ``terminal.source_policy.owner_ids``.

    The operator's own DM. Callers use this to grant the operator an extra toolset that
    must NOT reach every paired friend's DM — adding it to ``platform_toolsets.<p>``
    would do exactly that (see QQAdapter.toolsets_for_source and the ``cronjob`` case:
    a job runs as a source-less, host-privileged cron session, so handing the tool to a
    friend's sandboxed DM is a sandbox escape).
    Fail-closed: an unreadable policy or an unclassifiable source is NOT the owner.
    """
    try:
        policy = read_source_policy() if policy is None else policy
        key = _source_sender_key(source)
        return bool(key) and key in _policy_id_set(policy, "owner_ids")
    except Exception:
        return False


def build_source_sandbox_scope(
    source: Any, *, config: Optional[dict] = None, hermes_home: Any = None
) -> Optional[dict[str, str]]:
    """Sandbox terminal policy for *source*, or ``None`` when the profile policy stands.

    The returned mapping is COMPLETE (profile policy projected, then the sandbox keys
    overlaid), so a bound scope never widens back to ambient authority.
    """
    policy = read_source_policy(config)
    if not source_needs_sandbox(source, policy):
        return None
    if hermes_home is None:
        try:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        except Exception:
            hermes_home = None
    base = build_profile_terminal_scope(hermes_home) if hermes_home else {}
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP, _terminal_env_value

    merged = {**_SANDBOX_POLICY_DEFAULTS, **(policy.get("sandbox") or {})}
    for key, value in merged.items():
        env_var = TERMINAL_CONFIG_ENV_MAP.get(key)
        if not env_var:
            logger.warning("terminal.source_policy.sandbox: unknown key %r ignored", key)
            continue
        base[env_var] = _terminal_env_value(value)
    # The sandbox image is the user's explicit choice — never re-derived from defaults.
    base["TERMINAL_DOCKER_IMAGE_PINNED"] = "1"
    return base


@contextmanager
def install_source_sandbox_scope(source: Any) -> Iterator[None]:
    """Install the sandbox policy for one turn when *source* requires it. Never raises;
    a sandbox turn whose policy cannot be built gets the refusal scope (fail closed)."""
    token: Optional[Token] = None
    policy = read_source_policy()
    if source_needs_sandbox(source, policy):
        try:
            token = set_terminal_scope(
                build_source_sandbox_scope(
                    source, config={"terminal": {_SOURCE_POLICY_KEY: policy}}
                )
            )
        except TerminalPolicyUnavailable as exc:
            logger.warning("source sandbox policy unavailable: %s", exc)
            token = _terminal_scope_var.set(TerminalPolicyRefusal(str(exc)))
        except Exception as exc:
            logger.warning("source sandbox policy build failed: %s", exc)
            token = _terminal_scope_var.set(TerminalPolicyRefusal(str(exc)))
    try:
        yield
    finally:
        if token is not None:
            reset_terminal_scope(token)
