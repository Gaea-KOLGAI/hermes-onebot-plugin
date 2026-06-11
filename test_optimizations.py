
import os, tempfile, asyncio, sys
from pathlib import Path

import adapter
import onebot_platform.adapter as adapter_impl
import onebot_platform.outbound.results as results_impl
import onebot_platform.state.core as state_core
from adapter import _parse_single_account_env, _truthy, _csv_list, _apply_yaml_config, DATA_DIR, MEDIA_CACHE_DIR
from adapter import _extract_text_from_message, _extract_segments, _MediaCache, OneBotAdapter
from onebot_platform.config.core import _config_extra, _configured_ws_urls, validate_config
from onebot_platform.state.core import DedupCache, MemberCache
from onebot_platform.outbound.results import _account_extra, _file_uri, _post_onebot_http, _safe_int
from adapter import (
    _load_gateway_tool_progress_mode,
    _normalise_tool_progress_mode,
    _save_gateway_tool_progress_mode,
)


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "image/jpeg"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        yield b"x" * 8
        yield b"y" * 8

    def raise_for_status(self):
        raise AssertionError("unexpected status")


class _FakeClient:
    def stream(self, *args, **kwargs):
        return _FakeResponse()


def test_bool_and_csv_parsing_from_extra(monkeypatch):
    for key in ["ONEBOT_ALLOW_ALL_USERS", "ONEBOT_ALLOWED_USERS"]:
        monkeypatch.delenv(key, raising=False)
    parsed = _parse_single_account_env({"allow_all": True, "allowed_users": [123, "456"]})
    assert parsed["allow_all"] is True
    assert parsed["allowed_users"] == ["123", "456"]


def test_yaml_bridge_sets_env_and_returns_extra(monkeypatch):
    for key in ["ONEBOT_WS_URL", "ONEBOT_HTTP_API_URL", "ONEBOT_ALLOWED_USERS", "ONEBOT_ALLOW_ALL_USERS"]:
        monkeypatch.delenv(key, raising=False)
    extra = _apply_yaml_config({}, {"extra": {
        "ws_url": "ws://127.0.0.1:8002",
        "http_api_url": "http://127.0.0.1:3000",
        "allowed_users": ["123456789"],
        "allow_all": False,
    }})
    assert os.environ["ONEBOT_WS_URL"] == "ws://127.0.0.1:8002"
    assert os.environ["ONEBOT_HTTP_API_URL"] == "http://127.0.0.1:3000"
    assert os.environ["ONEBOT_ALLOWED_USERS"] == "123456789"
    assert os.environ["ONEBOT_ALLOW_ALL_USERS"] == "false"
    assert extra["http_api_url"] == "http://127.0.0.1:3000"


def test_runtime_paths_are_profile_writable():
    assert DATA_DIR.exists()
    assert DATA_DIR.is_dir()
    assert MEDIA_CACHE_DIR.exists()
    assert MEDIA_CACHE_DIR.is_dir()
    assert os.access(DATA_DIR, os.W_OK)
    assert os.access(MEDIA_CACHE_DIR, os.W_OK)


