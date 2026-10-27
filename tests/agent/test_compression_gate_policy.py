"""门禁本体的策略与判定（2026-10-28 补：此前这个补丁零自有测试）。

上游那 16 个用桩摘要验证压缩机制的测试会撞上门禁，所以 ``gate_enabled()`` 在 pytest 下
默认关 —— 这里把「默认」「显式关」「显式开压过测试探测」三条都钉住，别让将来的改动
把「测试里意外生效」或「生产里意外失效」的洞再打开。
"""
import pytest

from agent import compression_gates as cg


def _msgs(n_users: int, filler: str = "x") -> list[dict]:
    return [{"role": "user", "content": filler * 40} for _ in range(n_users)]


# ── 开关 ────────────────────────────────────────────────────────────────────

def test_default_on_outside_pytest(monkeypatch):
    monkeypatch.delenv("HERMES_COMPRESSION_GATE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    assert cg.gate_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off", "no", " Off "])
def test_explicit_off(monkeypatch, value):
    monkeypatch.setenv("HERMES_COMPRESSION_GATE", value)
    assert cg.gate_enabled() is False


def test_inert_under_pytest_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_COMPRESSION_GATE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test_y (call)")
    assert cg.gate_enabled() is False


def test_inert_under_isolation_marker(monkeypatch):
    monkeypatch.delenv("HERMES_COMPRESSION_GATE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert cg.gate_enabled() is False


def test_explicit_on_beats_test_probe(monkeypatch):
    monkeypatch.setenv("HERMES_COMPRESSION_GATE", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test_y (call)")
    assert cg.gate_enabled() is True


# ── 判定 ────────────────────────────────────────────────────────────────────

def test_rejects_empty_result():
    v = cg.evaluate(_msgs(3), [])
    assert v.rejected and v.reason == "empty_result"


def test_rejects_when_summary_is_not_smaller():
    before = _msgs(3)
    v = cg.evaluate(before, before)
    assert v.rejected and v.reason == "no_net_shrink"


def test_accepts_a_real_shrink():
    before = _msgs(10, "y" * 200)
    after = [{"role": "user", "content": "[CONTEXT COMPACTION] short summary"}]
    v = cg.evaluate(before, after, before)
    assert not v.rejected and v.reason == "ok"
    assert v.saved_tokens > 0 and v.saved_pct > 0


def test_rejects_too_many_folded_user_turns():
    before = _msgs(40, "z" * 200)
    after = [{"role": "user", "content": "[CONTEXT COMPACTION] short summary"}]
    v = cg.evaluate(before, after, before)
    assert v.rejected and v.reason.startswith("folded_user_turns=")
    assert v.folded_user_turns == 40


def test_manual_compress_ignores_user_turn_heuristic():
    before = _msgs(40, "z" * 200)
    after = [{"role": "user", "content": "[CONTEXT COMPACTION] short summary"}]
    v = cg.evaluate(before, after, before, enforce_user_turns=False)
    assert not v.rejected, "手动 /compress 只该被净缩减那条拦"


def test_summary_placeholders_do_not_count_as_user_turns():
    folded = _msgs(5) + [{"role": "user", "content": "[CONTEXT COMPACTION] prior summary"}]
    assert cg._count_user_turns(folded) == 5


def test_never_raises_on_garbage():
    v = cg.evaluate([], [{"role": "assistant", "content": "ok"}], None)
    assert v.rejected is True  # before=0 → after>=before
