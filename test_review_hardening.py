import asyncio
import json
import os
from types import SimpleNamespace
from pathlib import Path

import adapter
import onebot_platform.adapter as adapter_impl
import onebot_platform.outbound.notices as notices_impl
import onebot_platform.outbound.results as results_impl
import onebot_platform.config.core as config_core
from adapter import OneBotAdapter, _MediaCache
from onebot_platform.state.core import DedupCache, RateLimiter, MemberCache
from onebot_platform.state.settings import _PluginSettings


class _CaptureBot(OneBotAdapter):
    def __init__(self, config=None, **kwargs):
        super().__init__(config or {"extra": {"ws_url": "ws://127.0.0.1:3000/ws", "allowed_users": ["12345"]}}, **kwargs)
        self.calls = []

    async def _send_action_conn(self, conn, action, params, timeout=30.0):
        self.calls.append((action, params, timeout))
        return {"retcode": 0, "data": {"message_id": 123}}


def test_send_document_rejects_file_uri_outside_allowed_roots():
    bot = _CaptureBot()
    result = asyncio.run(bot.send_document("private_12345", "file:///etc/hosts", file_name="hosts"))
    assert result.success is False
    assert "outside allowed" in result.error
    assert bot.calls == []


def test_send_document_accepts_file_uri_inside_media_cache(tmp_path):
    media = tmp_path / "ok.txt"
    media.write_text("ok", encoding="utf-8")
    bot = _CaptureBot(media_cache=_MediaCache(tmp_path))
    result = asyncio.run(bot.send_document("private_12345", media.as_uri(), file_name="ok.txt"))
    assert result.success is True
    assert bot.calls[0][1]["file"] == media.as_uri()


