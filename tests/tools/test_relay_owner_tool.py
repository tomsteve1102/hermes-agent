"""[tamako-local] ``relay_to_owner`` — host-side, one-way relay of a message to the owner.

A group chat or a non-owner DM runs its terminal/file/execute_code inside the offline
docker sandbox (``terminal.source_policy``), so it cannot reach the host's send machinery
or credentials.  ``relay_to_owner`` restores exactly one thing the sandbox took away:
"帮我给 Tom 带句话".  It runs in the GATEWAY process, so it touches neither the sandbox
terminal nor the network; the destination is fixed by config (never a tool argument).

What these tests pin down:
  * the owner's OWN turn is refused (no self-relay) — and nothing is sent;
  * an unreadable session source is refused (fail CLOSED);
  * a non-owner turn sends EXACTLY once, to the configured owner target, with the
    ``【转告｜来自 <platform>/<name>】`` source stamp;
  * a group source is allowed (the whole point);
  * over-long text is refused; a same-source second call inside the window is refused
    (in-process rate limit); ``relay.enabled: false`` is refused;
  * a send that fails is reported as an error, never as success.
"""

import json

import pytest

from tools import relay_owner_tool as rt
from tools import media_delivery as md

OWNER_QQ = "0123456789ABCDEF0123456789ABCDEF"
OWNER_WX = "owner-openid@im.wechat"
OWNERS = (f"qqbot:{OWNER_QQ}", f"weixin:{OWNER_WX}")

_BASE_CFG = {
    "enabled": True,
    "owner_targets": {"qqbot": OWNER_QQ, "weixin": OWNER_WX},
    "max_chars": 500,
    "min_seconds_between": 60,
}


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    """The rate-limit clock is process-global; never leak state between tests."""
    rt._last_sent.clear()
    yield
    rt._last_sent.clear()


def _wire(monkeypatch, *, platform="qqbot", chat_type="dm", user_id="5030BEEF",
          user_name="Alice", owners=OWNERS, cfg=None, send=None, now=1000.0):
    """Point every external seam of the tool at an in-test double; returns ``sent`` (list)."""
    monkeypatch.setattr(rt, "_session_source", lambda: {
        "platform": platform, "chat_type": chat_type,
        "user_id": user_id, "user_name": user_name,
    })
    monkeypatch.setattr(rt, "_owner_ids_raw", lambda: list(owners))
    monkeypatch.setattr(rt, "_read_relay_config", lambda: dict(_BASE_CFG if cfg is None else cfg))
    monkeypatch.setattr(rt, "_now", lambda: now)
    sent = []
    if send is None:
        def send(args):
            sent.append(args)
            return json.dumps({"success": True, "message_id": "m1"})
    monkeypatch.setattr(rt, "_dispatch_send", send)
    return sent


def _call(text="测试：Alice让带的问候", media=None):
    args = {"text": text}
    if media is not None:
        args["media"] = media
    return json.loads(rt.relay_to_owner(args))


# --------------------------------------------------------------------------- refusals

def test_owner_source_is_refused_and_nothing_is_sent(monkeypatch):
    """owner 自己（私聊/CLI）调用 → 拒绝；且绝不触发发送。"""
    sent = _wire(monkeypatch, chat_type="dm", user_id=OWNER_QQ, user_name="Tom")
    out = _call()
    assert "error" in out
    assert sent == []


def test_weixin_owner_source_is_refused(monkeypatch):
    sent = _wire(monkeypatch, platform="weixin", chat_type="dm", user_id=OWNER_WX, user_name="Tom")
    out = _call()
    assert "error" in out
    assert sent == []


def test_unreadable_source_is_refused_fail_closed(monkeypatch):
    """来源读不到（ContextVar 未绑定）→ 拒绝。"""
    sent = _wire(monkeypatch, platform="", user_id="")
    assert "error" in _call()
    assert sent == []

    sent = _wire(monkeypatch, platform="qqbot", user_id="")
    assert "error" in _call()
    assert sent == []


def test_over_long_text_is_refused(monkeypatch):
    sent = _wire(monkeypatch, cfg={**_BASE_CFG, "max_chars": 10})
    out = _call(text="x" * 11)
    assert "error" in out
    assert sent == []


def test_disabled_relay_is_refused(monkeypatch):
    sent = _wire(monkeypatch, cfg={**_BASE_CFG, "enabled": False})
    out = _call()
    assert "error" in out
    assert sent == []


# --------------------------------------------------------------------------- the happy path

