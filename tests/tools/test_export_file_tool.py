"""[tamako-local] ``export_file`` — host-pull a container file into the host inbox.

The sandbox must not be able to write the host, so file delivery is host-pull:
the caller names a CONTAINER path (under a fixed root), the host reads a bounded
slice of it and writes its own copy into the inbox, and the tool hands back the
host path + a ready ``MEDIA:`` line.

What these tests pin down:
  * local backend → the file is already on the host, hand the path back;
  * sandbox pull → inbox gets the bytes, result carries host_path + MEDIA: line;
  * container path policy → outside the root, traversal, and disallowed
    extensions are refused; a symlink that escapes the root is caught in-container;
  * size cap and per-turn count cap are enforced; no sandbox → refused.
"""

import base64
import json
from pathlib import Path

import pytest

from tools import file_export_tool as ef
from tools import media_delivery as md
from tools.media_delivery import DeliveryMedia


class _FakeEnv:
    """Minimal stand-in for a sandbox env: ``execute`` returns canned output."""

    def __init__(self, payload: bytes = b"hello", container_id: str = "cid-1",
                 output: str = None, returncode: int = 0):
        self._payload = payload
        self._container_id = container_id
        self._output = output
        self._returncode = returncode

    def execute(self, cmd):
        out = self._output if self._output is not None else base64.b64encode(self._payload).decode()
        return {"returncode": self._returncode, "output": out}


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A non-local backend (sandbox) profile home."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(md, "_turn_counts", {})
    monkeypatch.setattr(ef, "local_backend", lambda: False)
    return home


def _wire_env(monkeypatch, env):
    monkeypatch.setattr(md, "_active_sandbox_env", lambda task_id: env)


def _patch_policy(monkeypatch, **kw):
    monkeypatch.setattr(DeliveryMedia, "from_config",
                        classmethod(lambda cls, cfg=None: DeliveryMedia(**kw)))


# --------------------------------------------------------------------------- local backend

def test_local_backend_hands_back_the_host_path(monkeypatch, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    monkeypatch.setattr(ef, "local_backend", lambda: True)
    out = json.loads(ef.export_file({"path": str(f)}, task_id="t"))
    assert out["success"] is True
    assert out["host_path"] == str(f)
    assert out["media_line"] == f"MEDIA:{f}"


def test_local_backend_missing_file_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(ef, "local_backend", lambda: True)
    out = json.loads(ef.export_file({"path": str(tmp_path / "nope.png")}, task_id="t"))
    assert "error" in out


# --------------------------------------------------------------------------- sandbox pull

def test_sandbox_pull_writes_inbox_and_returns_media_line(sandbox, monkeypatch):
    _wire_env(monkeypatch, _FakeEnv(b"\x89PNGdata"))
    out = json.loads(ef.export_file({"path": "/root/export/chart.png"}, task_id="t"))
    assert out["success"] is True
    host = Path(out["host_path"])
    assert host.is_file()
    assert host.read_bytes() == b"\x89PNGdata"
    assert out["media_line"] == f"MEDIA:{host}"
    assert host.is_relative_to(sandbox / "cache" / "exports")


def test_empty_path_is_refused(sandbox, monkeypatch):
    _wire_env(monkeypatch, _FakeEnv())
    out = json.loads(ef.export_file({"path": "  "}, task_id="t"))
    assert "error" in out


def test_no_sandbox_env_is_refused(sandbox, monkeypatch):
    monkeypatch.setattr(md, "_active_sandbox_env", lambda task_id: None)
    out = json.loads(ef.export_file({"path": "/root/export/a.txt"}, task_id="t"))
    assert "error" in out


# --------------------------------------------------------------------------- container path policy

def test_path_outside_the_root_is_refused(sandbox, monkeypatch):
    _wire_env(monkeypatch, _FakeEnv())
    out = json.loads(ef.export_file({"path": "/root/secret.txt"}, task_id="t"))
    assert "error" in out


def test_traversal_out_of_the_root_is_refused(sandbox, monkeypatch):
    _wire_env(monkeypatch, _FakeEnv())
    out = json.loads(ef.export_file({"path": "/root/export/../secret.txt"}, task_id="t"))
    assert "error" in out


def test_disallowed_extension_is_refused(sandbox, monkeypatch):
    _wire_env(monkeypatch, _FakeEnv())
    for bad in ("/root/export/run.sh", "/root/export/key.pem", "/root/export/noext"):
        out = json.loads(ef.export_file({"path": bad}, task_id="t"))
        assert "error" in out, bad


def test_symlink_escape_is_refused(sandbox, monkeypatch):
    """The in-container readlink guard reports the outside sentinel."""
    _wire_env(monkeypatch, _FakeEnv(output="__HERMES_OUTSIDE__"))
    out = json.loads(ef.export_file({"path": "/root/export/link.txt"}, task_id="t"))
    assert "error" in out


# --------------------------------------------------------------------------- caps

def test_size_cap_is_enforced(sandbox, monkeypatch):
    _patch_policy(monkeypatch, max_bytes=4)
    _wire_env(monkeypatch, _FakeEnv(b"way too long"))
    out = json.loads(ef.export_file({"path": "/root/export/big.txt"}, task_id="t"))
    assert "error" in out


def test_per_turn_count_cap_is_enforced(sandbox, monkeypatch):
    _patch_policy(monkeypatch, max_files_per_turn=1)
    _wire_env(monkeypatch, _FakeEnv(b"ok"))
    first = json.loads(ef.export_file({"path": "/root/export/a.txt"}, task_id="t"))
    assert first["success"] is True
    second = json.loads(ef.export_file({"path": "/root/export/b.txt"}, task_id="t"))
    assert "error" in second


def test_read_failure_is_reported(sandbox, monkeypatch):
    _wire_env(monkeypatch, _FakeEnv(returncode=1, output="No such file or directory"))
    out = json.loads(ef.export_file({"path": "/root/export/ghost.txt"}, task_id="t"))
    assert "error" in out
