
import os, tempfile, asyncio, sys
from pathlib import Path

import adapter
import onebot_platform.adapter as adapter_impl
from adapter import _parse_single_account_env, _truthy, _csv_list, _apply_yaml_config, DATA_DIR, MEDIA_CACHE_DIR
from adapter import _extract_text_from_message, _extract_segments, _MediaCache, OneBotAdapter
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
        "group_id": 1103659691,
        "user_id": 257155386,
        "file": {"name": "Hermes.Studio-0.6.10-x64.exe", "size": 146393250},
    }
    asyncio.run(bot._handle_notice(data, bot._default_conn))
    assert bot.group_upload_calls == 0


def test_media_download_streams_and_removes_oversized_partial(tmp_path):
    cache = _MediaCache(tmp_path, max_file_size=12)
    result = asyncio.run(cache.download("https://example.com/image.jpg", _FakeClient(), "image"))
    assert result is None
    assert list((tmp_path / "image").iterdir()) == []