def test_non_owner_group_source_sends_once_to_the_owner(monkeypatch):
    """群来源（非 owner）→ 发送一次；目标 == 配置的 owner_targets[platform]；带来源前缀。"""
    sent = _wire(monkeypatch, chat_type="group", user_id="5030BEEF", user_name="Alice")
    out = _call(text="帮我跟 Tom 说一声晚安")
    assert out.get("success") is True
    assert len(sent) == 1
    assert sent[0]["action"] == "send"
    assert sent[0]["target"] == f"qqbot:{OWNER_QQ}"
    assert sent[0]["message"].startswith("【转告｜来自")
    assert "qqbot/Alice" in sent[0]["message"]
    assert "帮我跟 Tom 说一声晚安" in sent[0]["message"]


def test_non_owner_dm_source_is_allowed(monkeypatch):
    """非 owner 私聊同样允许。"""
    sent = _wire(monkeypatch, chat_type="dm", user_id="5030BEEF")
    out = _call()
    assert out.get("success") is True
    assert len(sent) == 1


def test_group_source_via_guild_chat_type_is_allowed(monkeypatch):
    sent = _wire(monkeypatch, chat_type="guild", user_id="5030BEEF")
    assert _call().get("success") is True
    assert len(sent) == 1


def test_target_falls_back_to_source_policy_owner_ids(monkeypatch):
    """owner_targets 缺失该平台时，回落到 terminal.source_policy.owner_ids 的同平台 id。"""
    sent = _wire(monkeypatch, cfg={**_BASE_CFG, "owner_targets": {}})
    out = _call()
    assert out.get("success") is True
    assert sent[0]["target"] == f"qqbot:{OWNER_QQ}"


# --------------------------------------------------------------------------- rate limit + failure truth

def test_second_call_within_the_window_is_rate_limited(monkeypatch):
    sent = _wire(monkeypatch, now=1000.0)
    assert _call().get("success") is True
    out = _call()
    assert "error" in out
    assert len(sent) == 1  # the second call never reached the sender


def test_rate_limit_expires_after_the_window(monkeypatch):
    sent = _wire(monkeypatch, now=1000.0)
    assert _call().get("success") is True
    monkeypatch.setattr(rt, "_now", lambda: 1000.0 + 61)
    assert _call().get("success") is True
    assert len(sent) == 2


def test_send_failure_is_reported_not_pretended(monkeypatch):
    """发送失败 → 返回错误，绝不假报成功；且不占用限流窗口（可立刻重试）。"""
    def failing_send(args):
        return json.dumps({"error": "QQBot send failed: c2c=500 group=500"})

    sent = _wire(monkeypatch, send=failing_send)
    out = _call()
    assert "error" in out
    assert out.get("success") is not True

    # A failure must not consume the rate-limit slot.
    sent2 = _wire(monkeypatch, send=None)  # success double
    assert _call().get("success") is True
    assert len(sent2) == 1


def test_sender_exception_becomes_a_tool_error(monkeypatch):
    def explodes(args):
        raise RuntimeError("boom")

    _wire(monkeypatch, send=explodes)
    out = _call()
    assert "error" in out


def test_empty_text_is_refused(monkeypatch):
    sent = _wire(monkeypatch)
    assert "error" in _call(text="   ")
    assert sent == []


# --------------------------------------------------------------------------- attachments (inbox only)

@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    """A profile home with an empty export inbox; HERMES_HOME points at it."""
    home = tmp_path / "hermes-home"
    box = home / "cache" / "exports"
    box.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return box


def test_media_from_the_inbox_is_attached(monkeypatch, inbox):
    """export_file 收件箱里的文件可以作为附件一起带给 Tom（在正文里落成 MEDIA:）。"""
    f = inbox / "export_deadbeef_chart.png"
    f.write_bytes(b"\x89PNG fake")
    sent = _wire(monkeypatch, chat_type="group")
    out = _call(text="图给你", media=[str(f)])
    assert out.get("success") is True
    assert "MEDIA:" in sent[0]["message"]
    assert str(f.resolve()) in sent[0]["message"]


def test_media_outside_the_inbox_is_refused(monkeypatch, tmp_path, inbox):
    """任意宿主路径（不在任何媒体缓存里的）一律拒绝——这正是要堵的口子。"""
    other = tmp_path / "secret.pdf"
    other.write_bytes(b"%PDF fake")
    sent = _wire(monkeypatch, chat_type="group")
    out = _call(text="x", media=[str(other)])
    assert "error" in out
    assert sent == []


