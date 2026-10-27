"""[tamako-local] Per-source sandbox terminal policy (2026-10-09).

Hermes resolves the terminal backend per PROFILE, so one gateway serving one profile
gave every chat the same execution backend.  ``terminal.source_policy`` adds a per-turn
override: a group chat (always) or a non-owner sender runs its terminal/file/execute_code
inside the sandbox (docker by default), while the owner's DMs and every source-less turn
(CLI / cron / TUI) keep the host policy.

What these tests pin down:
  * classification — group/guild always sandbox, owner DM host, unknown → sandbox (fail CLOSED);
  * the sandbox scope is COMPLETE and carries the hardcoded baseline even when the config
    block is absent or thin (never a partial scope that could widen back to ambient authority);
  * config overrides land, unknown keys are ignored;
  * the context manager installs for sandbox sources, restores afterwards, and leaves
    non-sandbox sources untouched;
  * a policy that cannot be built fails CLOSED (refusal scope → terminal refuses).
"""

from types import SimpleNamespace

import pytest

from tools import terminal_scope as ts
from tools.terminal_scope import (
    TerminalPolicyRefusal,
    TerminalPolicyUnavailable,
    build_source_sandbox_scope,
    get_terminal_scope,
    install_source_sandbox_scope,
    reset_terminal_scope,
    set_terminal_scope,
    source_needs_sandbox,
    terminal_env,
)

OWNER_QQ = "0123456789ABCDEF0123456789ABCDEF"
OWNER_WX = "owner-openid@im.wechat"
TRUSTED_QQ = "FEDCBA9876543210FEDCBA9876543210"  # Alice — host-trusted by config

_POLICY = {
    "enabled": True,
    "owner_ids": [f"qqbot:{OWNER_QQ}", f"weixin:{OWNER_WX}"],
}


def _policy_config(policy=None):
    return {"terminal": {"source_policy": _POLICY if policy is None else policy}}


def _qq(chat_type="dm", user_id=OWNER_QQ):
    return SimpleNamespace(platform="qqbot", chat_type=chat_type, user_id=user_id)


@pytest.fixture()
def empty_home(tmp_path, monkeypatch):
    """A profile home with no config.yaml/.env: the scope is built from defaults only."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    return tmp_path / "hermes-home"


# --------------------------------------------------------------------------- classification

def test_group_and_guild_always_sandbox_even_for_the_owner(empty_home):
    """群内一律沙箱，无一例外 — the owner's own messages in a group included."""
    for chat_type in ("group", "guild", "GROUP"):
        assert source_needs_sandbox(_qq(chat_type), _POLICY) is True


def test_owner_dm_keeps_the_host_policy(empty_home):
    for source in (_qq("dm"), _qq("c2c"), _qq("private")):
        assert source_needs_sandbox(source, _POLICY) is False
        assert build_source_sandbox_scope(source, config=_policy_config(), hermes_home=empty_home) is None


def test_trusted_dm_keeps_the_host_policy(empty_home):
    """[tamako-local] ``trusted_ids`` gets the same host treatment as the owner (Alice)."""
    policy = {"enabled": True, "owner_ids": [], "trusted_ids": [f"qqbot:{TRUSTED_QQ}"]}
    source = _qq("dm", user_id=TRUSTED_QQ)
    assert source_needs_sandbox(source, policy) is False
    assert build_source_sandbox_scope(source, config=_policy_config(policy), hermes_home=empty_home) is None


def test_trusted_id_still_sandboxes_in_a_group(empty_home):
    """A group is sandboxed for EVERYONE, trusted or not, owner included."""
    policy = {"enabled": True, "owner_ids": [], "trusted_ids": [f"qqbot:{TRUSTED_QQ}"]}
    assert source_needs_sandbox(_qq("group", user_id=TRUSTED_QQ), policy) is True


def test_trusted_ids_as_a_string_literal_still_matches(empty_home):
    """A list value persisted as a string (``hermes config set``) must not fail open."""
    policy = {"enabled": True, "owner_ids": [], "trusted_ids": f"['qqbot:{TRUSTED_QQ}']"}
    assert source_needs_sandbox(_qq("dm", user_id=TRUSTED_QQ), policy) is False
    assert source_needs_sandbox(_qq("dm", user_id="someone-else"), policy) is True


def test_non_owner_dm_is_sandboxed(empty_home):
    assert source_needs_sandbox(_qq("dm", user_id="5030BEDF03D4CB642ECDB8B2E8C12659"), _POLICY) is True


def test_weixin_owner_id_matches(empty_home):
    source = SimpleNamespace(platform="weixin", chat_type="dm", user_id=OWNER_WX)
    assert source_needs_sandbox(source, _POLICY) is False


def test_unknown_sender_sandboxes_fail_closed(empty_home):
    """Empty/absent identity must NOT be read as 'owner'."""
    assert source_needs_sandbox(_qq("dm", user_id=""), _POLICY) is True
    assert source_needs_sandbox(_qq("dm", user_id="whoever"), {"enabled": True, "owner_ids": []}) is True