def test_tool_progress_switch_is_gateway_scoped(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("display:\n  tool_progress: new\n", encoding="utf-8")
    monkeypatch.setattr(adapter_impl, "_hermes_config_path", lambda: cfg)

    assert _normalise_tool_progress_mode(False) == "off"
    assert _normalise_tool_progress_mode(True) == "all"
    assert _normalise_tool_progress_mode("verbose") == "verbose"
    assert _normalise_tool_progress_mode("bad") == "all"
    assert adapter_impl._load_gateway_tool_progress_mode("onebot") == "new"

    adapter_impl._save_gateway_tool_progress_mode("off", "onebot")
    assert adapter_impl._load_gateway_tool_progress_mode("onebot") == "off"
    assert "platforms:\n    onebot:\n      tool_progress: 'off'" in cfg.read_text(encoding="utf-8")


def test_adapter_no_longer_filters_tool_progress_locally():
    src = Path(adapter_impl.__file__).read_text(encoding="utf-8")
    command_src = Path(adapter_impl.CommandMixin.__module__.replace('.', '/')).with_suffix('.py')
    command_text = (Path(__file__).resolve().parent / command_src).read_text(encoding="utf-8")
    assert "_TOOL_PROGRESS_RE" not in src
    assert "settings.get(\"tool_progress\") is False" not in src
    assert "_save_gateway_tool_progress_mode(mode, \"onebot\")" in command_text


def test_cq_text_unescape_uses_single_segment_parser():
    raw = "hello&#44; [CQ:at,qq=123] world&#91;ok&#93;"
    assert _extract_segments(raw) == [{"type": "text", "data": {"text": "hello,"}}, {"type": "at", "data": {"qq": "123"}}, {"type": "text", "data": {"text": "world[ok]"}}]
    assert _extract_text_from_message(raw) == "hello,world[ok]"


def test_group_upload_notice_is_passive_and_does_not_dispatch():
    class PassiveUploadAdapter(OneBotAdapter):
        def __init__(self):
            super().__init__({"extra": {"allowed_users": ["12345"]}})
            self.group_upload_calls = 0

        async def _handle_group_upload_notice(self, data, conn):
            self.group_upload_calls += 1
            raise AssertionError("group_upload notice should not be actively dispatched")

    bot = PassiveUploadAdapter()
    data = {
        "notice_type": "group_upload",
        "group_id": 1000000001,
        "user_id": 100000001,
        "file": {"name": "Hermes.Studio-0.6.10-x64.exe", "size": 146393250},
    }
    asyncio.run(bot._handle_notice(data, bot._default_conn))
    assert bot.group_upload_calls == 0


def test_media_download_streams_and_removes_oversized_partial(tmp_path):
    cache = _MediaCache(tmp_path, max_file_size=12)
    result = asyncio.run(cache.download("https://example.com/image.jpg", _FakeClient(), "image"))
    assert result is None
    assert list((tmp_path / "image").iterdir()) == []


class _ImmediateWs:
    close_code = None

    async def send(self, payload):
        pass


class _StandaloneWs:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        return self.payload


async def _connect_standalone_ws(url, **kwargs):
    return _StandaloneWs('{"echo":"fixed-echo","retcode":200,"data":{"message_id":654321}}')


def test_send_action_success_cleans_echo_state(monkeypatch):
    bot = OneBotAdapter({"extra": {"allowed_users": ["12345"]}})
    conn = bot._default_conn
    conn.ws = _ImmediateWs()

    async def fast_wait(fut, timeout=None):
        echo = next(iter(conn.echo_futures))
        fut.set_result({"retcode": 0, "data": {"message_id": 123}})
        return await fut

    monkeypatch.setattr(adapter_impl.asyncio, "wait_for", fast_wait)
    result = asyncio.run(bot._send_action_conn(conn, "send_private_msg", {"user_id": 1, "message": []}))
    assert result["retcode"] == 0
    assert conn.echo_futures == {}
    assert conn._echo_timestamps == {}


def test_as_onebot_file_value_rejects_empty_path():
    bot = OneBotAdapter({"extra": {"allowed_users": ["12345"]}})
    for raw in ["", "   "]:
        try:
            bot._as_onebot_file_value(raw)
        except ValueError as exc:
            assert "empty" in str(exc).lower()
        else:
            raise AssertionError("empty outbound file path should be rejected")


def test_result_to_send_result_accepts_retcode_200():
    result = adapter_impl._result_to_send_result({"retcode": 200, "data": {"message_id": 42}}, "send", extract_msg_id=True)
    assert result.success is True
    assert result.message_id == "42"


def test_clear_input_status_keeps_state_when_remote_clear_fails(monkeypatch):
    bot = OneBotAdapter({"extra": {"allowed_users": ["12345"]}})
    bot._active_input_status["private_12345"] = True

    async def failed_set_input_status(chat_id, event_type=1):
        return adapter_impl.SendResult(success=False, error="boom")

    monkeypatch.setattr(bot, "set_input_status", failed_set_input_status)
    asyncio.run(bot.clear_input_status("private_12345"))
    assert bot._active_input_status["private_12345"] is True


def test_standalone_ws_success_preserves_message_id(monkeypatch):
    monkeypatch.delenv("ONEBOT_HTTP_API_URL", raising=False)
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    monkeypatch.setattr(results_impl.uuid, "uuid4", lambda: "fixed-echo")
    result = asyncio.run(results_impl._standalone_send(
        type("Cfg", (), {"extra": {"ws_url": "ws://example.test/ws"}})(),
        "private_12345",
        "hello",
        media_cache_factory=_MediaCache,
        media_cache_dir=MEDIA_CACHE_DIR,
        parse_chat_id=adapter_impl._parse_chat_id,
        extract_account_from_chat_id=adapter_impl._extract_account_from_chat_id,
        guess_media_segment_type=adapter_impl._guess_media_segment_type,
        websockets_available=True,
        websockets_connect=_connect_standalone_ws,
    ))
    assert result == {"success": True, "message_id": "654321"}


def test_account_extra_skips_malformed_account_entries():
    extra = {"accounts": ["bad", None, {"name": "target", "ws_url": "ws://ok"}], "ws_url": "ws://fallback"}
    account = _account_extra(extra, "target:private_123", adapter_impl._extract_account_from_chat_id)
    assert account == {"name": "target", "ws_url": "ws://ok"}


def test_file_uri_rejects_empty_path():
    for raw in ["", "   "]:
        try:
            _file_uri(raw)
        except ValueError as exc:
            assert "empty" in str(exc).lower()
        else:
            raise AssertionError("empty standalone file path should be rejected")


def test_post_onebot_http_accepts_retcode_200(monkeypatch):
    class FakeHTTPResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return b'{"retcode":200,"data":{"file_id":"abc-file"}}'

    monkeypatch.setattr(results_impl.urllib.request, "urlopen", lambda req, timeout=60: FakeHTTPResponse())
    result = _post_onebot_http("http://127.0.0.1:3001", "", "upload_private_file", {"user_id": 1})
    assert result == {"success": True, "message_id": "abc-file"}


def test_safe_int_rejects_bool_target_ids():
    for raw in [True, False]:
        try:
            _safe_int(raw, "target_id")
        except ValueError as exc:
            assert "target_id" in str(exc)
        else:
            raise AssertionError("bool target ids should not be accepted as 0/1")


def test_standalone_send_reuses_media_cache_factory(monkeypatch, tmp_path):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    sent_actions = []
    media = tmp_path / "a.png"
    media.write_bytes(b"png")

    class CountingCache:
        calls = 0
        def __init__(self, cache_dir):
            type(self).calls += 1
        def prepare_outbound_local_file(self, path):
            return str(path)

    def fake_post(http_api_url, token, action, params):
        sent_actions.append(action)
        return {"success": True, "message_id": action}

    monkeypatch.setattr(results_impl, "_post_onebot_http", fake_post)
    result = asyncio.run(results_impl._standalone_send(
        type("Cfg", (), {"extra": {"http_api_url": "http://127.0.0.1:3001"}})(),
        "private_12345",
        "caption",
        media_files=[(str(media), False), (str(media), False)],
        media_cache_factory=CountingCache,
        media_cache_dir=tmp_path,
        parse_chat_id=adapter_impl._parse_chat_id,
        extract_account_from_chat_id=adapter_impl._extract_account_from_chat_id,
        guess_media_segment_type=adapter_impl._guess_media_segment_type,
        websockets_available=False,
        websockets_connect=lambda *args, **kwargs: None,
    ))
    assert result["success"] is True
    assert CountingCache.calls == 1
    assert sent_actions == ["send_private_msg", "send_private_msg", "send_private_msg"]


def test_config_extra_ignores_non_dict_extra():
    assert _config_extra({"extra": "bad"}) == {}
    assert _config_extra(type("Cfg", (), {"extra": ["bad"]})()) == {}


def test_configured_ws_urls_skips_malformed_accounts():
    extra = {"accounts": ["bad", {"name": "a", "ws_url": "ws://127.0.0.1:3000/ws"}, {"name": "empty"}]}
    assert _configured_ws_urls(extra) == ["ws://127.0.0.1:3000/ws"]
    assert validate_config({"extra": extra}) is True


def test_validate_config_returns_false_for_only_malformed_accounts():
    assert validate_config({"extra": {"accounts": ["bad", None, {"name": "empty"}]}}) is False


def test_member_cache_removes_expired_entry_on_get(monkeypatch):
    cache = MemberCache(ttl=0.1)
    cache.set("g", "u", {"nickname": "n"})
    monkeypatch.setattr(state_core.time, "time", lambda: 9999999999)
    assert cache.get("g", "u") is None
    assert cache._cache == {}


def test_media_cache_cleanup_keeps_at_most_max_files(tmp_path):
    cache = _MediaCache(tmp_path, max_files=2)
    subdir = tmp_path / "image"
    subdir.mkdir()
    for idx in range(7):
        p = subdir / f"{idx}.jpg"
        p.write_bytes(b"x")
        os.utime(p, (idx, idx))
    cache._cleanup_subdir(subdir)
    remaining = sorted(p.name for p in subdir.iterdir())
    assert remaining == ["5.jpg", "6.jpg"]


class _EmptyResponse:
    status_code = 200
    headers = {"content-type": "image/jpeg"}
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
    async def aiter_bytes(self):
        if False:
            yield b""
    def raise_for_status(self):
        raise AssertionError("unexpected status")


class _EmptyClient:
    def stream(self, *args, **kwargs):
        return _EmptyResponse()


def test_parse_single_account_env_tolerates_non_dict_extra(monkeypatch):
    for key in ["ONEBOT_WS_URL", "ONEBOT_ACCESS_TOKEN", "ONEBOT_HTTP_API_URL", "ONEBOT_ALLOWED_USERS", "ONEBOT_GROUP_IDS"]:
        monkeypatch.delenv(key, raising=False)
    parsed = _parse_single_account_env("bad-extra")
    assert parsed["ws_url"] == ""
    assert parsed["allowed_users"] == []


def test_yaml_bridge_tolerates_non_dict_extra(monkeypatch):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    extra = _apply_yaml_config({}, {"extra": "bad"})
    assert extra == {}
    assert "ONEBOT_WS_URL" not in os.environ


def test_save_gateway_tool_progress_replaces_non_mapping_root(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- bad\n", encoding="utf-8")
    monkeypatch.setattr(adapter_impl, "_hermes_config_path", lambda: cfg)
    adapter_impl._save_gateway_tool_progress_mode("off", "onebot")
    assert adapter_impl._load_gateway_tool_progress_mode("onebot") == "off"


def test_media_download_rejects_empty_response(tmp_path):
    cache = _MediaCache(tmp_path, max_file_size=12)
    result = asyncio.run(cache.download("https://example.com/empty.jpg", _EmptyClient(), "image"))
    assert result is None
    assert list((tmp_path / "image").iterdir()) == []


def test_dedup_cache_zero_size_does_not_crash():
    cache = DedupCache(ttl=60, max_size=0)
    assert cache.is_duplicate("a") is False
    assert cache.is_duplicate("a") is True
