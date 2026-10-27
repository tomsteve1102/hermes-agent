"""Tests for the QQ Bot platform adapter."""

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from gateway.config import PlatformConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**extra):
    """Build a PlatformConfig(enabled=True, extra=extra) for testing."""
    return PlatformConfig(enabled=True, extra=extra)


# ---------------------------------------------------------------------------
# check_qq_requirements
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# QQAdapter.__init__
# ---------------------------------------------------------------------------

class TestQQAdapterInit:
    def _make(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))


    def test_env_fallback(self):
        with mock.patch.dict(os.environ, {"QQ_APP_ID": "env_id", "QQ_CLIENT_SECRET": "env_sec"}, clear=False):
            adapter = self._make()
            assert adapter._app_id == "env_id"
            assert adapter._client_secret == "env_sec"


    def test_dm_policy_default(self):
        adapter = self._make(app_id="a", client_secret="b")
        assert adapter._dm_policy == "pairing"



    def test_allow_from_parsing_string(self):
        adapter = self._make(app_id="a", client_secret="b", allow_from="x, y , z")
        assert adapter._allow_from == ["x", "y", "z"]




# ---------------------------------------------------------------------------
# _coerce_list
# ---------------------------------------------------------------------------

class TestCoerceList:
    def _fn(self, value):
        from plugins.platforms.qqbot.utils import coerce_list as _coerce_list
        return _coerce_list(value)

    def test_none(self):
        assert self._fn(None) == []

    def test_string(self):
        assert self._fn("a, b ,c") == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _is_voice_content_type
# ---------------------------------------------------------------------------

class TestIsVoiceContentType:
    def _fn(self, content_type, filename):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter._is_voice_content_type(content_type, filename)


    def test_voice_extension_fallback_when_content_type_empty(self):
        """content_type='' with audio extension → True (extension fallback)."""
        assert self._fn("", "file.silk") is True




# ---------------------------------------------------------------------------
# Voice attachment SSRF protection
# ---------------------------------------------------------------------------

class TestVoiceAttachmentSSRFProtection:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))


    def test_connect_uses_redirect_guard_hook(self):
        from gateway.platforms.base import ssrf_redirect_guard
        from plugins.platforms.qqbot.adapter import QQAdapter

        client = mock.AsyncMock()
        with mock.patch("plugins.platforms.qqbot.adapter.httpx.AsyncClient", return_value=client) as async_client_cls:
            adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
            adapter._ensure_token = mock.AsyncMock(side_effect=RuntimeError("stop after client creation"))

            connected = asyncio.run(adapter.connect())

        assert connected is False
        assert async_client_cls.call_count == 1
        kwargs = async_client_cls.call_args.kwargs
        assert kwargs.get("follow_redirects") is True
        assert kwargs.get("event_hooks", {}).get("response") == [ssrf_redirect_guard]


# ---------------------------------------------------------------------------
# Voice attachment temp-file cleanup
# ---------------------------------------------------------------------------

class TestVoiceAttachmentTempCleanup:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))

    def _setup_download_mocks(self, adapter, content=b"RIFFmock-wav-audio-data"):
        response = mock.Mock()
        response.content = content
        response.headers = {"content-type": "audio/wav"}
        response.raise_for_status = mock.Mock()

        adapter._http_client = mock.AsyncMock()
        adapter._http_client.get = mock.AsyncMock(return_value=response)

    def test_temp_wav_cleaned_up_on_stt_failure(self):
        adapter = self._make_adapter(app_id="a", client_secret="b")
        self._setup_download_mocks(adapter)
        seen = {}

        async def _raise_transport_error(path):
            seen["wav_path"] = path
            raise httpx.TransportError("boom")

        with mock.patch("tools.url_safety.is_safe_url", return_value=True):
            adapter._call_stt = mock.AsyncMock(side_effect=_raise_transport_error)
            transcript = asyncio.run(
                adapter._stt_voice_attachment(
                    "https://cdn.qq.com/voice.silk",
                    "audio/silk",
                    "voice.silk",
                    voice_wav_url="https://cdn.qq.com/voice.wav",
                )
            )

        assert transcript is None
        assert "wav_path" in seen
        assert not os.path.exists(seen["wav_path"])


# ---------------------------------------------------------------------------
# WebSocket proxy handling
# ---------------------------------------------------------------------------

class TestQQWebSocketProxy:
    @pytest.mark.asyncio
    async def test_open_ws_honors_proxy_env(self, monkeypatch):
        from plugins.platforms.qqbot.adapter import QQAdapter

        for key in (
            "WSS_PROXY",
            "wss_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

        adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))

        seen_session_kwargs = {}
        seen_ws_kwargs = {}

        class FakeSession:
            def __init__(self, **kwargs):
                seen_session_kwargs.update(kwargs)
                self.closed = False

            async def close(self):
                self.closed = True

            async def ws_connect(self, *args, **kwargs):
                seen_ws_kwargs.update(kwargs)
                return mock.AsyncMock(closed=False)

        with mock.patch("plugins.platforms.qqbot.adapter.aiohttp.ClientSession", side_effect=FakeSession):
            await adapter._open_ws("wss://api.sgroup.qq.com/websocket")

        assert seen_session_kwargs.get("trust_env") is True
        assert seen_ws_kwargs.get("proxy") == "http://127.0.0.1:7897"

# ---------------------------------------------------------------------------
# _strip_at_mention
# ---------------------------------------------------------------------------

class TestStripAtMention:
    def _fn(self, content):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter._strip_at_mention(content)

    def test_removes_mention(self):
        result = self._fn("@BotUser hello there")
        assert result == "hello there"


# ---------------------------------------------------------------------------
# _is_dm_allowed
# ---------------------------------------------------------------------------

class TestDmAllowed:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))


    def test_open_policy_with_opt_in(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        adapter = self._make_adapter(app_id="a", client_secret="b", dm_policy="open")
        assert adapter._is_dm_allowed("any_user") is True
        assert adapter._is_dm_intake_allowed("any_user") is True


    def test_allowlist_match(self):
        adapter = self._make_adapter(app_id="a", client_secret="b", dm_policy="allowlist", allow_from="user1,user2")
        assert adapter._is_dm_allowed("user1") is True


# ---------------------------------------------------------------------------
# _is_group_allowed
# ---------------------------------------------------------------------------

class TestGroupAllowed:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))


    def test_allowlist_match(self):
        adapter = self._make_adapter(app_id="a", client_secret="b", group_policy="allowlist", group_allow_from="grp1")
        assert adapter._is_group_allowed("grp1", "user1") is True


    def test_pairing_default_blocks_groups(self):
        adapter = self._make_adapter(app_id="a", client_secret="b")
        assert adapter._group_policy == "pairing"
        assert adapter._is_group_allowed("grp1", "user1") is False


# ---------------------------------------------------------------------------
# _resolve_stt_config
# ---------------------------------------------------------------------------

class TestResolveSTTConfig:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))

    def test_no_config(self):
        adapter = self._make_adapter(app_id="a", client_secret="b")
        with mock.patch.dict(os.environ, {}, clear=True):
            assert adapter._resolve_stt_config() is None

    def test_call_stt_posts_with_configured_timeout(self, tmp_path):
        """The configured ``stt.timeout`` reaches the STT HTTP request; default 60s, not the
        old fixed 30s (#112939). Drives ``_call_stt`` so a regression at the call-site is caught."""
        wav = tmp_path / "v.wav"
        wav.write_bytes(b"RIFF")
        posted = []

        class _FakeClient:
            async def post(self, url, **kwargs):
                posted.append(kwargs)
                return httpx.Response(200, json={"text": "hi"}, request=httpx.Request("POST", url))

        with mock.patch.dict(os.environ, {}, clear=True):
            for stt, expected in (({"apiKey": "k", "provider": "zai"}, 60.0),
                                  ({"apiKey": "k", "provider": "zai", "timeout": "95"}, 95.0)):
                adapter = self._make_adapter(app_id="a", client_secret="b", stt=stt)
                adapter._http_client = _FakeClient()
                assert asyncio.run(adapter._call_stt(str(wav))) == "hi"
                assert posted[-1]["timeout"] == expected


# ---------------------------------------------------------------------------
# _detect_message_type
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# QQCloseError
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _dispatch_payload
# ---------------------------------------------------------------------------

class TestDispatchPayload:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(**extra))
        return adapter

    def test_unknown_op(self):
        adapter = self._make_adapter(app_id="a", client_secret="b")
        # Should not raise
        adapter._dispatch_payload({"op": 99, "d": {}})
        # last_seq should remain None
        assert adapter._last_seq is None


    def test_seq_increments(self):
        adapter = self._make_adapter(app_id="a", client_secret="b")
        adapter._dispatch_payload({"op": 0, "t": "READY", "s": 5, "d": {}})
        adapter._dispatch_payload({"op": 0, "t": "SOME_EVENT", "s": 10, "d": {}})
        assert adapter._last_seq == 10


# ---------------------------------------------------------------------------
# READY / RESUMED handling
# ---------------------------------------------------------------------------

class TestReadyHandling:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))

    def test_ready_stores_session(self):
        adapter = self._make_adapter(app_id="a", client_secret="b")
        adapter._dispatch_payload({
            "op": 0, "t": "READY",
            "s": 1,
            "d": {"session_id": "sess_abc123"},
        })
        assert adapter._session_id == "sess_abc123"


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------

