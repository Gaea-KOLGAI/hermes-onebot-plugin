import asyncio

from adapter import OneBotAdapter
from onebot_platform.adapter_runtime_helpers import build_connections, install_shared_state


class _FakeMediaCache:
    pass


def test_build_connections_keeps_adapter_connection_semantics(monkeypatch):
    for key in ("ONEBOT_WS_URL", "ONEBOT_ALLOWED_USERS", "ONEBOT_GROUP_IDS", "ONEBOT_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)

    connections, multi_account, default_conn = build_connections({
        "accounts": [
            {
                "name": "main",
                "ws_url": "ws://127.0.0.1:3001",
                "allowed_users": [123, "456"],
                "group_ids": [789],
                "allow_all": False,
                "admin_qq": "100000001",
                "http_api_url": "http://127.0.0.1:3000",
            }
        ]
    })

    assert multi_account is True
    assert list(connections) == ["main"]
    assert default_conn is connections["main"]
    assert default_conn.allowed_users == ["123", "456"]
    assert default_conn.group_ids == ["789"]
    assert default_conn.admin_qq == "100000001"
    assert default_conn.http_api_url == "http://127.0.0.1:3000"


def test_install_shared_state_owns_runtime_maps_and_injected_media_cache():
    obj = type("RuntimeHolder", (), {})()
    cache = _FakeMediaCache()

    install_shared_state(obj, {"show_qq_id": True}, {"http_client": "client", "media_cache": cache})

    assert obj._http_client == "client"
    assert obj._show_qq_id is True
    assert isinstance(obj._settings_lock, asyncio.Lock)
    for attr in (
        "_chat_msg_seq", "_msg_receive_seq", "_last_msg_id",
        "_pending_approvals", "_pending_approval_admin", "_pending_approval_messages", "_approval_locks",
        "_pending_update_chats", "_last_progress_msg", "_in_edit_resend_count",
        "_active_input_status", "_active_tasks", "_reject_notified",
        "_recent_outbound_media",
    ):
        assert getattr(obj, attr) == {}
    assert obj._unsupported_actions == set()
    assert obj._delete_msg_supported is True
    assert obj._delete_msg_circuit == {"failures": 0, "opened_until": 0.0}
    assert obj._bg_delete_tasks == set()
    assert obj._last_seq_cleanup_time == 0
    assert obj._media_cache is cache


def test_adapter_facade_uses_runtime_helpers(monkeypatch):
    for key in ("ONEBOT_WS_URL", "ONEBOT_ALLOWED_USERS", "ONEBOT_GROUP_IDS", "ONEBOT_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)

    bot = OneBotAdapter({"extra": {"allowed_users": ["12345"], "show_qq_id": True}}, media_cache=_FakeMediaCache())

    assert list(bot._connections) == ["default"]
    assert bot._default_conn.allowed_users == ["12345"]
    assert bot._show_qq_id is True
    assert bot._active_tasks == {}
    assert isinstance(bot._media_cache, _FakeMediaCache)
