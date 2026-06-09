import asyncio
from types import SimpleNamespace

from adapter import OneBotAdapter
from onebot_platform.inbound import context as inbound_context
from onebot_platform.inbound import files as inbound_files
from onebot_platform.parsing.segments import _extract_typed_segments
from onebot_platform.transport import lifecycle as transport_lifecycle


class _InfoBot(OneBotAdapter):
    def __init__(self):
        super().__init__({"extra": {"ws_url": "ws://127.0.0.1:3000/ws", "http_api_url": "http://127.0.0.1:3001", "allowed_users": ["12345"]}})
        self.actions = []

    async def _send_action_conn(self, conn, action, params, timeout=15.0):
        self.actions.append((action, params, timeout))
        if action == "get_group_info":
            return {"retcode": 0, "data": {"group_name": "测试群"}}
        if action == "get_stranger_info":
            return {"retcode": 0, "data": {"nickname": "阿漂"}}
        return {"retcode": 0, "data": {"message_id": 1}}


def test_get_chat_info_uses_http_api_when_websocket_is_not_connected():
    bot = _InfoBot()
    bot._default_conn.ws = None
    info = asyncio.run(bot.get_chat_info("group_67890"))
    assert info == {"id": "group_67890", "name": "测试群", "type": "group"}
    assert bot.actions and bot.actions[0][0] == "get_group_info"


def test_get_chat_info_private_uses_http_api_when_websocket_is_not_connected():
    bot = _InfoBot()
    bot._default_conn.ws = None
    info = asyncio.run(bot.get_chat_info("private_12345"))
    assert info == {"id": "private_12345", "name": "阿漂", "type": "dm"}
    assert bot.actions and bot.actions[0][0] == "get_stranger_info"


def test_markdown_segment_extracts_nested_content_as_plain_text():
    parsed = _extract_typed_segments([
        {"type": "markdown", "data": {"content": {"text": "# 标题\n**重点** [链接](https://a.test)"}}}
    ])
    assert parsed["markdown_msg"] == "[Markdown消息]\n标题\n重点 链接 (https://a.test)"


def test_node_segment_extracts_sender_and_content_preview():
    parsed = _extract_typed_segments([
        {"type": "node", "data": {"name": "小明", "uin": "10001", "content": [{"type": "text", "data": {"text": "节点内容"}}]}}
    ])
    assert parsed["node_msg"] == "[转发节点: 小明(10001): 节点内容]"


def test_extracted_inbound_and_transport_helpers_keep_mixin_compatibility():
    assert OneBotAdapter._inject_file_content is inbound_files.inject_file_content
    assert OneBotAdapter._resolve_file_url is inbound_files.resolve_file_url
    assert OneBotAdapter._resolve_forward_message is inbound_context.resolve_forward_message
    assert OneBotAdapter._append_reply_context is inbound_context.append_reply_context
    assert OneBotAdapter._disconnect_conn is transport_lifecycle.disconnect_conn
    assert OneBotAdapter._force_close_ws is transport_lifecycle.force_close_ws


def test_forward_context_honors_instance_limit_overrides():
    class Bot(_InfoBot):
        _FORWARD_MAX_IMAGES = 1

    bot = Bot()
    dst = []
    assert bot._take_forward_images(dst, ["a", "b"]) is True
    assert dst == ["a"]
    assert bot._take_forward_images(dst, ["c"]) is False
    bot._FORWARD_MAX_DEPTH = -1
    text, images = asyncio.run(bot._resolve_forward_message("x", bot._default_conn))
    assert text == ""
    assert images == []
    bot._FORWARD_MAX_DEPTH = 3
    bot._FORWARD_MAX_FETCHES = 0
    text, images = asyncio.run(bot._resolve_forward_message("x", bot._default_conn))
    assert text == ""
    assert images == []


def test_send_forward_message_accepts_natural_node_fields():
    class Bot(_InfoBot):
        async def _send_action_conn(self, conn, action, params, timeout=30.0):
            self.actions.append((action, params, timeout))
            return {"retcode": 0, "data": {"forward_id": "fwd-1"}}
    bot = Bot()
    result = asyncio.run(bot.send_forward_message("group_67890", [
        {"name": "小明", "user_id": "10001", "segments": [{"type": "text", "data": {"text": "你好"}}]},
    ]))
    assert result.success is True
    assert result.message_id == "fwd-1"
    params = bot.actions[0][1]
    node = params["messages"][0]["data"]
    assert node["nickname"] == "小明"
    assert node["user_id"] == 10001
    assert node["content"] == [{"type": "text", "data": {"text": "你好"}}]