class TestParseJson:
    def _fn(self, raw):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter._parse_json(raw)


    def test_invalid_json(self):
        result = self._fn("not json")
        assert result is None


# ---------------------------------------------------------------------------
# _build_text_body
# ---------------------------------------------------------------------------

class TestBuildTextBody:
    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))

    def test_plain_text(self):
        adapter = self._make_adapter(app_id="a", client_secret="b", markdown_support=False)
        body = adapter._build_text_body("hello world")
        assert body["msg_type"] == 0  # MSG_TYPE_TEXT
        assert body["content"] == "hello world"

    def test_markdown_text(self):
        adapter = self._make_adapter(app_id="a", client_secret="b", markdown_support=True)
        body = adapter._build_text_body("**bold** text")
        assert body["msg_type"] == 2  # MSG_TYPE_MARKDOWN
        assert body["markdown"]["content"] == "**bold** text"


# ---------------------------------------------------------------------------
# _wait_for_reconnection / send reconnection wait
# ---------------------------------------------------------------------------

class TestWaitForReconnection:
    """Test that send() waits for reconnection instead of silently dropping."""

    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(**extra))

    @pytest.mark.asyncio
    async def test_send_waits_and_succeeds_on_reconnect(self):
        """send() should wait for reconnection and then deliver the message."""
        adapter = self._make_adapter(app_id="a", client_secret="b")
        # Initially disconnected
        adapter._running = False
        adapter._http_client = mock.MagicMock()

        # Simulate reconnection after 0.3s (faster than real interval)
        async def fake_api_request(*args, **kwargs):
            return {"id": "msg_123"}

        adapter._api_request = fake_api_request
        adapter._ensure_token = mock.AsyncMock()
        adapter._RECONNECT_POLL_INTERVAL = 0.1
        adapter._RECONNECT_WAIT_SECONDS = 5.0

        # Schedule reconnection after a short delay
        async def reconnect_after_delay():
            await asyncio.sleep(0.2)
            adapter._running = True
            adapter._ws = SimpleNamespace(closed=False)

        asyncio.get_event_loop().create_task(reconnect_after_delay())

        result = await adapter.send("test_openid", "Hello, world!")
        assert result.success
        assert result.message_id == "msg_123"


# ---------------------------------------------------------------------------
# Regression for #78183: httpx timeout empty-string defeats _is_timeout_error
# ---------------------------------------------------------------------------

class TestQQTimeoutErrorNormalization:
    """When an httpx timeout has an empty string representation, qqbot's send
    paths must preserve the exception type name so the base-layer timeout guard
    (_is_timeout_error) can still recognise it and suppress the duplicate-
    delivery plain-text fallback."""

    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b", **extra))

    @pytest.mark.asyncio
    async def test_send_chunk_preserves_read_timeout_type(self, monkeypatch):
        from gateway.platforms.base import BasePlatformAdapter
        import plugins.platforms.qqbot.adapter as qq_adapter_mod

        # Skip the real 1s+2s retry backoff.
        monkeypatch.setattr(qq_adapter_mod.asyncio, "sleep", mock.AsyncMock())
        adapter = self._make_adapter()

        async def _boom(*args, **kwargs):
            raise httpx.ReadTimeout("")

        adapter._send_c2c_text = _boom

        result = await adapter._send_chunk("test_openid", "hello world")

        assert not result.success
        assert result.error, "error must not be empty"
        assert BasePlatformAdapter._is_timeout_error(result.error), (
            f"_is_timeout_error must recognise {result.error!r}"
        )



# ---------------------------------------------------------------------------
# ChunkedUploader
# ---------------------------------------------------------------------------





class TestChunkedUploadHelpers:

    def test_read_chunk_short_read_raises(self, tmp_path):
        from plugins.platforms.qqbot.chunked_upload import _read_file_chunk
        f = tmp_path / "x.bin"
        f.write_bytes(b"hi")
        with pytest.raises(IOError):
            _read_file_chunk(str(f), 0, 100)


    def test_parse_prepare_response_wrapped_in_data(self):
        from plugins.platforms.qqbot.chunked_upload import _parse_prepare_response
        raw = {
            "data": {
                "upload_id": "uid-42",
                "block_size": 4096,
                "parts": [
                    {"part_index": 1, "presigned_url": "https://cos/1", "block_size": 4096},
                    {"index": 2, "url": "https://cos/2"},
                ],
                "concurrency": 3,
                "retry_timeout": 90,
            }
        }
        r = _parse_prepare_response(raw)
        assert r.upload_id == "uid-42"
        assert r.block_size == 4096
        assert len(r.parts) == 2
        assert r.parts[0].presigned_url == "https://cos/1"
        assert r.parts[1].index == 2
        assert r.concurrency == 3
        assert r.retry_timeout == 90.0


class TestChunkedUploaderFlow:
    """End-to-end prepare / PUT / part_finish / complete flow with mocked HTTP.

    Verifies the state machine matches the QQ v2 contract without hitting the network.
    """

    @pytest.mark.asyncio
    async def test_full_upload_two_parts_success(self, tmp_path):
        from plugins.platforms.qqbot.chunked_upload import ChunkedUploader

        # Two-part file.
        f = tmp_path / "vid.mp4"
        f.write_bytes(b"A" * 5_000_000 + b"B" * 3_000_000)

        # Mock api_request — handles prepare, part_finish, complete based on URL.
        api_calls = []

        async def fake_api_request(method, path, *, body=None, timeout=None):
            api_calls.append((method, path, body))
            if path.endswith("/upload_prepare"):
                return {
                    "upload_id": "uid-xyz",
                    "block_size": 5_000_000,
                    "parts": [
                        {"part_index": 1, "presigned_url": "https://cos.example/p1"},
                        {"part_index": 2, "presigned_url": "https://cos.example/p2"},
                    ],
                    "concurrency": 1,
                }
            if path.endswith("/upload_part_finish"):
                return {}
            # complete
            return {"file_info": "FILEINFO_TOKEN", "file_uuid": "u-1"}

        # Mock http_put — always returns 200.
        put_calls = []

        class _FakeResp:
            status_code = 200
            text = ""

        async def fake_put(url, data=None, headers=None):
            put_calls.append((url, len(data), headers))
            return _FakeResp()

        uploader = ChunkedUploader(
            api_request=fake_api_request,
            http_put=fake_put,
            log_tag="QQBot:TEST",
        )
        result = await uploader.upload(
            chat_type="c2c",
            target_id="user-openid-1",
            file_path=str(f),
            file_type=2,  # MEDIA_TYPE_VIDEO
            file_name="vid.mp4",
        )

        assert result["file_info"] == "FILEINFO_TOKEN"
        # Two PUTs, one per part.
        assert len(put_calls) == 2
        assert put_calls[0][0] == "https://cos.example/p1"
        assert put_calls[1][0] == "https://cos.example/p2"
        # Prepare + 2 part_finish + complete = 4 api calls.
        assert len(api_calls) == 4
        assert api_calls[0][1].endswith("/upload_prepare")
        assert api_calls[1][1].endswith("/upload_part_finish")
        assert api_calls[2][1].endswith("/upload_part_finish")
        # complete path reuses /files.
        assert api_calls[3][1].endswith("/files")
        assert api_calls[3][2] == {"upload_id": "uid-xyz"}

    @pytest.mark.asyncio
    async def test_group_paths(self, tmp_path):
        """Group uploads hit /v2/groups/... instead of /v2/users/..."""
        from plugins.platforms.qqbot.chunked_upload import ChunkedUploader

        f = tmp_path / "a.bin"
        f.write_bytes(b"x" * 100)

        seen_paths = []

        async def fake_api_request(method, path, *, body=None, timeout=None):
            seen_paths.append(path)
            if path.endswith("/upload_prepare"):
                return {
                    "upload_id": "gid-1",
                    "block_size": 100,
                    "parts": [{"part_index": 1, "presigned_url": "https://cos/g1"}],
                }
            if path.endswith("/upload_part_finish"):
                return {}
            return {"file_info": "GFILE"}

        class _R:
            status_code = 200
            text = ""

        async def fake_put(url, data=None, headers=None):
            return _R()

        u = ChunkedUploader(fake_api_request, fake_put, "QQBot:T")
        await u.upload(
            chat_type="group",
            target_id="grp-openid-1",
            file_path=str(f),
            file_type=4,
            file_name="a.bin",
        )
        assert all("/v2/groups/" in p for p in seen_paths)
        assert any(p.endswith("/upload_prepare") for p in seen_paths)
        assert any(p.endswith("/files") for p in seen_paths)


# ---------------------------------------------------------------------------
# Inline keyboards — approval + update-prompt flows
# ---------------------------------------------------------------------------

class TestApprovalButtonData:
    def test_parse_allow_once(self):
        from plugins.platforms.qqbot.keyboards import parse_approval_button_data
        result = parse_approval_button_data("approve:agent:main:qqbot:c2c:UID:allow-once")
        assert result == ("agent:main:qqbot:c2c:UID", "allow-once")


    def test_parse_empty_returns_none(self):
        from plugins.platforms.qqbot.keyboards import parse_approval_button_data
        assert parse_approval_button_data("") is None
        assert parse_approval_button_data(None) is None  # type: ignore[arg-type]


class TestUpdatePromptButtonData:
    def test_parse_yes(self):
        from plugins.platforms.qqbot.keyboards import parse_update_prompt_button_data
        assert parse_update_prompt_button_data("update_prompt:y") == "y"


