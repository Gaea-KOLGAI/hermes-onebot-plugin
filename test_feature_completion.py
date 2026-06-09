import asyncio
from types import SimpleNamespace

from adapter import OneBotAdapter
from onebot_platform.parsing.segments import _extract_typed_segments


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