def test_sourceless_turn_is_never_sandboxed(empty_home):
    """CLI / cron / TUI / dashboard have no source — they keep the host backend."""
    assert source_needs_sandbox(None, _POLICY) is False


def test_disabled_or_absent_policy_is_a_noop(empty_home):
    assert source_needs_sandbox(_qq("group"), {}) is False
    assert source_needs_sandbox(_qq("group"), {"enabled": False}) is False
    assert build_source_sandbox_scope(_qq("group"), config={"terminal": {}}, hermes_home=empty_home) is None


def test_unclassifiable_source_sandboxes(empty_home):
    class ExplodingSource:
        @property
        def chat_type(self):
            raise RuntimeError("boom")

        platform = "qqbot"
        user_id = "x"

    assert source_needs_sandbox(ExplodingSource(), _POLICY) is True


# --------------------------------------------------------------------------- scope contents

def test_sandbox_scope_is_complete_and_offline_by_default(empty_home):
    scope = build_source_sandbox_scope(_qq("group"), config=_policy_config(), hermes_home=empty_home)
    assert scope is not None
    assert scope["TERMINAL_ENV"] == "docker"
    assert scope["TERMINAL_DOCKER_IMAGE"] == "hermes-sandbox:group"
    assert scope["TERMINAL_DOCKER_IMAGE_PINNED"] == "1"
    # bools serialize the way the profile projection already spells them (str(bool));
    # every reader goes through _tenv_bool (``.lower() in {"true","1","yes"}``).
    assert scope["TERMINAL_DOCKER_NETWORK"] == "False"
    assert scope["TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"] == "False"
    assert scope["TERMINAL_DOCKER_VOLUMES"] == "[]"
    assert scope["TERMINAL_DOCKER_FORWARD_ENV"] == "[]"
    assert scope["TERMINAL_DOCKER_ENV"] == "{}"
    assert scope["TERMINAL_CONTAINER_PERSISTENT"] == "False"
    assert scope["TERMINAL_CWD"] == "/root"
    # Complete by construction: the other terminal keys are projected, not missing.
    assert "TERMINAL_TIMEOUT" in scope


def test_hardcoded_baseline_applies_even_without_a_sandbox_block(empty_home):
    """A thin/absent ``sandbox`` block must still land in the sandbox, never the host."""
    scope = build_source_sandbox_scope(
        _qq("group"), config=_policy_config({"enabled": True, "owner_ids": []}), hermes_home=empty_home
    )
    assert scope["TERMINAL_ENV"] == "docker"
    assert scope["TERMINAL_DOCKER_NETWORK"] == "False"


def test_config_overrides_land_and_unknown_keys_are_ignored(empty_home):
    policy = {
        "enabled": True,
        "owner_ids": [],
        "sandbox": {"docker_image": "my/sandbox:1", "container_persistent": True, "nope": 1},
    }
    scope = build_source_sandbox_scope(_qq("group"), config=_policy_config(policy), hermes_home=empty_home)
    assert scope["TERMINAL_DOCKER_IMAGE"] == "my/sandbox:1"
    assert scope["TERMINAL_CONTAINER_PERSISTENT"] == "True"
    assert "nope" not in scope and "NOPE" not in " ".join(scope)


# --------------------------------------------------------------------------- turn wrapper

def test_context_manager_installs_and_restores(empty_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: _policy_config())
    assert get_terminal_scope() is None
    with install_source_sandbox_scope(_qq("group")):
        assert terminal_env("TERMINAL_ENV") == "docker"
        assert terminal_env("TERMINAL_DOCKER_IMAGE") == "hermes-sandbox:group"
    assert get_terminal_scope() is None


def test_context_manager_leaves_owner_turns_untouched(empty_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: _policy_config())
    token = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": "/host/me"})
    try:
        with install_source_sandbox_scope(_qq("dm")):
            assert terminal_env("TERMINAL_ENV") == "local"
            assert terminal_env("TERMINAL_CWD") == "/host/me"
    finally:
        reset_terminal_scope(token)


def test_policy_build_failure_fails_closed(empty_home, monkeypatch):
    """A sandbox turn whose policy cannot be built must NOT inherit host authority."""
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: _policy_config())

    def _boom(*_a, **_k):
        raise TerminalPolicyUnavailable("cannot parse config.yaml")

    monkeypatch.setattr(ts, "build_source_sandbox_scope", _boom)
    with install_source_sandbox_scope(_qq("group")):
        assert isinstance(get_terminal_scope(), TerminalPolicyRefusal)
        with pytest.raises(TerminalPolicyUnavailable):
            terminal_env("TERMINAL_ENV")
    assert get_terminal_scope() is None


def test_unexpected_build_error_also_fails_closed(empty_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: _policy_config())
    monkeypatch.setattr(ts, "build_source_sandbox_scope", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bug")))
    with install_source_sandbox_scope(_qq("group")):
        assert isinstance(get_terminal_scope(), TerminalPolicyRefusal)