class TestBuildApprovalKeyboard:

    def test_button_data_embeds_session_key(self):
        from plugins.platforms.qqbot.keyboards import build_approval_keyboard
        kb = build_approval_keyboard("agent:main:qqbot:c2c:UID")
        datas = [b.action.data for b in kb.content.rows[0].buttons]
        assert datas[0] == "approve:agent:main:qqbot:c2c:UID:allow-once"
        assert datas[1] == "approve:agent:main:qqbot:c2c:UID:allow-always"
        assert datas[2] == "approve:agent:main:qqbot:c2c:UID:deny"




class TestBuildApprovalText:


    def test_truncates_long_commands(self):
        from plugins.platforms.qqbot.keyboards import (
            ApprovalRequest, build_approval_text,
        )
        long = "x" * 1000
        req = ApprovalRequest(
            session_key="s", title="t", command_preview=long, cwd="/x",
        )
        text = build_approval_text(req)
        # Preview is truncated to 300 chars; 1000 "x"s would still push the
        # body past 300, but the inline preview specifically must be capped.
        # 2 backtick fences; the content line in between is separate.
        xs_in_preview = sum(line.count("x") for line in text.split("\n") if line and "```" not in line)
        assert xs_in_preview <= 301  # 300 xs + one-off tolerance


class TestInteractionEventParsing:
    def test_parse_c2c_interaction(self):
        from plugins.platforms.qqbot.keyboards import parse_interaction_event
        raw = {
            "id": "interaction-42",
            "chat_type": 2,
            "user_openid": "user-1",
            "data": {
                "type": 11,
                "resolved": {
                    "button_data": "approve:sess:allow-once",
                    "button_id": "allow",
                },
            },
        }
        ev = parse_interaction_event(raw)
        assert ev.id == "interaction-42"
        assert ev.scene == "c2c"
        assert ev.chat_type == 2
        assert ev.user_openid == "user-1"
        assert ev.button_data == "approve:sess:allow-once"
        assert ev.button_id == "allow"
        assert ev.operator_openid == "user-1"


class TestAdapterInteractionDispatch:
    """End-to-end verification of _on_interaction including ACK + callback."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    @pytest.mark.asyncio
    async def test_callback_invoked_with_parsed_event(self):
        adapter = self._make_adapter()

        # Stub ACK so we don't require a live http_client.
        ack_calls = []

        async def fake_ack(interaction_id, code=0):
            ack_calls.append((interaction_id, code))

        adapter._acknowledge_interaction = fake_ack  # type: ignore[assignment]

        received = []

        async def cb(event):
            received.append(event)

        adapter.set_interaction_callback(cb)
        await adapter._on_interaction({
            "id": "i-1",
            "chat_type": 2,
            "user_openid": "user-1",
            "data": {
                "type": 11,
                "resolved": {"button_data": "approve:agent:main:qqbot:c2c:u:deny", "button_id": "deny"},
            },
        })

        assert len(ack_calls) == 1
        assert ack_calls[0][0] == "i-1"
        assert len(received) == 1
        assert received[0].button_data == "approve:agent:main:qqbot:c2c:u:deny"
        assert received[0].scene == "c2c"


# ---------------------------------------------------------------------------
# Quoted-message handling (message_type=103 → msg_elements)
# ---------------------------------------------------------------------------

class TestProcessQuotedContext:
    """Verify the quoted-message pipeline: text + voice STT + images + files."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    @pytest.mark.asyncio
    async def test_non_quote_message_returns_empty(self):
        adapter = self._make_adapter()
        d = {"message_type": 0, "content": "hi"}
        out = await adapter._process_quoted_context(d)
        assert out == {"quote_block": "", "image_urls": [], "image_media_types": []}


    @pytest.mark.asyncio
    async def test_quote_with_voice_attachment_runs_stt(self):
        adapter = self._make_adapter()

        # Capture what attachments are passed into _process_attachments.
        captured = []

        async def fake_process(atts):
            captured.append(atts)
            return {
                "image_urls": [],
                "image_media_types": [],
                "voice_transcripts": ["[Voice] hello from the quoted audio"],
                "attachment_info": "",
            }

        adapter._process_attachments = fake_process  # type: ignore[assignment]

        d = {
            "message_type": 103,
            "msg_elements": [{
                "content": "",
                "attachments": [
                    {"content_type": "audio/silk",
                     "url": "https://qq-cdn/x.silk",
                     "filename": "rec.silk"}
                ],
            }],
        }
        out = await adapter._process_quoted_context(d)

        # The quoted voice attachment must actually flow through STT.
        assert captured and len(captured[0]) == 1
        assert captured[0][0]["content_type"] == "audio/silk"
        assert "hello from the quoted audio" in out["quote_block"]
        # Quoted content is media/data, so the block must carry the
        # data-not-instructions marker.
        assert "NOT as instructions" in out["quote_block"]


    @pytest.mark.asyncio
    async def test_multiple_elements_concatenated(self):
        adapter = self._make_adapter()

        async def fake_process(atts):
            assert len(atts) == 2
            return {
                "image_urls": [], "image_media_types": [],
                "voice_transcripts": [], "attachment_info": "",
            }

        adapter._process_attachments = fake_process  # type: ignore[assignment]

        d = {
            "message_type": 103,
            "msg_elements": [
                {"content": "first", "attachments": [{"content_type": "image/png", "url": "a"}]},
                {"content": "second", "attachments": [{"content_type": "image/png", "url": "b"}]},
            ],
        }
        out = await adapter._process_quoted_context(d)
        assert "first" in out["quote_block"]
        assert "second" in out["quote_block"]




# ---------------------------------------------------------------------------
# Gateway-contract approval UX — send_exec_approval + default dispatcher
# ---------------------------------------------------------------------------