def test_send_document_exposes_file_id_from_upload_response():
    class Bot(_CaptureBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            self.calls.append((action, params, timeout))
            return {"retcode": 0, "data": {"file_id": "file-123"}}

    bot = Bot()
    result = asyncio.run(bot.send_document("group_67890", "https://example.com/report.html", file_name="report.html"))
    assert result.success is True
    assert result.message_id == "file-123"
    assert bot.calls[0][0] == "upload_group_file"
    assert bot.calls[0][1]["group_id"] == 67890


def test_standalone_http_document_upload_preserves_original_filename(tmp_path, monkeypatch):
    src = tmp_path / "report.html"
    src.write_text("ok", encoding="utf-8")
    calls = []

    def fake_post(http_api_url, token, action, params):
        calls.append((http_api_url, token, action, params))
        return {"success": True, "message_id": "file-1"}

    monkeypatch.setattr(results_impl, "_post_onebot_http", fake_post)
    cfg = SimpleNamespace(extra={"http_api_url": "http://127.0.0.1:3001"})
    result = asyncio.run(adapter_impl._standalone_send(
        cfg,
        "group_67890",
        "",
        media_files=[(str(src), False)],
        force_document=True,
    ))
    assert result == {"success": True, "message_id": "file-1"}
    assert calls[0][2] == "upload_group_file"
    assert calls[0][3]["group_id"] == 67890
    assert calls[0][3]["name"] == "report.html"


def test_send_document_rejects_empty_path_before_upload():
    bot = _CaptureBot()
    result = asyncio.run(bot.send_document("private_12345", "   "))
    assert result.success is False
    assert "empty" in result.error.lower()
    assert bot.calls == []


def test_adapter_config_extra_tolerates_non_dict_extra():
    assert adapter_impl._config_extra({"extra": "bad"}) == {}
    bot = OneBotAdapter({"extra": "bad"})
    assert isinstance(bot._default_conn.ws_url, str)


def test_adapter_init_skips_malformed_accounts_and_keeps_valid_account():
    cfg = {"extra": {"accounts": ["bad", None, {"name": "ok", "ws_url": "ws://127.0.0.1:3000/ws"}]}}
    bot = OneBotAdapter(cfg)
    assert list(bot._connections) == ["ok"]
    assert bot._default_conn.ws_url == "ws://127.0.0.1:3000/ws"


def test_adapter_init_all_malformed_accounts_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    bot = OneBotAdapter({"extra": {"accounts": ["bad", None, {"name": ""}], "ws_url": "ws://fallback/ws"}})
    assert list(bot._connections) == ["default"]
    assert bot._default_conn.ws_url == "ws://fallback/ws"


def test_plugin_multi_account_detection_ignores_malformed_accounts(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("gateway:\n  platforms:\n    onebot:\n      extra:\n        accounts:\n          - bad\n          - name: ok\n            ws_url: ws://127.0.0.1:3000/ws\n", encoding="utf-8")
    monkeypatch.setattr(adapter_impl, "_hermes_config_path", lambda: cfg)
    import importlib.util
    root_init = Path(__file__).resolve().parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("onebot_plugin_root_for_test", root_init)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "onebot_platform"
    spec.loader.exec_module(mod)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    accounts = mod._detect_multi_account()
    assert accounts == [{"name": "ok", "ws_url": "ws://127.0.0.1:3000/ws"}]


def test_standalone_send_tolerates_non_dict_extra_without_crashing(monkeypatch):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    monkeypatch.delenv("ONEBOT_HTTP_API_URL", raising=False)
    result = asyncio.run(adapter_impl._standalone_send(SimpleNamespace(extra="bad"), "private_12345", "hello"))
    assert result["success"] is False
    assert "ONEBOT_WS_URL" in result["error"]


def test_file_uri_rejects_empty_file_scheme():
    for raw in ["file://", "file://   "]:
        try:
            results_impl._file_uri(raw)
        except ValueError as exc:
            assert "empty" in str(exc).lower() or "file" in str(exc).lower()
        else:
            raise AssertionError("empty file:// URI should be rejected")


def test_file_uri_rejects_missing_local_file_uri(tmp_path):
    missing = (tmp_path / "missing.txt").as_uri()
    try:
        results_impl._file_uri(missing)
    except ValueError as exc:
        assert "not found" in str(exc).lower() or "file" in str(exc).lower()
    else:
        raise AssertionError("missing file:// URI should be rejected")


def test_post_onebot_http_preserves_forward_id(monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"retcode":200,"data":{"forward_id":"fwd-1"}}'
    monkeypatch.setattr(results_impl.urllib.request, "urlopen", lambda req, timeout=60: Resp())
    result = results_impl._post_onebot_http("http://127.0.0.1:3001", "", "send_group_forward_msg", {})
    assert result == {"success": True, "message_id": "fwd-1"}


def test_send_forward_message_accepts_retcode_200_and_forward_id():
    class Bot(_CaptureBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            return {"retcode": 200, "data": {"forward_id": "fwd-2"}}
    bot = Bot()
    result = asyncio.run(bot.send_forward_message("private_12345", [{"content": "hello"}]))
    assert result.success is True
    assert result.message_id == "fwd-2"


def test_delete_message_accepts_retcode_200():
    class Bot(_CaptureBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            return {"retcode": 200}
    bot = Bot()
    assert asyncio.run(bot.delete_message("private_12345", "123")) is True


def test_send_action_retcode_200_does_not_mark_action_unsupported(monkeypatch):
    bot = OneBotAdapter({"extra": {"ws_url": "ws://127.0.0.1:3000/ws"}})
    conn = bot._default_conn
    class Ws:
        close_code = None
        async def send(self, payload): pass
    conn.ws = Ws()
    async def fast_wait(fut, timeout=None):
        fut.set_result({"retcode": 200, "msg": "unknown action", "data": {"message_id": 1}})
        return await fut
    monkeypatch.setattr(adapter_impl.asyncio, "wait_for", fast_wait)
    result = asyncio.run(bot._send_action_conn(conn, "send_private_msg", {"user_id": 1, "message": []}))
    assert result["retcode"] == 200
    assert "send_private_msg" not in bot._unsupported_actions


def test_real_set_input_status_failure_keeps_active_state():
    class Bot(_CaptureBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            return {"retcode": -1, "msg": "boom"}
    bot = Bot()
    bot._active_input_status["private_12345"] = True
    result = asyncio.run(bot.set_input_status("private_12345", event_type=0))
    assert result.success is False
    assert bot._active_input_status["private_12345"] is True


def test_disconnect_cancels_background_delete_tasks():
    class Bot(_CaptureBot):
        async def _delete_message_with_status(self, chat_id, message_id, timeout=15.0):
            await asyncio.sleep(60)
    async def run():
        bot = Bot()
        bot._fire_and_forget_delete("private_12345", "1")
        await asyncio.sleep(0)
        assert bot._bg_delete_tasks
        await bot.disconnect()
        assert bot._bg_delete_tasks == set()
    asyncio.run(run())


def test_rate_limiter_zero_rate_does_not_divide_by_zero():
    async def run():
        limiter = RateLimiter(rate=0, burst=0)
        await asyncio.wait_for(limiter.acquire(), timeout=0.2)
    asyncio.run(run())


def test_rate_limiter_bad_values_fall_back_safely():
    async def run():
        limiter = RateLimiter(rate="bad", burst="bad")
        await asyncio.wait_for(limiter.acquire(), timeout=0.2)
    asyncio.run(run())


def test_dedup_cache_bad_values_fall_back_safely():
    cache = DedupCache(ttl="bad", max_size="bad")
    assert cache.is_duplicate("x") is False
    assert cache.is_duplicate("x") is True


def test_member_cache_bad_ttl_expires_safely():
    cache = MemberCache(ttl="bad")
    cache.set("g", "u", {"nickname": "n"})
    assert cache.get("g", "u") is not None


def test_apply_yaml_config_core_tolerates_non_dict_extra(monkeypatch):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    extra = config_core._apply_yaml_config({}, {"extra": "bad"}, merge_platform_blocks=lambda y, p: p)
    assert extra == {}
    assert "ONEBOT_WS_URL" not in os.environ


def test_runtime_paths_allow_napcat_send_directory():
    _data_dir, _media_cache_dir, outbound_roots = config_core.build_runtime_paths()
    assert Path("/var/lib/napcat/hermes-send") in outbound_roots


def test_send_document_accepts_file_from_napcat_send_dir(tmp_path):
    send_dir = Path("/var/lib/napcat/hermes-send")
    if not send_dir.exists():
        send_dir = tmp_path
    send_dir.mkdir(parents=True, exist_ok=True)
    media = send_dir / "adapter-safe-upload.txt"
    media.write_text("ok", encoding="utf-8")
    bot = _CaptureBot(media_cache=_MediaCache(send_dir))

    result = asyncio.run(bot.send_document("group_67890", str(media), file_name=media.name))

    assert result.success is True
    assert result.message_id == "123"
    assert bot.calls[0][0] == "upload_group_file"
    assert bot.calls[0][1]["group_id"] == 67890
    assert bot.calls[0][1]["name"] == "adapter-safe-upload.txt"


def test_send_document_rejects_bool_target_id():
    bot = _CaptureBot()
    result = asyncio.run(bot.send_document("private_True", "https://example.com/a.txt"))
    assert result.success is False
    assert "target_id" in result.error
    assert bot.calls == []


def test_send_image_rejects_bare_local_path_outside_allowed_roots():
    bot = _CaptureBot()
    result = asyncio.run(bot.send_image("private_12345", "/etc/hosts"))
    assert result.success is False
    assert "outside allowed" in result.error
    assert bot.calls == []


def test_send_document_ignores_trusted_local_file_metadata_for_unsafe_paths():
    bot = _CaptureBot()
    result = asyncio.run(bot.send_document(
        "private_12345",
        "file:///etc/hosts",
        file_name="hosts",
        metadata={"trusted_local_file": True},
    ))
    assert result.success is False
    assert "outside allowed" in result.error
    assert bot.calls == []


def test_group_send_does_not_mention_for_originator_metadata_only():
    bot = _CaptureBot()
    bot._plugin_settings = _PluginSettings(Path(os.environ.get("TMPDIR", "/tmp")) / "onebot-normal-mention-test.json")
    result = asyncio.run(bot.send(
        "group_67890",
        "普通回复",
        metadata={"originator_user_id": "12345"},
    ))
    assert result.success is True
    message = bot.calls[-1][1]["message"]
    assert not any(seg.get("type") == "at" for seg in message)
    assert message[0] == {"type": "text", "data": {"text": "普通回复"}}


def test_group_send_mentions_only_for_explicit_notify_metadata():
    bot = _CaptureBot()
    bot._plugin_settings = _PluginSettings(Path(os.environ.get("TMPDIR", "/tmp")) / "onebot-explicit-mention-test.json")
    result = asyncio.run(bot.send(
        "group_67890",
        "审批提醒",
        metadata={"originator_user_id": "12345", "mention_originator_user_id": "12345"},
    ))
    assert result.success is True
    message = bot.calls[-1][1]["message"]
    assert message[0] == {"type": "at", "data": {"qq": "12345"}}
    assert message[1] == {"type": "text", "data": {"text": " "}}
    assert message[2] == {"type": "text", "data": {"text": "审批提醒"}}


def test_send_action_uses_http_fallback_when_ws_send_raises(monkeypatch):
    bot = OneBotAdapter({"extra": {"ws_url": "ws://127.0.0.1:3000/ws", "http_api_url": "http://127.0.0.1:3001"}})
    conn = bot._default_conn
    class Ws:
        close_code = None
        async def send(self, payload):
            raise OSError("broken ws")
    conn.ws = Ws()
    http_calls = []
    async def fake_http(conn_arg, action, params):
        http_calls.append((conn_arg, action, params))
        return {"retcode": 0, "data": {"message_id": 9}}
    monkeypatch.setattr(bot, "_http_call_conn", fake_http)
    result = asyncio.run(bot._send_action_conn(conn, "send_private_msg", {"user_id": 1, "message": []}))
    assert result["retcode"] == 0
    assert http_calls and http_calls[0][1] == "send_private_msg"
    assert conn.echo_futures == {}


def test_failed_media_send_is_retryable_not_suppressed_by_dedup():
    class Bot(_CaptureBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            self.calls.append((action, params, timeout))
            if len(self.calls) == 1:
                return {"retcode": -1, "msg": "timeout"}
            return {"retcode": 0, "data": {"message_id": 456}}
    bot = Bot()
    bot._default_conn.http_api_url = ""
    bot._default_conn.ws = SimpleNamespace(close_code=None)
    result1 = asyncio.run(bot._send_media("private_12345", "image", "https://example.com/a.png"))
    result2 = asyncio.run(bot._send_media("private_12345", "image", "https://example.com/a.png"))
    assert result1.success is False
    assert result1.retryable is True
    assert result2.success is True
    assert result2.message_id == "456"
    assert len(bot.calls) == 2


def test_successful_media_send_is_deduped_after_delivery_only():
    class Bot(_CaptureBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            self.calls.append((action, params, timeout))
            return {"retcode": 0, "data": {"message_id": 789}}
    bot = Bot()
    bot._default_conn.http_api_url = ""
    bot._default_conn.ws = SimpleNamespace(close_code=None)
    result1 = asyncio.run(bot._send_media("private_12345", "image", "https://example.com/a.png"))
    result2 = asyncio.run(bot._send_media("private_12345", "image", "https://example.com/a.png"))
    assert result1.success is True
    assert result1.message_id == "789"
    assert result2.success is True
    assert len(bot.calls) == 1


def test_prepare_outbound_local_file_rejects_oversized_file(tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(b"12345")
    cache = _MediaCache(tmp_path / "cache", max_file_size=4)
    assert cache.prepare_outbound_local_file(str(src)) is None
    assert list((tmp_path / "cache").glob("outbound-*")) == []


def test_read_bounded_json_response_rejects_oversized_body():
    class Resp:
        def read(self, n=-1):
            return b"x" * n
    try:
        results_impl._read_bounded_json_response(Resp(), max_bytes=4)
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("oversized response should be rejected")


def test_post_onebot_http_rejects_oversized_response_body(monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, n=-1): return b"x" * n
    monkeypatch.setattr(results_impl.urllib.request, "urlopen", lambda req, timeout=60: Resp())
    result = results_impl._post_onebot_http("http://127.0.0.1:3001", "", "send_private_msg", {})
    assert result["success"] is False
    assert "exceeds" in result["error"]


def _install_fake_approval(monkeypatch):
    import sys
    import types

    import onebot_platform.gateway_integration.approvals as approvals_impl
    calls = []
    approval_mod = types.SimpleNamespace(
        has_blocking_approval=lambda session_key: True,
        resolve_gateway_approval=lambda session_key, choice: calls.append((session_key, choice)),
    )
    tools_mod = types.SimpleNamespace(approval=approval_mod)
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.approval", approval_mod)
    monkeypatch.setattr(approvals_impl, "_HAS_APPROVAL", True)
    monkeypatch.setattr(notices_impl, "_HAS_APPROVAL", True)
    return calls


class _ApprovalBot(_CaptureBot):
    def __init__(self, *, admin_qq="12345", allowed_users=None, group_ids=None):
        super().__init__({"extra": {"accounts": [{"name": "default", "ws_url": "ws://127.0.0.1:3000/ws", "allowed_users": allowed_users or ["12345"], "group_ids": group_ids or ["67890"], "admin_qq": admin_qq}]}})
        self.sent = []
        self.reaction_actions = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SimpleNamespace(success=True, message_id="approval-msg-1")

    async def _send_action_conn(self, conn, action, params, timeout=30.0):
        self.reaction_actions.append((action, params, timeout))
        return {"retcode": 0, "data": {}}


def test_admin_bypasses_message_allowlist_and_group_scope():
    bot = _ApprovalBot(admin_qq="99999", allowed_users=["12345"], group_ids=["67890"])
    allowed = asyncio.run(bot._check_authorization_async(
        "99999", "group", {"message_type": "group", "group_id": 11111, "user_id": 99999}, bot._default_conn
    ))
    normal = asyncio.run(bot._check_authorization_async(
        "88888", "group", {"message_type": "group", "group_id": 11111, "user_id": 88888}, bot._default_conn
    ))
    assert allowed is True
    assert normal is False


def test_group_poke_approval_respects_allowlist(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    data = {
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": 67890,
        "user_id": 99999,
        "target_id": 11111,
        "self_id": 11111,
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == []
    assert bot.sent == []
    assert bot._pending_approvals["group_67890"] == "session-key"


def test_core_approval_shortcut_respects_allowlist(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"

    blocked = asyncio.run(bot._resolve_approval_shortcut("group_67890", "1", "99999", "12345"))
    allowed = asyncio.run(bot._resolve_approval_shortcut("group_67890", "1", "12345", "12345"))

    assert blocked is False
    assert allowed is True
    assert calls == [("session-key", "once")]
    assert bot.sent == [("group_67890", "✓ 单次批准", None, None)]


def test_group_approval_text_shortcut_bypasses_wake_requirement(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    data = {
        "message_type": "group",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "user-msg-1",
        "message": [{"type": "text", "data": {"text": "2"}}],
    }

    asyncio.run(bot._handle_message(data, bot._default_conn))

    assert calls == [("session-key", "session")]
    assert bot.sent == [("group_67890", "✓ 会话批准", None, None)]


def test_admin_approval_bypasses_allowlist_and_group_scope(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot(admin_qq="99999", allowed_users=["12345"], group_ids=["67890"])
    bot._pending_approvals["group_11111"] = "session-key"

    allowed = asyncio.run(bot._resolve_approval_shortcut("group_11111", "1", "99999", "99999"))

    assert allowed is True
    assert calls == [("session-key", "once")]
    assert bot.sent == [("group_11111", "✓ 单次批准", None, None)]


def test_group_poke_approval_allows_whitelisted_user(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    data = {
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": 67890,
        "user_id": 12345,
        "target_id": 11111,
        "self_id": 11111,
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == [("session-key", "once")]
    assert bot.sent == [("group_67890", "✓ 单次批准", None, None)]


def test_exec_approval_prompt_tracks_message_id_for_reaction():
    bot = _ApprovalBot()

    result = asyncio.run(bot.send_exec_approval("group_67890", "rm -rf /tmp/x", "session-key"))

    assert result.success is True
    assert bot._pending_approvals["group_67890"] == "session-key"
    assert bot._pending_approval_messages["group_67890"] == "approval-msg-1"
    assert "点下方表情=允许该会话" in bot.sent[0][1]


def test_exec_approval_prompt_mentions_originator_when_metadata_has_only_originator():
    bot = _ApprovalBot()

    asyncio.run(bot.send_exec_approval(
        "group_67890",
        "rm -rf /tmp/x",
        "session-key",
        metadata={"originator_user_id": "12345"},
    ))

    metadata = bot.sent[0][3]
    assert metadata["originator_user_id"] == "12345"
    assert metadata["mention_originator_user_id"] == "12345"
    assert metadata["mention_reason"] == "approval_prompt"


def test_exec_approval_prompt_adds_clickable_reaction_hint():
    bot = _ApprovalBot()

    asyncio.run(bot.send_exec_approval("group_67890", "rm -rf /tmp/x", "session-key"))

    assert bot.reaction_actions == [(
        "set_msg_emoji_like",
        {"message_id": "approval-msg-1", "emoji_id": "66"},
        10.0,
    )]


def test_group_reaction_approval_allows_session_on_prompt_message(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "group_reaction",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "approval-msg-1",
        "qface_id": "66",
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == [("session-key", "session")]
    assert bot.sent == [("group_67890", "✓ 会话批准", None, None)]
    assert "group_67890" not in bot._pending_approvals
    assert "group_67890" not in bot._pending_approval_messages


def test_group_reaction_approval_ignores_other_message(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "group_reaction",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "normal-msg-9",
        "qface_id": "66",
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == []
    assert bot.sent == []
    assert bot._pending_approvals["group_67890"] == "session-key"


def test_group_reaction_removal_does_not_approve(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "group_reaction",
        "sub_type": "remove",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "approval-msg-1",
        "qface_id": "66",
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == []
    assert bot.sent == []
    assert bot._pending_approvals["group_67890"] == "session-key"


def test_message_reactions_updated_is_recognized(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "message_reactions_updated",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "approval-msg-1",
        "current_reactions": [{"emoji_id": "66", "count": 1}],
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == [("session-key", "session")]


def test_group_reaction_approval_accepts_nested_target_message_id(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "notify",
        "sub_type": "emoji_like",
        "group_id": 67890,
        "operator_id": 12345,
        "message": {"id": "approval-msg-1"},
        "likes": [{"emoji_id": "66", "count": 1}],
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == [("session-key", "session")]
    assert bot.sent == [("group_67890", "✓ 会话批准", None, None)]


def test_group_msg_emoji_like_approval_allows_session(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "group_msg_emoji_like",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "approval-msg-1",
        "likes": [{"emoji_id": "66", "count": 1}],
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == [("session-key", "session")]
    assert bot.sent == [("group_67890", "✓ 会话批准", None, None)]


def test_group_msg_emoji_like_removal_does_not_approve(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "group_msg_emoji_like",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "approval-msg-1",
        "likes": [{"emoji_id": "66", "count": 0}],
        "is_add": False,
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == []
    assert bot.sent == []
    assert bot._pending_approvals["group_67890"] == "session-key"


def test_reaction_approval_ignores_other_emoji(monkeypatch):
    calls = _install_fake_approval(monkeypatch)
    bot = _ApprovalBot()
    bot._pending_approvals["group_67890"] = "session-key"
    bot._pending_approval_messages["group_67890"] = "approval-msg-1"
    data = {
        "notice_type": "group_msg_emoji_like",
        "group_id": 67890,
        "user_id": 12345,
        "message_id": "approval-msg-1",
        "likes": [{"emoji_id": "123", "count": 1}],
    }

    asyncio.run(bot._handle_notice(data, bot._default_conn))

    assert calls == []
    assert bot.sent == []
    assert bot._pending_approvals["group_67890"] == "session-key"