def test_inbound_cached_file_is_accepted(monkeypatch, tmp_path):
    """对方(Alice)发来的文件由网关缓存在宿主 cache/documents —— 本轮带进来的那份 relay 可以直接转给 Tom。"""
    home = tmp_path / "home"
    docs = home / "cache" / "documents"
    docs.mkdir(parents=True)
    f = docs / "报告.pdf"
    f.write_bytes(b"%PDF inbound")
    monkeypatch.setenv("HERMES_HOME", str(home))
    sent = _wire(monkeypatch, chat_type="group")
    with md.bind_turn_media([str(f)]):
        out = _call(text="木偶发的，转给你", media=[str(f)])
    assert out.get("success") is True
    assert "MEDIA:" in sent[0]["message"]
    assert str(f.resolve()) in sent[0]["message"]


def test_inbound_cached_image_is_accepted(monkeypatch, tmp_path):
    home = tmp_path / "home"
    imgs = home / "cache" / "images"
    imgs.mkdir(parents=True)
    f = imgs / "img_abc.png"
    f.write_bytes(b"\x89PNG inbound")
    monkeypatch.setenv("HERMES_HOME", str(home))
    sent = _wire(monkeypatch, chat_type="group")
    with md.bind_turn_media([str(f)]):
        out = _call(text="图给他", media=[str(f)])
    assert out.get("success") is True
    assert "MEDIA:" in sent[0]["message"]


def test_cache_file_not_from_this_turn_is_refused(monkeypatch, tmp_path):
    """别的会话缓存里的文件（不是本轮这条消息带的）—— provenance 挡下，不发。"""
    home = tmp_path / "home"
    docs = home / "cache" / "documents"
    docs.mkdir(parents=True)
    other = docs / "someone_else.pdf"
    other.write_bytes(b"%PDF other")
    mine = docs / "mine.pdf"
    mine.write_bytes(b"%PDF mine")
    monkeypatch.setenv("HERMES_HOME", str(home))
    sent = _wire(monkeypatch, chat_type="group")
    with md.bind_turn_media([str(mine)]):
        out = _call(text="转这个", media=[str(other)])
    assert "error" in out
    assert sent == []


def test_cache_file_without_any_provenance_is_refused(monkeypatch, tmp_path):
    """没有任何本轮绑定（未声明附件）时，缓存里的文件也拒（fail-closed）。"""
    home = tmp_path / "home"
    docs = home / "cache" / "documents"
    docs.mkdir(parents=True)
    f = docs / "x.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setenv("HERMES_HOME", str(home))
    sent = _wire(monkeypatch, chat_type="group")
    out = _call(text="x", media=[str(f)])
    assert "error" in out
    assert sent == []


def test_inbound_media_can_be_disabled(monkeypatch, tmp_path):
    """accept_inbound_media: false → 只认收件箱，入站缓存里的文件也拒。"""
    home = tmp_path / "home"
    docs = home / "cache" / "documents"
    docs.mkdir(parents=True)
    f = docs / "报告.pdf"
    f.write_bytes(b"%PDF inbound")
    monkeypatch.setenv("HERMES_HOME", str(home))
    sent = _wire(monkeypatch, chat_type="group",
                 cfg={**_BASE_CFG, "export": {"accept_inbound_media": False}})
    out = _call(text="x", media=[str(f)])
    assert "error" in out
    assert sent == []


def test_container_form_path_is_mapped_and_accepted(monkeypatch, tmp_path):
    """传入容器可见形式（/root/.hermes/cache/…）也能被映射回宿主并接受。"""
    home = tmp_path / "home"
    docs = home / "cache" / "documents"
    docs.mkdir(parents=True)
    f = docs / "x.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setenv("HERMES_HOME", str(home))
    import tools.credential_files as cf
    monkeypatch.setattr(
        cf, "from_agent_visible_cache_path",
        lambda p, container_base="/root/.hermes": (str(f) if str(p).startswith("/root/.hermes/") else p),
    )
    sent = _wire(monkeypatch, chat_type="group")
    with md.bind_turn_media([str(f)]):
        out = _call(text="x", media=["/root/.hermes/cache/documents/x.pdf"])
    assert out.get("success") is True
    assert "MEDIA:" in sent[0]["message"]


def test_media_missing_file_is_refused(monkeypatch, inbox):
    sent = _wire(monkeypatch, chat_type="group")
    out = _call(text="x", media=[str(inbox / "nope.png")])
    assert "error" in out
    assert sent == []


def test_text_media_directive_is_neutralized(monkeypatch, inbox):
    """正文里的 MEDIA: 指令必须被中和，不能靠它夹带宿主文件（连路径一起扔掉）。"""
    sent = _wire(monkeypatch, chat_type="group")
    out = _call(text="看这个 MEDIA:/etc/passwd 别当真")
    assert out.get("success") is True
    assert "MEDIA:" not in sent[0]["message"]
    assert "/etc/passwd" not in sent[0]["message"]
    assert "看这个" in sent[0]["message"] and "别当真" in sent[0]["message"]