class TestDefaultInteractionDispatch:
    """Verify the adapter's default INTERACTION_CREATE router."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_default_callback_installed_on_init(self):
        """Fresh adapter has a working default interaction callback."""
        adapter = self._make_adapter()
        assert adapter._interaction_callback is not None
        assert adapter._interaction_callback == adapter._default_interaction_dispatch


    @pytest.mark.asyncio
    async def test_approval_click_once_maps_to_once(self):
        """'allow-once' button → resolve_gateway_approval(session, 'once')."""
        adapter = self._make_adapter()

        resolve_calls = []

        def fake_resolve(session_key, choice, resolve_all=False):
            resolve_calls.append((session_key, choice, resolve_all))
            return 1

        # Patch the *module-level* function that _default_interaction_dispatch
        # imports lazily.
        import tools.approval
        orig = tools.approval.resolve_gateway_approval
        tools.approval.resolve_gateway_approval = fake_resolve
        try:
            from plugins.platforms.qqbot.keyboards import parse_interaction_event
            event = parse_interaction_event({
                "id": "i",
                "chat_type": 2,
                "user_openid": "u-42",
                "data": {"resolved": {"button_data": "approve:agent:main:qqbot:dm:u-42:allow-once"}},
            })
            await adapter._default_interaction_dispatch(event)
        finally:
            tools.approval.resolve_gateway_approval = orig

        assert resolve_calls == [("agent:main:qqbot:dm:u-42", "once", False)]


    @pytest.mark.asyncio
    async def test_approval_click_rejects_unauthorized_operator(self):
        adapter = self._make_adapter()
        resolve_calls = []

        def fake_resolve(session_key, choice, resolve_all=False):
            resolve_calls.append((session_key, choice, resolve_all))
            return 1

        import tools.approval
        orig = tools.approval.resolve_gateway_approval
        tools.approval.resolve_gateway_approval = fake_resolve
        try:
            from plugins.platforms.qqbot.keyboards import parse_interaction_event
            event = parse_interaction_event({
                "id": "i", "chat_type": 1,
                "group_openid": "g-1",
                "group_member_openid": "attacker",
                "data": {"resolved": {"button_data": "approve:agent:main:qqbot:group:g-1:owner:allow-once"}},
            })
            await adapter._default_interaction_dispatch(event)
        finally:
            tools.approval.resolve_gateway_approval = orig

        assert resolve_calls == []

    @pytest.mark.asyncio
    async def test_update_prompt_click_writes_response_file(self, tmp_path, monkeypatch):
        """update_prompt:y click writes 'y' to ~/.hermes/.update_response."""
        adapter = self._make_adapter()
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home",
            lambda: hermes_home,
        )

        from plugins.platforms.qqbot.keyboards import parse_interaction_event
        event = parse_interaction_event({
            "id": "i", "chat_type": 2, "user_openid": "u-1",
            "data": {"resolved": {"button_data": "update_prompt:y"}},
        })
        await adapter._default_interaction_dispatch(event)

        response = hermes_home / ".update_response"
        assert response.exists()
        assert response.read_text() == "y"


class TestProfileNamespaceApprovalAuthz:
    """Named-profile (multiplex) session keys must authorize like ``main``.

    ``build_session_key`` namespaces named-profile keys as ``agent:<profile>:...``
    while the default profile keeps the legacy ``agent:main`` prefix
    (``gateway/session.py::_session_key_namespace``). The interaction authz
    parser used to require the literal ``main`` in the namespace slot, so
    every approval button click in a named profile was rejected as
    unauthorized and the pending approval timed out (fail-closed block).
    """

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    @staticmethod
    def _parse(key):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter._parse_gateway_session_key(key)

    def test_parse_accepts_named_profile_namespace(self):
        parsed = self._parse("agent:coder:qqbot:dm:u-1")
        assert parsed is not None
        assert parsed["platform"] == "qqbot"
        assert parsed["chat_type"] == "dm"
        assert parsed["chat_id"] == "u-1"

    def test_parse_accepts_main_namespace_with_user_id(self):
        parsed = self._parse("agent:main:qqbot:group:g-1:owner")
        assert parsed is not None
        assert parsed["platform"] == "qqbot"
        assert parsed["chat_type"] == "group"
        assert parsed["chat_id"] == "g-1"
        assert parsed["user_id"] == "owner"

    def test_parse_still_rejects_non_agent_and_malformed_keys(self):
        assert self._parse("session:main:qqbot:c2c:u-1") is None
        assert self._parse("agent::qqbot:c2c:u-1") is None
        assert self._parse("agent:main") is None
        assert self._parse("") is None

    @pytest.mark.asyncio
    async def test_c2c_click_on_named_profile_key_resolves(self):
        """Approval click carrying a named-profile c2c key resolves (was rejected)."""
        adapter = self._make_adapter()

        resolve_calls = []

        def fake_resolve(session_key, choice, resolve_all=False):
            resolve_calls.append((session_key, choice, resolve_all))
            return 1

        import tools.approval
        orig = tools.approval.resolve_gateway_approval
        tools.approval.resolve_gateway_approval = fake_resolve
        try:
            from plugins.platforms.qqbot.keyboards import parse_interaction_event
            event = parse_interaction_event({
                "id": "i",
                "chat_type": 2,
                "user_openid": "u-42",
                "data": {"resolved": {"button_data": "approve:agent:coder:qqbot:c2c:u-42:allow-once"}},
            })
            await adapter._default_interaction_dispatch(event)
        finally:
            tools.approval.resolve_gateway_approval = orig

        assert resolve_calls == [("agent:coder:qqbot:c2c:u-42", "once", False)]

    @pytest.mark.asyncio
    async def test_group_click_on_named_profile_key_authorizes_session_owner(self):
        """Group approval click under a named profile authorizes the session owner."""
        adapter = self._make_adapter()

        resolve_calls = []

        def fake_resolve(session_key, choice, resolve_all=False):
            resolve_calls.append((session_key, choice, resolve_all))
            return 1

        import tools.approval
        orig = tools.approval.resolve_gateway_approval
        tools.approval.resolve_gateway_approval = fake_resolve
        try:
            from plugins.platforms.qqbot.keyboards import parse_interaction_event
            event = parse_interaction_event({
                "id": "i", "chat_type": 1,
                "group_openid": "g-1",
                "group_member_openid": "owner",
                "data": {"resolved": {"button_data": "approve:agent:coder:qqbot:group:g-1:owner:allow-once"}},
            })
            await adapter._default_interaction_dispatch(event)
        finally:
            tools.approval.resolve_gateway_approval = orig

        assert resolve_calls == [("agent:coder:qqbot:group:g-1:owner", "once", False)]

    @pytest.mark.asyncio
    async def test_named_profile_key_still_rejects_wrong_operator(self):
        """The namespace relaxation must not weaken the operator check."""
        adapter = self._make_adapter()

        resolve_calls = []

        def fake_resolve(session_key, choice, resolve_all=False):
            resolve_calls.append((session_key, choice, resolve_all))
            return 1

        import tools.approval
        orig = tools.approval.resolve_gateway_approval
        tools.approval.resolve_gateway_approval = fake_resolve
        try:
            from plugins.platforms.qqbot.keyboards import parse_interaction_event
            event = parse_interaction_event({
                "id": "i", "chat_type": 1,
                "group_openid": "g-1",
                "group_member_openid": "attacker",
                "data": {"resolved": {"button_data": "approve:agent:coder:qqbot:group:g-1:owner:allow-once"}},
            })
            await adapter._default_interaction_dispatch(event)
        finally:
            tools.approval.resolve_gateway_approval = orig

        assert resolve_calls == []




class TestSendUpdatePrompt:
    """Verify the cross-adapter send_update_prompt signature + behaviour."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    @pytest.mark.asyncio
    async def test_delegates_to_send_with_keyboard(self):
        adapter = self._make_adapter()

        captured = {}

        async def fake_swk(chat_id, content, keyboard, reply_to=None):
            from gateway.platforms.base import SendResult
            captured["chat_id"] = chat_id
            captured["content"] = content
            captured["keyboard"] = keyboard
            captured["reply_to"] = reply_to
            return SendResult(success=True, message_id="mid")

        adapter.send_with_keyboard = fake_swk  # type: ignore[assignment]
        adapter._last_msg_id["u1"] = "prev-msg"

        result = await adapter.send_update_prompt(
            chat_id="u1", prompt="Continue with update?",
            default="y", session_key="ignored", metadata={"x": 1},
        )
        assert result.success
        assert "Continue with update?" in captured["content"]
        assert captured["reply_to"] == "prev-msg"
        # Keyboard has the Yes/No buttons.
        dd = captured["keyboard"].to_dict()
        datas = [b["action"]["data"] for b in dd["content"]["rows"][0]["buttons"]]
        assert datas == ["update_prompt:y", "update_prompt:n"]


# ---------------------------------------------------------------------------
# _send_identify includes INTERACTION intent
# ---------------------------------------------------------------------------

