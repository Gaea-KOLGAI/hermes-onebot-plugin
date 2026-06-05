
import os, tempfile, asyncio
from pathlib import Path

import adapter
from adapter import _parse_single_account_env, _truthy, _csv_list, _apply_yaml_config, DATA_DIR, MEDIA_CACHE_DIR
from adapter import _extract_text_from_message, _extract_segments, _MediaCache


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
    assert ".hermes/plugins/onebot-platform" in str(DATA_DIR)
    assert MEDIA_CACHE_DIR.parent == DATA_DIR


def test_cq_text_unescape_uses_single_segment_parser():
    raw = "hello&#44; [CQ:at,qq=123] world&#91;ok&#93;"
    assert _extract_segments(raw) == [{"type": "text", "data": {"text": "hello,"}}, {"type": "at", "data": {"qq": "123"}}, {"type": "text", "data": {"text": "world[ok]"}}]
    assert _extract_text_from_message(raw) == "hello,world[ok]"


def test_media_download_streams_and_removes_oversized_partial(tmp_path):
    cache = _MediaCache(tmp_path, max_file_size=12)
    result = asyncio.run(cache.download("https://example.com/image.jpg", _FakeClient(), "image"))
    assert result is None
    assert list((tmp_path / "image").iterdir()) == []