class TestIdentifyIntents:
    """Verify the WebSocket identify payload includes the INTERACTION intent bit."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    @pytest.mark.asyncio
    async def test_intents_include_interaction_bit(self):
        adapter = self._make_adapter()

        # Mock token retrieval and WebSocket
        adapter._access_token = "fake_token"
        adapter._token_expires_at = 9999999999.0

        sent_payloads = []

        class FakeWS:
            closed = False

            async def send_json(self, payload):
                sent_payloads.append(payload)

        adapter._ws = FakeWS()
        await adapter._send_identify()

        assert len(sent_payloads) == 1
        intents = sent_payloads[0]["d"]["intents"]

        # Verify all expected intent bits are present
        assert intents & (1 << 25), "GROUP_MESSAGES (1<<25) missing"
        assert intents & (1 << 30), "GUILD_AT_MESSAGE (1<<30) missing"
        assert intents & (1 << 12), "DIRECT_MESSAGES (1<<12) missing"
        assert intents & (1 << 26), "INTERACTION (1<<26) missing"
        assert intents & (1 << 24), "GROUP_MEMBER_EVENT (1<<24, GROUP_JOIN_REQUEST) missing"


class TestGroupJoinRequest:
    """GROUP_JOIN_REQUEST (intent 1<<24): report to the operator, never auto-approve."""

    class _Ok:
        success = True
        error = None

    def _make(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b", **extra))

    def test_format_includes_applicant_fields(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        text = QQAdapter._format_join_request({
            "group_openid": "G1", "member_openid": "M1", "username": "小明",
            "apply_source": "invited", "invited_by": "M0", "apply_at": "2026-10-03T12:00:00+08:00",
            "verify_info": {"method": "verify_message", "verify_message": "放我进去"},
            "join_request_id": "JR1"})
        assert "小明" in text and "M1" in text and "放我进去" in text and "M0" in text

    @pytest.mark.asyncio
    async def test_notice_goes_to_operator_and_never_auto_approves(self):
        adapter = self._make(allow_admin_from=["OP1"])
        sent = []

        async def fake_send(chat_id, content, **kw):
            sent.append((chat_id, content))
            return self._Ok()

        adapter.send = fake_send
        await adapter._on_group_join_request({
            "group_openid": "G1", "member_openid": "M1", "username": "小明",
            "apply_source": "self_apply", "join_request_id": "JR1"})
        assert [chat for chat, _ in sent] == ["OP1"]
        assert "入群申请" in sent[0][1]

    @pytest.mark.asyncio
    async def test_no_operator_target_is_a_silent_noop(self):
        adapter = self._make()
        called = []

        async def fake_send(*args, **kw):
            called.append(args)
            return self._Ok()

        adapter.send = fake_send
        with mock.patch.dict(os.environ, {"QQ_ALLOWED_USERS": ""}, clear=False):
            await adapter._on_group_join_request({"group_openid": "G1", "member_openid": "M1"})
        assert called == []


class TestProactiveSwitchEvents:
    """C2C_MSG_REJECT / C2C_MSG_RECEIVE: log-only visibility for the proactive-message switch."""

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_reject_logs_a_warning(self, caplog):
        adapter = self._make()
        with caplog.at_level(logging.WARNING):
            adapter._dispatch_payload({"op": 0, "t": "C2C_MSG_REJECT", "s": 1, "d": {"openid": "U1"}})
        assert any("proactive-message switch DISABLED" in r.getMessage() for r in caplog.records)

    def test_receive_is_an_info_line(self, caplog):
        adapter = self._make()
        with caplog.at_level(logging.INFO):
            adapter._dispatch_payload({"op": 0, "t": "C2C_MSG_RECEIVE", "s": 2, "d": {"openid": "U1"}})
        assert any("proactive-message switch enabled" in r.getMessage() for r in caplog.records)


class TestQuotedReply:
    """message_reference: a reply quotes its triggering message (message_scene.ext msg_idx)."""

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_extract_msg_idx(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        assert QQAdapter._extract_msg_idx(
            {"message_scene": {"source": "default", "ext": ["auth_token=x", "msg_idx=REFIDX_ab=="]}}
        ) == "REFIDX_ab=="
        assert QQAdapter._extract_msg_idx({"message_scene": {"ext": ["ref_msg_idx=REFIDX_x=="]}}) == ""
        assert QQAdapter._extract_msg_idx({"message_scene": "nope"}) == ""
        assert QQAdapter._extract_msg_idx(None) == ""

    @pytest.mark.asyncio
    async def test_group_reply_carries_message_reference(self):
        adapter = self._make()
        adapter._remember_quote_ref("MID1", "REFIDX_1==")
        cap = {}

        async def fake_post(path, body):
            cap["path"], cap["body"] = path, body
            return SimpleNamespace(success=True, message_id="M1", error=None)

        adapter._post_message = fake_post
        await adapter._send_text_to("group", "G1", "hi", reply_to="MID1")
        assert cap["body"]["msg_id"] == "MID1"
        assert cap["body"]["message_reference"] == {"message_id": "REFIDX_1=="}

    @pytest.mark.asyncio
    async def test_c2c_reply_never_carries_reference(self):
        """DMs are 1:1 — a quoted header is noise, and C2C stays on the plain path."""
        adapter = self._make()
        adapter._remember_quote_ref("MID1", "REFIDX_1==")
        cap = {}

        async def fake_post(path, body):
            cap["path"], cap["body"] = path, body
            return SimpleNamespace(success=True, message_id="M1", error=None)

        adapter._post_message = fake_post
        await adapter._send_text_to("c2c", "U1", "hi", reply_to="MID1")
        assert cap["body"]["msg_id"] == "MID1"
        assert "message_reference" not in cap["body"]

    @pytest.mark.asyncio
    async def test_unknown_reply_target_gets_no_reference(self):
        adapter = self._make()
        cap = {}

        async def fake_post(path, body):
            cap["body"] = body
            return SimpleNamespace(success=True, message_id="M1", error=None)

        adapter._post_message = fake_post
        await adapter._send_text_to("c2c", "U1", "hi", reply_to="MID_UNKNOWN")
        assert "message_reference" not in cap["body"]

    @pytest.mark.asyncio
    async def test_proactive_send_is_never_quoted(self):
        adapter = self._make()
        adapter._remember_quote_ref("MID1", "REFIDX_1==")
        cap = {}

        async def fake_post(path, body):
            cap["body"] = body
            return SimpleNamespace(success=True, message_id="M1", error=None)

        adapter._post_message = fake_post
        await adapter._send_text_to("c2c", "U1", "notice")  # no reply_to → proactive
        assert "message_reference" not in cap["body"]

    def test_quote_refs_are_fifo_capped(self):
        adapter = self._make()
        adapter._quote_refs_max = 3
        for i in range(5):
            adapter._remember_quote_ref(f"M{i}", f"R{i}")
        assert list(adapter._quote_refs) == ["M2", "M3", "M4"]


class TestNativeStreaming:
    """send_stream_frame: grow ONE QQ C2C message; hold short answers; never break sends."""

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))
        adapter._last_msg_id["U1"] = "MID1"
        adapter._chat_type_map["U1"] = "c2c"
        adapter._chat_type_map["G1"] = "group"
        calls = []

        async def fake_api(method, path, body=None):
            calls.append((method, path, dict(body or {})))
            return {"id": "SID1", "timestamp": "", "ext_info": {}}

        adapter._api_request = fake_api
        return adapter, calls

    def test_probe_is_c2c_only(self):
        adapter, _ = self._make()
        assert adapter.supports_native_streaming(chat_id="U1") is True
        assert adapter.supports_native_streaming(chat_id="G1") is False

    @pytest.mark.asyncio
    async def test_empty_seed_is_swallowed(self):
        adapter, calls = self._make()
        assert await adapter.send_stream_frame("", chat_id="U1", turn_id="t1") is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_short_answer_never_opens_a_stream(self):
        adapter, calls = self._make()
        assert await adapter.send_stream_frame("短！", chat_id="U1", turn_id="t1") is True
        # short final → False hands it to the normal send path (one ordinary message)
        assert await adapter.send_stream_frame("短！", finalize=True, chat_id="U1", turn_id="t1") is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_long_answer_streams_keeping_msg_id_on_every_frame(self):
        adapter, calls = self._make()
        first = "长" * 300
        assert await adapter.send_stream_frame(first, chat_id="U1", reply_to="MID1", turn_id="t1") is True
        method, path, b0 = calls[0]
        assert (method, path) == ("POST", "/v2/users/U1/stream_messages")
        assert b0["msg_id"] == "MID1" and "stream_msg_id" not in b0
        assert b0["index"] == 0 and b0["input_state"] == 1 and b0["content_type"] == "markdown"
        assert await adapter.send_stream_frame(first + "加长", finalize=True, chat_id="U1",
                                               reply_to="MID1", turn_id="t1") is True
        b1 = calls[1][2]
        assert b1["stream_msg_id"] == "SID1" and b1["index"] == 1 and b1["input_state"] == 10
        assert b1["msg_id"] == "MID1", "every frame must keep the passive credential"

    @pytest.mark.asyncio
    async def test_group_is_never_streamed(self):
        adapter, calls = self._make()
        assert await adapter.send_stream_frame("长" * 300, chat_id="G1", turn_id="t1") is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_frame_error_returns_false_and_clears_state(self):
        adapter, _ = self._make()

        async def boom(method, path, body=None):
            raise RuntimeError("QQ Bot API error [500] 系统繁忙")

        adapter._api_request = boom
        assert await adapter.send_stream_frame("长" * 300, chat_id="U1", turn_id="t1") is False
        assert adapter._streams == {}

    @pytest.mark.asyncio
    async def test_stream_text_is_tag_neutralized(self):
        adapter, calls = self._make()
        body = "长" * 300 + ' 参考 <qqbot-at-user id="X" /> 这个标签'
        assert await adapter.send_stream_frame(body, chat_id="U1", turn_id="t1") is True
        assert "<qqbot-at-user" not in calls[0][2]["content_raw"]


class TestGroupMembership:
    """GROUP_ADD_ROBOT: greet the new group via event_id (no active-message permission)."""

    def _make(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(app_id="a", client_secret="b", **extra))
        calls = []

        async def fake_api(method, path, body=None):
            calls.append((method, path, dict(body or {})))
            return {"id": "MID9"}

        adapter._api_request = fake_api
        return adapter, calls

    @pytest.mark.asyncio
    async def test_greets_allowlisted_group_with_event_id(self):
        adapter, calls = self._make(group_policy="allowlist", group_allow_from=["G1"])
        await adapter._on_group_membership("GROUP_ADD_ROBOT", {"group_openid": "G1"}, "EV1")
        method, path, body = calls[0]
        assert (method, path) == ("POST", "/v2/groups/G1/messages")
        assert body["event_id"] == "EV1"           # passive reply → no active-message permission
        assert body["msg_type"] == 0
        assert "tamako" in body["content"]
        assert "http" not in body["content"]       # 群消息不允许发 URL（40054010）

    @pytest.mark.asyncio
    async def test_non_allowlisted_group_stays_silent(self):
        adapter, calls = self._make(group_policy="allowlist", group_allow_from=["G1"])
        await adapter._on_group_membership("GROUP_ADD_ROBOT", {"group_openid": "G_OTHER"}, "EV1")
        assert calls == []

    @pytest.mark.asyncio
    async def test_removal_only_logs(self):
        adapter, calls = self._make(group_policy="allowlist", group_allow_from=["G1"])
        await adapter._on_group_membership("GROUP_DEL_ROBOT", {"group_openid": "G1"}, "EV2")
        assert calls == []

    @pytest.mark.asyncio
    async def test_missing_ids_skip_the_greeting(self):
        adapter, calls = self._make(group_policy="open")
        await adapter._on_group_membership("GROUP_ADD_ROBOT", {}, None)
        assert calls == []

    @pytest.mark.asyncio
    async def test_greeting_failure_is_swallowed(self):
        adapter, _ = self._make(group_policy="open")

        async def boom(method, path, body=None):
            raise RuntimeError("QQ Bot API error [400] 主动消息发送失败，无权限")

        adapter._api_request = boom
        await adapter._on_group_membership("GROUP_ADD_ROBOT", {"group_openid": "G1"}, "EV1")


class TestGroupRecall:
    """Admin-only 「撤回上一条」 in groups: the adapter deletes, the agent just narrates."""

    ADMIN = "0123456789ABCDEF0123456789ABCDEF"
    OTHER = "SOMEONE_ELSE_OPENID"

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(
            app_id="a", client_secret="b", group_policy="allowlist", group_allow_from=["G1"],
            group_allow_admin_from=[self.ADMIN]))
        deletes = []

        class Resp:
            status_code = 200
            text = ""

        class Client:
            async def request(self, method, url, headers=None, timeout=None):
                deletes.append((method, url))
                return Resp()

        adapter._require_http_client = lambda: Client()

        async def fake_headers():
            return {}

        adapter._auth_headers = fake_headers
        return adapter, deletes

    def test_trigger_patterns(self):
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        assert A._RECALL_PREV_RE.search("撤回上一条")
        assert A._RECALL_PREV_RE.search("把刚才那条撤掉")
        assert A._RECALL_OWN_RE.search("撤回你自己那条")
        assert not A._RECALL_PREV_RE.search("今天天气不错")
        assert not A._RECALL_OWN_RE.search("撤回上一条")

    @pytest.mark.asyncio
    async def test_prev_recall_targets_the_message_before_the_instruction(self):
        adapter, deletes = self._make()
        adapter._group_recent["G1"] = [("M0", "x"), ("M_SPAM", "spammer")]
        note = await adapter._maybe_recall_from_text("G1", "撤回上一条", self.ADMIN, "M_INSTR")
        from plugins.platforms.qqbot.constants import API_BASE
        assert deletes == [("DELETE", f"{API_BASE}/v2/groups/G1/messages/M_SPAM")]
        assert "已按前辈的话撤回" in note

    @pytest.mark.asyncio
    async def test_own_recall_uses_the_last_sent_id(self):
        adapter, deletes = self._make()
        adapter._last_sent_id["G1"] = "M_MINE"
        note = await adapter._maybe_recall_from_text("G1", "撤回你自己那条", self.ADMIN, "M_INSTR")
        assert deletes and deletes[0][1].endswith("/messages/M_MINE")
        assert "我自己上一条" in note

    @pytest.mark.asyncio
    async def test_non_admin_cannot_trigger(self):
        adapter, deletes = self._make()
        adapter._group_recent["G1"] = [("M_SPAM", "spammer")]
        assert await adapter._maybe_recall_from_text("G1", "撤回上一条", self.OTHER, "M_INSTR") == ""
        assert deletes == []

    @pytest.mark.asyncio
    async def test_plain_chat_is_ignored(self):
        adapter, deletes = self._make()
        assert await adapter._maybe_recall_from_text("G1", "前辈今天忙吗", self.ADMIN, "M") == ""
        assert deletes == []

    @pytest.mark.asyncio
    async def test_recall_failure_is_reported_in_the_note(self):
        adapter, _ = self._make()
        adapter._last_sent_id["G1"] = "M_MINE"

        class Resp:
            status_code = 400
            text = '{"message":"已超出消息撤回时限","code":40064004}'

        class Client:
            async def request(self, method, url, headers=None, timeout=None):
                return Resp()

        adapter._require_http_client = lambda: Client()
        note = await adapter._maybe_recall_from_text("G1", "撤回你自己那条", self.ADMIN, "M")
        assert "失败" in note and "40064004" in note

    @pytest.mark.asyncio
    async def test_no_target_reports_honestly(self):
        adapter, deletes = self._make()
        note = await adapter._maybe_recall_from_text("G1", "撤回上一条", self.ADMIN, "M")
        assert "没有可撤的消息 ID" in note
        assert deletes == []


class TestGroupMute:
    """Admin-only 「把 XXX 禁言 30 分钟」/「解除 XXX 禁言」: adapter acts, agent narrates."""

    ADMIN = "0123456789ABCDEF0123456789ABCDEF"
    OTHER = "SOMEONE_ELSE_OPENID"

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(
            app_id="a", client_secret="b", group_policy="allowlist", group_allow_from=["G1"],
            group_allow_admin_from=[self.ADMIN]))
        calls = []

        class Resp:
            status_code = 200
            text = ""

        class Client:
            async def request(self, method, url, headers=None, json=None, timeout=None):
                calls.append((method, url, json))
                return Resp()

        adapter._require_http_client = lambda: Client()

        async def fake_headers():
            return {}

        adapter._auth_headers = fake_headers
        adapter._group_recent["G1"] = [("M1", "SPAMMER_OPENID", "小广告")]
        return adapter, calls

    def test_duration_parsing(self):
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        assert A._parse_mute_seconds("把张三禁言 30 分钟") == 1800
        assert A._parse_mute_seconds("禁言 2 小时") == 7200
        assert A._parse_mute_seconds("禁言 1天") == 86400
        assert A._parse_mute_seconds("把他禁言了") is None

    @pytest.mark.asyncio
    async def test_mutes_recent_speaker_by_nickname(self):
        adapter, calls = self._make()
        note = await adapter._maybe_mute_from_text("G1", "把 小广告 禁言 30 分钟", self.ADMIN, {})
        method, url, body = calls[0]
        from plugins.platforms.qqbot.constants import API_BASE
        assert (method, url) == ("POST", f"{API_BASE}/v2/groups/G1/restrict_chat_setting")
        member = body["members"][0]
        assert member["op"] == "add" and member["member_openid"] == "SPAMMER_OPENID"
        assert member["mute_expire_at"].endswith("+08:00")
        assert "已按前辈的话把「小广告」禁言到" in note

    @pytest.mark.asyncio
    async def test_quoted_author_wins(self):
        adapter, calls = self._make()
        d = {"message_type": 103, "msg_elements": [{"author": {"member_openid": "QUOTED_OPENID"}}]}
        await adapter._maybe_mute_from_text("G1", "把他禁言 10 分钟", self.ADMIN, d)
        assert calls[0][2]["members"][0]["member_openid"] == "QUOTED_OPENID"

    @pytest.mark.asyncio
    async def test_missing_duration_asks_instead_of_muting(self):
        adapter, calls = self._make()
        note = await adapter._maybe_mute_from_text("G1", "把 小广告 禁言", self.ADMIN, {})
        assert calls == [] and "多久" in note

    @pytest.mark.asyncio
    async def test_unknown_target_does_nothing(self):
        adapter, calls = self._make()
        note = await adapter._maybe_mute_from_text("G1", "把 陌生人 禁言 1 小时", self.ADMIN, {})
        assert calls == [] and "没认出来是谁" in note

    @pytest.mark.asyncio
    async def test_unmute_uses_del_op(self):
        adapter, calls = self._make()
        note = await adapter._maybe_mute_from_text("G1", "解除 小广告 的禁言", self.ADMIN, {})
        member = calls[0][2]["members"][0]
        assert member["op"] == "del" and member["mute_expire_at"] == ""
        assert "解除" in note

    @pytest.mark.asyncio
    async def test_non_admin_stays_out(self):
        adapter, calls = self._make()
        assert await adapter._maybe_mute_from_text("G1", "把 小广告 禁言 30 分钟", self.OTHER, {}) == ""
        assert calls == []

    @pytest.mark.asyncio
    async def test_duration_over_30_days_is_clamped(self):
        adapter, calls = self._make()
        note = await adapter._maybe_mute_from_text("G1", "把 小广告 禁言 90 天", self.ADMIN, {})
        assert "按 30 天算" in note

    @pytest.mark.asyncio
    async def test_at_mention_identifies_the_target(self):
        adapter, calls = self._make()
        d = {"mentions": [{"member_openid": "AT_OPENID", "username": "小广告"}]}
        note = await adapter._maybe_mute_from_text("G1", "把 @小广告 禁言 15 分钟", self.ADMIN, d)
        assert calls[0][2]["members"][0]["member_openid"] == "AT_OPENID"
        assert "「小广告」" in note

    @pytest.mark.asyncio
    async def test_mention_beats_quote(self):
        adapter, calls = self._make()
        d = {"mentions": [{"member_openid": "AT_OPENID", "username": "谁"}],
             "message_type": 103, "msg_elements": [{"author": {"member_openid": "QUOTED_OPENID"}}]}
        await adapter._maybe_mute_from_text("G1", "禁言 5 分钟", self.ADMIN, d)
        assert calls[0][2]["members"][0]["member_openid"] == "AT_OPENID"

    @pytest.mark.asyncio
    async def test_negated_instruction_does_nothing(self):
        adapter, calls = self._make()
        assert await adapter._maybe_mute_from_text("G1", "先别禁言 小广告", self.ADMIN, {}) == ""
        assert calls == []

    @pytest.mark.asyncio
    async def test_mention_as_bare_name_falls_back_to_recent_speaker(self):
        """If the platform hands us only a nickname, resolve it against recent speakers."""
        adapter, calls = self._make()
        await adapter._maybe_mute_from_text("G1", "把他禁言 5 分钟", self.ADMIN, {"mentions": ["小广告"]})
        assert calls[0][2]["members"][0]["member_openid"] == "SPAMMER_OPENID"

    @pytest.mark.asyncio
    async def test_unusable_mention_is_asked_about_not_guessed(self):
        adapter, calls = self._make()
        note = await adapter._maybe_mute_from_text(
            "G1", "禁言 5 分钟", self.ADMIN, {"mentions": [{"username": "陌生人"}]})
        assert calls == [] and "没认出来是谁" in note


class TestGroupMemberChange:
    """GROUP_MEMBER_ADD/REMOVE (intent 1<<24): report to the operator, dedupe WS replays."""

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(
            app_id="a", client_secret="b", group_policy="allowlist", group_allow_from=["G1"],
            allow_admin_from=["OP1"]))
        sent = []

        async def fake_send(chat_id, content, **kw):
            sent.append((chat_id, content))
            return SimpleNamespace(success=True, message_id="M1", error=None)

        adapter.send = fake_send
        return adapter, sent

    @pytest.mark.asyncio
    async def test_new_member_is_reported_to_the_operator(self):
        adapter, sent = self._make()
        await adapter._on_group_member_change("GROUP_MEMBER_ADD", {
            "group_openid": "G1", "member_openid": "NEW_MEMBER", "timestamp": 1784276757})
        assert sent and sent[0][0] == "OP1"
        assert "有人进群了" in sent[0][1] and "NEW_MEMBER" in sent[0][1]

    @pytest.mark.asyncio
    async def test_replay_is_deduped(self):
        adapter, sent = self._make()
        event = {"group_openid": "G1", "member_openid": "NEW_MEMBER", "timestamp": 1784276757}
        await adapter._on_group_member_change("GROUP_MEMBER_ADD", event)
        await adapter._on_group_member_change("GROUP_MEMBER_ADD", dict(event))
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_non_allowlisted_group_is_silent(self):
        adapter, sent = self._make()
        await adapter._on_group_member_change("GROUP_MEMBER_ADD", {
            "group_openid": "G_OTHER", "member_openid": "X", "timestamp": 1})
        assert sent == []

    @pytest.mark.asyncio
    async def test_leaving_is_reported_too(self):
        adapter, sent = self._make()
        await adapter._on_group_member_change("GROUP_MEMBER_REMOVE", {
            "group_openid": "G1", "member_openid": "X", "timestamp": 1})
        assert sent and "退群了" in sent[0][1]

    @pytest.mark.asyncio
    async def test_missing_fields_are_ignored(self):
        adapter, sent = self._make()
        await adapter._on_group_member_change("GROUP_MEMBER_ADD", {"group_openid": "G1"})
        assert sent == []


class TestRelationAndGroupSwitchEvents:
    """FRIEND_ADD/DEL + GROUP_MSG_REJECT/RECEIVE all live on 1<<25 (already subscribed)."""

    def _make(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(
            app_id="a", client_secret="b", group_policy="allowlist", group_allow_from=["G1"],
            allow_admin_from=["OP1"]))
        sent = []

        async def fake_send(chat_id, content, **kw):
            sent.append((chat_id, content))
            return SimpleNamespace(success=True, message_id="M1", error=None)

        adapter.send = fake_send
        return adapter, sent

    @pytest.mark.asyncio
    async def test_friend_add_notifies_operator(self):
        adapter, sent = self._make()
        await adapter._on_friend_change("FRIEND_ADD", {"openid": "SOMEONE", "timestamp": 1784276757})
        assert sent and "添加了 tamako" in sent[0][1] and "SOMEONE" in sent[0][1]

    @pytest.mark.asyncio
    async def test_friend_remove_notifies_operator(self):
        adapter, sent = self._make()
        await adapter._on_friend_change("FRIEND_DEL", {"openid": "SOMEONE", "timestamp": 2})
        assert sent and "删掉了 tamako" in sent[0][1]

    @pytest.mark.asyncio
    async def test_friend_replay_is_deduped(self):
        adapter, sent = self._make()
        event = {"openid": "SOMEONE", "timestamp": 1}
        await adapter._on_friend_change("FRIEND_ADD", event)
        await adapter._on_friend_change("FRIEND_ADD", dict(event))
        assert len(sent) == 1

    def test_group_switch_events_are_logged_not_sent(self, caplog):
        import logging as _logging
        adapter, sent = self._make()
        with caplog.at_level(_logging.WARNING, logger="plugins.platforms.qqbot.adapter"):
            adapter._dispatch_payload({"op": 0, "s": 1, "t": "GROUP_MSG_REJECT",
                                       "d": {"group_openid": "G1"}})
            adapter._dispatch_payload({"op": 0, "s": 2, "t": "GROUP_MSG_RECEIVE",
                                       "d": {"group_openid": "G1"}})
        assert "DISABLED" in caplog.text and sent == []


class TestApiErrorSurfacing:
    """判失败只看 err_code（不信 message）；2xx 也可能带回 error body；trace_id 要留下。"""

    def _adapter_with_response(self, status, body, *, raises=False):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(app_id="a", client_secret="b"))

        class Resp:
            status_code = status
            text = str(body)

            def json(self):
                if raises:
                    raise ValueError("not json")
                return body

        class Client:
            async def request(self, *a, **kw):
                return Resp()

        adapter._require_http_client = lambda: Client()

        async def fake_headers():
            return {}

        adapter._auth_headers = fake_headers
        return adapter

    @pytest.mark.asyncio
    async def test_error_text_carries_err_code_and_trace_id(self):
        adapter = self._adapter_with_response(
            400, {"err_code": 40034005, "message": "回复消息msg_id已过期", "trace_id": "abc123"})
        with pytest.raises(RuntimeError) as exc:
            await adapter._api_request("POST", "/v2/users/U/messages", {})
        text = str(exc.value)
        assert "40034005" in text and "abc123" in text and "[400]" in text

    @pytest.mark.asyncio
    async def test_two_hundred_with_error_body_is_a_failure(self):
        adapter = self._adapter_with_response(200, {"err_code": 304025, "message": "消息被打击"})
        with pytest.raises(RuntimeError) as exc:
            await adapter._api_request("POST", "/v2/users/U/messages", {})
        assert "304025" in str(exc.value)

    @pytest.mark.asyncio
    async def test_async_accepted_audit_is_not_raised(self):
        adapter = self._adapter_with_response(
            202, {"err_code": 304024, "message": "回复消息异步调用成功，等待人工审核"})
        data = await adapter._api_request("POST", "/v2/groups/G/messages", {})
        assert data["err_code"] == 304024

    @pytest.mark.asyncio
    async def test_plain_success_returns_the_body(self):
        adapter = self._adapter_with_response(200, {"id": "M1"})
        assert (await adapter._api_request("POST", "/v2/users/U/messages", {}))["id"] == "M1"

    @pytest.mark.asyncio
    async def test_unparseable_body_on_success_is_tolerated(self):
        adapter = self._adapter_with_response(200, None, raises=True)
        assert await adapter._api_request("GET", "/v2/whatever") == {}


class TestGroupDropIsVisible:
    """A group message blocked by the allowlist must leave a trace — otherwise 'nothing happened'
    is indistinguishable from 'the platform never pushed it'."""

    @pytest.mark.asyncio
    async def test_dropped_inbound_is_logged(self, caplog):
        import logging as _logging
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(
            app_id="a", client_secret="b", group_policy="allowlist", group_allow_from=["G1"]))
        with caplog.at_level(_logging.INFO, logger="plugins.platforms.qqbot.adapter"):
            await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {
                "group_openid": "G_OTHER", "id": "M1", "content": "hi",
                "author": {"member_openid": "MEMBER1"},
                "timestamp": "2026-10-03T15:00:00+08:00"})
        assert "dropped by allowlist" in caplog.text


class TestGroupContentPassthrough:
    """GROUP_AT_MESSAGE_CREATE content arrives without the @bot prefix — do not re-strip it."""

    @pytest.mark.asyncio
    async def test_leading_mention_of_another_member_survives(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(_make_config(
            app_id="a", client_secret="b", group_policy="allowlist", group_allow_from=["G1"]))
        seen = {}

        async def fake_ingest(d, msg_id, content, attachments, timestamp, **kw):
            seen["content"] = content

        adapter._ingest = fake_ingest
        await adapter._handle_group_message(
            {"group_openid": "G1"}, "M1", "@Alice 你看这个", {"member_openid": "X"}, "")
        assert seen["content"] == "@Alice 你看这个"


class TestArkCard:
    """message_type=3 cards: only ADD the fields QQ's own content did not already render."""

    CARD = {"message_type": 3, "ark_data": {
        "ark_type": "miniapp", "ark_name": "小程序", "prompt": "[每日打卡]快来完成今日学习打卡",
        "fields": {"title": "快来完成今日学习打卡", "desc": "连续打卡 7 天",
                   "source": "学习助手", "tag": "微信小程序",
                   "jump_url": "https://example.com/jump"}}}

    def test_renders_fields_when_content_has_none(self):
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        out = A._render_ark_card(self.CARD)
        assert "小程序" in out and "链接: https://example.com/jump" in out and "来源: 学习助手" in out

    def test_skips_fields_the_platform_already_rendered(self):
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        already = "title: 快来完成今日学习打卡\nsource: 学习助手"
        out = A._render_ark_card(self.CARD, already=already)
        assert "标题:" not in out and "来源:" not in out
        assert "链接: https://example.com/jump" in out

    def test_nothing_new_returns_empty(self):
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        card = {"ark_data": {"ark_name": "小程序", "fields": {"title": "X"}}}
        assert A._render_ark_card(card, already="title: X") == ""
        assert A._render_ark_card({"content": "普通消息"}) == ""
        assert A._render_ark_card(None) == ""
        assert A._render_ark_card({"ark_data": "nope"}) == ""

    def test_truncated_content_gets_the_full_value_back(self):
        """A card whose desc the platform cut short (QQ 收藏 notes) should be completed."""
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        full = "bindPort = 7000\nserverAddr = \"127.0.0.1\"\n" * 3
        card = {"ark_data": {"ark_name": "图文H5", "fields": {"desc": full}}}
        out = A._render_ark_card(card, already="desc: " + full[:40] + "…")
        assert "描述: " in out and "serverAddr" in out

    def test_long_values_are_capped(self):
        from plugins.platforms.qqbot.adapter import QQAdapter as A
        card = {"ark_data": {"ark_name": "图文H5", "fields": {"desc": "x" * 5000}}}
        out = A._render_ark_card(card)
        assert "…（原值更长）" in out and len(out) < 900


# ---------------------------------------------------------------------------
# _process_attachments: video/file path exposure
# ---------------------------------------------------------------------------

class TestProcessAttachmentsPathExposure:
    """Verify that video and file attachments include the cached local path."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    @pytest.mark.asyncio
    async def test_video_attachment_includes_path(self):
        adapter = self._make_adapter()

        # Mock _download_and_cache to return a known path
        async def fake_download(url, ct, original_name=""):
            return "/tmp/cache/video_abc123.mp4"

        adapter._download_and_cache = fake_download  # type: ignore[assignment]

        attachments = [
            {
                "content_type": "video/mp4",
                "url": "https://multimedia.nt.qq.com.cn/download/video123",
                "filename": "my_video.mp4",
            }
        ]
        result = await adapter._process_attachments(attachments)

        assert result["image_urls"] == []
        assert result["voice_transcripts"] == []
        info = result["attachment_info"]
        assert "my_video.mp4" in info
        assert "/tmp/cache/video_abc123.mp4" in info




# ---------------------------------------------------------------------------
# WebSocket op 7 (Server Reconnect) and op 9 (Invalid Session)
# ---------------------------------------------------------------------------

class TestOp7ServerReconnect:
    """Verify op 7 triggers WS close (which triggers reconnect in outer loop)."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_op7_closes_websocket(self):
        adapter = self._make_adapter()
        adapter._session_id = "sess_keep"
        adapter._last_seq = 42

        close_called = []

        class FakeWS:
            closed = False

            async def close(self):
                close_called.append(True)

        adapter._ws = FakeWS()
        adapter._dispatch_payload({"op": 7, "d": None})

        # Session should be preserved for Resume
        assert adapter._session_id == "sess_keep"
        assert adapter._last_seq == 42


class TestOp9InvalidSession:
    """Verify op 9 handles resumable vs non-resumable sessions."""

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))


    @pytest.mark.asyncio
    async def test_op9_non_resumable_triggers_ws_close(self):
        adapter = self._make_adapter()
        adapter._session_id = "s"
        adapter._last_seq = 1
        close_called = []

        class FakeWS:
            closed = False

            async def close(self):
                close_called.append(True)
                self.closed = True

        adapter._ws = FakeWS()
        adapter._dispatch_payload({"op": 9, "d": False})
        await asyncio.sleep(0)

        assert close_called == [True]


# ---------------------------------------------------------------------------
# Close code classification
# ---------------------------------------------------------------------------



class TestReadEventsClosedWsGuard:
    """Regression: a closed-but-non-None ws must raise on entry, not return
    normally, so _listen_loop goes through reconnect/backoff instead of
    busy-looping at 100% CPU (issues #31193 / #31771)."""

    def _make_adapter(self, **extra):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b", **extra))

    def test_read_events_raises_when_ws_closed_on_entry(self):
        adapter = self._make_adapter()
        adapter._running = True
        adapter._ws = SimpleNamespace(closed=True)
        with pytest.raises(RuntimeError):
            asyncio.run(adapter._read_events())


# ---------------------------------------------------------------------------
# [tamako-local] toolsets_for_source — group/guild chats get a hardened toolset
# ---------------------------------------------------------------------------


class TestQQAdapterGroupToolsets:
    """A group keeps chat + web + media + ``terminal``; the per-source sandbox policy
    (``terminal.source_policy``) moves a group turn's execution into the docker sandbox,
    so no host-only toolset (file / code_execution / memory / …) is ever handed out."""

    _HOST_ONLY_TOOLSETS = {"file", "code_execution", "computer_use",
                           "memory", "session_search", "browser"}

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_group_and_guild_get_allowlist(self):
        from plugins.platforms.qqbot.adapter import _GROUP_ALLOWED_TOOLSETS
        adapter = self._make_adapter()
        for chat_type in ("group", "guild", "GROUP"):
            got = adapter.toolsets_for_source(SimpleNamespace(chat_type=chat_type))
            assert got == list(_GROUP_ALLOWED_TOOLSETS)

    def test_dm_c2c_and_unknown_keep_platform_default(self):
        adapter = self._make_adapter()
        for chat_type in ("dm", "c2c", "", None):
            assert adapter.toolsets_for_source(SimpleNamespace(chat_type=chat_type)) is None

    def test_group_result_keeps_terminal_but_no_host_only_toolsets(self):
        """``terminal`` is allowed back into groups (2026-10-09, "堵不如疏") BECAUSE the
        group turn's execution is moved into the docker sandbox per source
        (``terminal.source_policy``); every host-only toolset stays out.

        ``relay`` (2026-10-10) is likewise allowed: ``relay_to_owner`` takes no target,
        sends only to the owner fixed in config, and runs in the gateway — the one
        outward-send toolset a group may have, and not a host-only one."""
        adapter = self._make_adapter()
        got = set(adapter.toolsets_for_source(SimpleNamespace(chat_type="group")) or [])
        assert "terminal" in got
        assert "relay" in got
        assert not (got & self._HOST_ONLY_TOOLSETS)

    def test_fail_closed_when_source_cannot_be_classified(self):
        """SOUL §6.5 fails CLOSED: a source whose chat_type raises still gets
        the group-safe set instead of falling back to the platform default."""
        from plugins.platforms.qqbot.adapter import _GROUP_ALLOWED_TOOLSETS

        class ExplodingSource:
            @property
            def chat_type(self):
                raise RuntimeError("boom")

        adapter = self._make_adapter()
        assert adapter.toolsets_for_source(ExplodingSource()) == list(_GROUP_ALLOWED_TOOLSETS)


class TestQQAdapterTrustedDmToolsets:
    """[tamako-local] A HOST-TRUSTED sender's DM drops the sandbox-era ``relay`` toolset only.

    The trusted DM runs on the host, where ``terminal`` already reaches the send/file
    machinery, so ``relay_to_owner``/``export_file`` are redundant there. Everything else
    (browser, memory, MCP, …) stays exactly as the normal DM list has it.
    """

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_trusted_dm_drops_only_relay(self, monkeypatch):
        import plugins.platforms.qqbot.adapter as qq_adapter
        monkeypatch.setattr(qq_adapter, "_configured_qqbot_toolsets",
                            lambda: ["terminal", "file", "relay", "web", "memory"])
        monkeypatch.setattr("tools.terminal_scope.trusted_sender", lambda src: True)
        got = self._make_adapter().toolsets_for_source(
            SimpleNamespace(chat_type="dm", platform="qqbot", user_id="x"))
        assert got == ["terminal", "file", "web", "memory"]

    def test_untrusted_dm_keeps_the_platform_default(self, monkeypatch):
        monkeypatch.setattr("tools.terminal_scope.trusted_sender", lambda src: False)
        assert self._make_adapter().toolsets_for_source(SimpleNamespace(chat_type="dm")) is None

    def test_trusted_group_still_gets_the_group_allowlist(self, monkeypatch):
        from plugins.platforms.qqbot.adapter import _GROUP_ALLOWED_TOOLSETS
        monkeypatch.setattr("tools.terminal_scope.trusted_sender", lambda src: True)
        assert self._make_adapter().toolsets_for_source(
            SimpleNamespace(chat_type="group")) == list(_GROUP_ALLOWED_TOOLSETS)


class TestQQAdapterOwnerDmToolsets:
    """[tamako-local] The OPERATOR's own DM additionally gets ``cronjob``.

    The 传话待回 watcher is scheduled from the owner's QQ DM, and that DM's ``terminal``
    is the sandboxed one, so ``cronjob`` is the only tool that can schedule there.
    Deliberately scoped to owner ids (``terminal.source_policy.owner_ids``): adding the
    toolset to ``platform_toolsets.qqbot`` would hand it to every paired friend's DM, and
    a cron job runs as a source-less, host-privileged session — a sandbox escape.
    """

    def _make_adapter(self):
        from plugins.platforms.qqbot.adapter import QQAdapter
        return QQAdapter(_make_config(app_id="a", client_secret="b"))

    def test_owner_dm_adds_cronjob_after_the_platform_list(self, monkeypatch):
        import plugins.platforms.qqbot.adapter as qq_adapter
        monkeypatch.setattr(qq_adapter, "_configured_qqbot_toolsets",
                            lambda: ["terminal", "file", "relay", "web", "memory"])
        monkeypatch.setattr("tools.terminal_scope.trusted_sender", lambda src: False)
        monkeypatch.setattr("tools.terminal_scope.owner_sender", lambda src: True)
        got = self._make_adapter().toolsets_for_source(
            SimpleNamespace(chat_type="dm", platform="qqbot", user_id="owner"))
        assert got == ["terminal", "file", "relay", "web", "memory", "cronjob"]

    def test_owner_dm_with_cronjob_already_listed_keeps_platform_default(self, monkeypatch):
        import plugins.platforms.qqbot.adapter as qq_adapter
        monkeypatch.setattr(qq_adapter, "_configured_qqbot_toolsets",
                            lambda: ["terminal", "cronjob"])
        monkeypatch.setattr("tools.terminal_scope.trusted_sender", lambda src: False)
        monkeypatch.setattr("tools.terminal_scope.owner_sender", lambda src: True)
        assert self._make_adapter().toolsets_for_source(
            SimpleNamespace(chat_type="dm", platform="qqbot", user_id="owner")) is None

    def test_non_owner_dm_never_gets_cronjob(self, monkeypatch):
        monkeypatch.setattr("tools.terminal_scope.trusted_sender", lambda src: False)
        monkeypatch.setattr("tools.terminal_scope.owner_sender", lambda src: False)
        assert self._make_adapter().toolsets_for_source(
            SimpleNamespace(chat_type="dm", platform="qqbot", user_id="friend")) is None

    def test_owner_group_still_gets_the_group_allowlist(self, monkeypatch):
        from plugins.platforms.qqbot.adapter import _GROUP_ALLOWED_TOOLSETS
        monkeypatch.setattr("tools.terminal_scope.owner_sender", lambda src: True)
        assert self._make_adapter().toolsets_for_source(
            SimpleNamespace(chat_type="group")) == list(_GROUP_ALLOWED_TOOLSETS)

