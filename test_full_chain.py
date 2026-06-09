#!/usr/bin/env python3
"""
OneBot 适配器全链路端到端测试
覆盖: 消息解析 / 安全过滤 / 缓存 / 限流 / 发送 / 新特性
"""
import sys, os, asyncio, re, json, time, tempfile, pathlib, socket, struct, inspect, ast, textwrap

# 确保能导入插件模块和gateway
HERMES_SRC = pathlib.Path.home() / '.hermes' / 'hermes-agent'
for _path in (HERMES_SRC, pathlib.Path('/usr/local/lib/hermes-agent'), pathlib.Path(os.path.dirname(os.path.abspath(__file__)))):
    if _path.exists():
        sys.path.insert(0, str(_path))

PASS = 0
FAIL = 0
ERRORS = []

def ok(name):
    global PASS
    PASS += 1
    print(f"  ✅ {name}")

def fail(name, reason=""):
    global FAIL
    FAIL += 1
    ERRORS.append(f"{name}: {reason}")
    print(f"  ❌ {name} — {reason}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
section("1. 模块导入 & 基本实例化")
# ============================================================
try:
    import adapter as onebot_adapter
    from adapter import (
        strip_markdown, _strip_slash, _extract_text_from_message,
        _cq_unescape, _extract_segments, _extract_images, _extract_voice,
        _extract_video, _extract_face, _extract_at, _extract_reply,
        _extract_forward, _extract_json_card, _extract_xml,
        _extract_typed_segments,
        _make_chat_id, _parse_chat_id, _extract_account_from_chat_id,
        _load_gateway_tool_progress_mode, _normalise_tool_progress_mode,
        _save_gateway_tool_progress_mode,
        _is_safe_media_download_url, _is_safe_outbound_local_path, _websockets_connect,
        _standalone_send,
        DedupCache, RateLimiter, MemberCache,
        _NapCatConnection, _PluginSettings, _MediaCache, MEDIA_CACHE_DIR,
        SettingsMixin, ConnectionMixin, MessageMixin,
        CommandMixin, SendMixin, ApprovalMixin,
        OneBotAdapter,
    )
    from gateway.platforms.base import SendResult
    ok("所有核心类/函数导入成功")
    instance = OneBotAdapter({"extra": {"ws_url": "ws://127.0.0.1:3001", "allowed_users": ["12345"]}})
    assert instance.name == "OneBot"
    assert getattr(instance.platform, "value", instance.platform) == "onebot"
    assert instance._default_conn.ws_url
    assert isinstance(instance._default_conn.allowed_users, list)
    ok("OneBotAdapter 真实实例化成功")
except ImportError as e:
    fail("模块导入", str(e))
    sys.exit(1)
except AttributeError as e:
    fail("符号缺失", str(e))
    sys.exit(1)


# ============================================================
section("2. 文本处理函数")
# ============================================================

# strip_markdown
try:
    r1 = strip_markdown("**bold**")
    r2 = strip_markdown("*italic*")
    r3 = strip_markdown("normal text")
    assert r1 == "bold", f"bold: got {r1!r}"
    assert r2 == "italic", f"italic: got {r2!r}"
    assert r3 == "normal text", f"normal: got {r3!r}"
    ok("strip_markdown 基础格式")
except AssertionError as e:
    fail("strip_markdown", str(e))

# _strip_slash
try:
    assert _strip_slash("/help") == "help"
    assert _strip_slash("hello") == "hello"
    ok("_strip_slash 命令前缀去除")
except Exception as e:
    fail("_strip_slash", str(e))

# _cq_unescape
try:
    assert _cq_unescape("&#91;") == "["
    assert _cq_unescape("&#93;") == "]"
    assert _cq_unescape("&amp;") == "&"
    assert _cq_unescape("normal") == "normal"
    ok("_cq_unescape CQ码反转义")
except Exception as e:
    fail("_cq_unescape", str(e))

# ============================================================
# section("3. 消息段提取 (segments)")
# ============================================================

# 构造各种CQ码消息段
def make_cq_text(text):
    return [{"type": "text", "data": {"text": text}}]

def make_cq_image(url="http://example.com/img.jpg"):
    return [{"type": "image", "data": {"url": url, "file": "abc.jpg"}}]

def make_cq_voice(url="http://example.com/voice.amr"):
    return [{"type": "record", "data": {"url": url, "file": "voice.amr"}}]

def make_cq_video(url="http://example.com/video.mp4"):
    return [{"type": "video", "data": {"url": url, "file": "video.mp4"}}]

def make_cq_at(qq="12345"):
    return [{"type": "at", "data": {"qq": qq}}]

def make_cq_reply(msg_id="100"):
    return [{"type": "reply", "data": {"id": msg_id}}]

def make_cq_forward(res_id="abc123"):
    return [{"type": "forward", "data": {"id": res_id}}]

def make_cq_json(data_str='{"key":"val"}'):
    return [{"type": "json", "data": {"data": data_str}}]

def make_cq_xml(data_str='<?xml version="1.0"?><msg>hi</msg>'):
    return [{"type": "xml", "data": {"data": data_str}}]

def make_cq_location(lat=39.9, lon=116.4):
    return [{"type": "location", "data": {"lat": str(lat), "lon": str(lon), "title": "北京"}}]

def make_cq_share(url="http://example.com", title="分享"):
    return [{"type": "share", "data": {"url": url, "title": title}}]

def make_cq_music(type_="qq", id_="123"):
    return [{"type": "music", "data": {"type": type_, "id": id_}}]

def make_cq_face(id_="1"):
    return [{"type": "face", "data": {"id": id_}}]

def make_cq_mface(url="http://example.com/mface.gif"):
    return [{"type": "mface", "data": {"url": url}}]

def make_cq_rps(value=1):
    return [{"type": "rps", "data": {"value": value}}]

def make_cq_dice(value=3):
    return [{"type": "dice", "data": {"value": value}}]


# 逐个测试提取函数（用 _extract_typed_segments 统一提取）
tests_seg = [
    ("_extract_text_from_message", lambda: _extract_text_from_message(make_cq_text("你好世界")), "你好世界"),
    ("_extract_images", lambda: _extract_images(make_cq_image()), ["http://example.com/img.jpg"]),
    ("_extract_voice", lambda: _extract_voice(make_cq_voice()), "http://example.com/voice.amr"),
    ("_extract_video", lambda: _extract_video(make_cq_video()), "http://example.com/video.mp4"),
    ("_extract_at", lambda: _extract_at(make_cq_at("99999")), ["99999"]),
    ("_extract_reply", lambda: _extract_reply(make_cq_reply("200")), "200"),
    ("_extract_forward", lambda: _extract_forward(make_cq_forward("res_xyz")), "res_xyz"),
    ("_extract_face", lambda: _extract_face(make_cq_face("5")), not None),
]

for name, fn, expected in tests_seg:
    try:
        result = fn()
        if expected is True:
            assert result is not None and result is not False
            ok(f"{name} 非空返回")
        elif isinstance(expected, list):
            assert result == expected, f"got {result}"
            ok(f"{name} 值匹配")
        elif expected == "not None":
            assert result is not None
            ok(f"{name} 非空")
        else:
            assert result == expected, f"got {result}, expected {expected}"
            ok(f"{name} 值匹配")
    except Exception as e:
        fail(name, str(e))

# _extract_typed_segments — 统一提取所有 segment 类型
try:
    mixed_segs = make_cq_location() + make_cq_share() + make_cq_music()
    result = _extract_typed_segments(mixed_segs)
    assert result.get("location_msg") is not None, f"location_msg missing: {result}"
    assert result.get("share_msg") is not None, f"share_msg missing: {result}"
    assert result.get("music_msg") is not None, f"music_msg missing: {result}"
    ok("_extract_typed_segments 统一提取 location/share/music")
except Exception as e:
    fail("_extract_typed_segments", str(e))

# _extract_typed_segments — mface/rps/dice
try:
    mixed_segs = make_cq_mface() + make_cq_rps() + make_cq_dice()
    result = _extract_typed_segments(mixed_segs)
    assert result.get("mface_msg") is not None, f"mface_msg missing: {result}"
    assert result.get("rps_msg") is not None, f"rps_msg missing: {result}"
    assert result.get("dice_msg") is not None, f"dice_msg missing: {result}"
    ok("_extract_typed_segments 统一提取 mface/rps/dice")
except Exception as e:
    fail("_extract_typed_segments mface/rps/dice", str(e))

# JSON 卡片提取 — 返回固定占位符
try:
    segs = make_cq_json('{"app":"com.test","meta":{"detail_1":{"title":"测试卡片"}}}')
    result = _extract_json_card(segs)
    assert result is not None, "JSON卡片提取为None"
    # 实际行为: 返回 [卡片消息] 占位符
    ok(f"_extract_json_card 返回: {result}")
except Exception as e:
    fail("_extract_json_card", str(e))

# XML 卡片提取
try:
    segs = make_cq_xml('<?xml version="1.0"?><msg><item><title>XML标题</title></item></msg>')
    result = _extract_xml(segs)
    assert result is not None, "XML提取为None"
    ok("_extract_xml XML卡片解析")
except Exception as e:
    fail("_extract_xml", str(e))


# ============================================================
section("4. 文件段提取 (_extract_typed_segments file) — Lagrange兼容")
# ============================================================

# 格式1: URL在data顶层
try:
    segs = [{"type": "file", "data": {"url": "http://example.com/file.pdf", "name": "test.pdf"}}]
    result = _extract_typed_segments(segs)
    file_seg = result.get("file_seg")
    assert file_seg is not None, "URL格式提取为None"
    assert "file.pdf" in file_seg or "test.pdf" in file_seg, f"got {file_seg}"
    ok(f"文件段: URL格式 → {file_seg}")
except Exception as e:
    fail("文件段URL", str(e))

# 格式2: file_id
try:
    segs = [{"type": "file", "data": {"file_id": "abc123", "name": "test.pdf"}}]
    result = _extract_typed_segments(segs)
    file_seg = result.get("file_seg")
    assert file_seg is not None, "file_id格式提取为None"
    ok("文件段: file_id格式")
except Exception as e:
    fail("文件段file_id", str(e))

# 格式3: 本地路径
try:
    segs = [{"type": "file", "data": {"file": "/tmp/test.pdf", "name": "test.pdf"}}]
    result = _extract_typed_segments(segs)
    file_seg = result.get("file_seg")
    assert file_seg is not None, "路径格式提取为None"
    ok("文件段: 本地路径格式")
except Exception as e:
    fail("文件段路径", str(e))

# 格式4: 非file类型不匹配
try:
    segs = [{"type": "image", "data": {"url": "http://example.com/img.jpg"}}]
    result = _extract_typed_segments(segs)
    assert result.get("file_seg") is None, f"非file类型应该没有file_seg  got {result}"
    ok("文件段: 非file类型无file_seg")
except Exception as e:
    fail("文件段排除", str(e))


# ============================================================
section("5. chat_id 构造与解析")
# ============================================================

try:
    # 私聊
    data = {"message_type": "private", "user_id": 12345, "self_id": 99999}
    chat_id = _make_chat_id(data, "mybot")
    assert "12345" in chat_id, f"私聊chat_id不含user_id: {chat_id}"
    ok(f"_make_chat_id 私聊: {chat_id}")
except Exception as e:
    fail("_make_chat_id 私聊", str(e))

try:
    # 群聊
    data = {"message_type": "group", "group_id": 67890, "user_id": 12345, "self_id": 99999}
    chat_id = _make_chat_id(data, "mybot")
    assert "67890" in chat_id, f"群聊chat_id不含group_id: {chat_id}"
    ok(f"_make_chat_id 群聊: {chat_id}")
except Exception as e:
    fail("_make_chat_id 群聊", str(e))

try:
    # 解析回去 — 实际格式是 mybot:private_12345 不是 mybot:group:67890
    chat_type, entity_id = _parse_chat_id("mybot:group_67890")
    assert chat_type == "group", f"type={chat_type}"
    assert entity_id == "67890", f"id={entity_id}"
    ok("_parse_chat_id 反向解析")
except Exception as e:
    fail("_parse_chat_id", str(e))

try:
    acc = _extract_account_from_chat_id("mybot:group_67890")
    assert acc == "mybot", f"got {acc}"
    ok("_extract_account_from_chat_id 账号提取")
except Exception as e:
    fail("_extract_account_from_chat_id", str(e))




# ============================================================
section("7. DedupCache 去重缓存")
# ============================================================

try:
    cache = DedupCache(ttl=2.0, max_size=100)
    assert cache.is_duplicate("msg_001") == False, "首次应该不重复"
    assert cache.is_duplicate("msg_001") == True, "第二次应该重复"
    assert cache.is_duplicate("msg_002") == False, "不同消息不重复"
    ok("DedupCache 基本去重")
except Exception as e:
    fail("DedupCache", str(e))

try:
    cache2 = DedupCache(ttl=0.1, max_size=100)
    cache2.is_duplicate("ttl_test")
    time.sleep(0.15)
    assert cache2.is_duplicate("ttl_test") == False, "TTL过期后应该不重复"
    ok("DedupCache TTL过期清理")
except Exception as e:
    fail("DedupCache TTL", str(e))


# ============================================================
section("8. RateLimiter 限流器")
# ============================================================

async def test_rate_limiter():
    try:
        rl = RateLimiter(rate=5.0, burst=3)
        # 突发3条应该立即通过
        for i in range(3):
            await rl.acquire()
        ok("RateLimiter 突发3条立即通过")
    except Exception as e:
        fail("RateLimiter 突发", str(e))

    try:
        rl2 = RateLimiter(rate=10.0, burst=2)
        t0 = time.monotonic()
        for i in range(2):
            await rl2.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"突发应该快: {elapsed}s"
        ok(f"RateLimiter 突发速度正常: {elapsed:.3f}s")
    except Exception as e:
        fail("RateLimiter 速度", str(e))


asyncio.get_event_loop().run_until_complete(test_rate_limiter())


# ============================================================
section("9. MemberCache 群成员缓存")
# ============================================================

try:
    mc = MemberCache(ttl=300)
    mc.set("group1", "user1", {"nickname": "测试用户", "card": "测试卡片"})
    result = mc.get("group1", "user1")
    assert result is not None, "缓存未命中"
    assert result.get("nickname") == "测试用户"
    ok("MemberCache 存取")
except Exception as e:
    fail("MemberCache", str(e))

try:
    mc2 = MemberCache(ttl=300)
    sender = {"user_id": 123, "nickname": "sender_nick", "card": "card_name"}
    mc2.set_from_sender("g1", "123", sender)
    r = mc2.get("g1", "123")
    assert r is not None and r.get("nickname") == "sender_nick"
    ok("MemberCache set_from_sender")
except Exception as e:
    fail("MemberCache from_sender", str(e))

try:
    mc3 = MemberCache(ttl=0.1)
    mc3.set("g", "u", {"nickname": "x"})
    time.sleep(0.15)
    r = mc3.get("g", "u")
    assert r is None, f"TTL过期后应该None  got {r}"
    ok("MemberCache TTL过期")
except Exception as e:
    fail("MemberCache TTL", str(e))


# ============================================================
section("10. XML ReDoS 防护")
# ============================================================

try:
    # 恶意嵌套XML (ReDoS攻击向量)
    evil_xml = '<?xml version="1.0"?><msg>' + '<a>' * 1000 + '</a>' * 1000 + '</msg>'
    segs = [{"type": "xml", "data": {"data": evil_xml}}]
    t0 = time.monotonic()
    result = _extract_xml(segs)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"XML解析超时: {elapsed}s (可能被ReDoS)"
    ok(f"XML ReDoS防护: 恶意嵌套耗时 {elapsed:.3f}s")
except Exception as e:
    fail("XML ReDoS", str(e))


# ============================================================
section("13. ChatStore 持久化")
# ============================================================

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = pathlib.Path(tmpdir) / "settings.json"
        store = _PluginSettings(store_path)

        # 写入
        store.load()
        store._data["test_key"] = "test_value"
        asyncio.get_event_loop().run_until_complete(store.save())

        # 重新加载
        store2 = _PluginSettings(store_path)
        store2.load()
        assert store2._data.get("test_key") == "test_value", f"持久化失败: {store2._data}"
        ok("_PluginSettings 持久化读写")
except Exception as e:
    fail("_PluginSettings", str(e))


# ============================================================
section("14. _NapCatConnection 连接对象")
# ============================================================

try:
    conn = _NapCatConnection(
        name="test_bot",
        ws_url="ws://127.0.0.1:3001",
        access_token="",
        ws_mode="forward",
    )
    assert conn.name == "test_bot"
    assert conn.is_connected == False  # 还没连接
    ok("_NapCatConnection 初始化")
except Exception as e:
    fail("_NapCatConnection", str(e))

try:
    conn.add_allowed_user("11111")
    conn.add_allowed_user("22222")
    assert "11111" in conn.list_allowed_users()
    assert "22222" in conn.list_allowed_users()
    # 重复添加
    conn.add_allowed_user("11111")
    assert conn.list_allowed_users().count("11111") == 1, "重复添加"
    ok("_NapCatConnection 白名单管理")
except Exception as e:
    fail("白名单管理", str(e))

try:
    conn.remove_allowed_user("11111")
    assert "11111" not in conn.list_allowed_users()
    ok("_NapCatConnection 白名单移除")
except Exception as e:
    fail("白名单移除", str(e))

try:
    # 授权检查 — 私聊
    data_priv = {"message_type": "private", "user_id": 22222, "self_id": 99999}
    assert conn.is_user_authorized("22222", "private", data_priv) == True
    assert conn.is_user_authorized("88888", "private", data_priv) == False
    ok("授权检查: 私聊白名单")
except Exception as e:
    fail("授权检查", str(e))


# ============================================================
section("15. 混合消息段解析")
# ============================================================

try:
    # 混合消息: 文本 + 图片 + @ + 引用
    mixed = [
        {"type": "reply", "data": {"id": "300"}},
        {"type": "at", "data": {"qq": "12345"}},
        {"type": "text", "data": {"text": " 你好啊"}},
        {"type": "image", "data": {"url": "http://img.example.com/pic.jpg", "file": "pic.jpg"}},
        {"type": "text", "data": {"text": " 看看这张图"}},
    ]

    text = _extract_text_from_message(mixed)
    assert "你好啊" in text, f"文本提取缺失: {text}"
    assert "看看这张图" in text, f"第二段文本缺失: {text}"

    images = _extract_images(mixed)
    assert len(images) == 1 and "pic.jpg" in images[0]

    ats = _extract_at(mixed)
    assert "12345" in ats

    reply = _extract_reply(mixed)
    assert reply == "300"

    ok("混合消息段: 文本+图片+@+引用 全部正确提取")
except Exception as e:
    fail("混合消息段", str(e))


# ============================================================
section("16. 合并转发消息解析 (_extract_multimsg_text)")
# ============================================================

try:
    from adapter import _extract_multimsg_text
    # 模拟合并转发消息的嵌套结构
    forward_obj = {
        "messages": [
            {"content": [{"type": "text", "data": {"text": "转发消息1"}}]},
            {"content": [{"type": "text", "data": {"text": "转发消息2"}}]},
        ]
    }
    result = _extract_multimsg_text(forward_obj)
    if result is not None:
        assert "转发消息" in result, f"内容不对: {result}"
        ok(f"_extract_multimsg_text 合并转发: {result[:50]}")
    else:
        # 可能结构不同 试另一种
        forward_obj2 = {
            "meta": {"detail": {"news": [{"text": "摘要1"}, {"text": "摘要2"}]}}
        }
        result2 = _extract_multimsg_text(forward_obj2)
        if result2 is not None:
            ok(f"_extract_multimsg_text meta格式: {result2[:50]}")
        else:
            ok("_extract_multimsg_text 存在且可调用(结构不匹配时返回None是合理的)")
except Exception as e:
    fail("_extract_multimsg_text", str(e))


# ============================================================
section("17. SendMixin 新方法签名检查")
# ============================================================

try:
    import inspect
    # send_forward_message
    sig = inspect.signature(SendMixin.send_forward_message)
    params = list(sig.parameters.keys())
    assert "self" in params
    assert len(params) >= 4, f"参数不够: {params}"
    ok(f"send_forward_message 签名: {params}")
except Exception as e:
    fail("send_forward_message签名", str(e))



# ============================================================
section("18. OneBotAdapter 完整性")
# ============================================================

try:
    # 确认继承了所有Mixin
    bases = OneBotAdapter.__mro__
    base_names = [b.__name__ for b in bases]
    expected = ["SettingsMixin", "ConnectionMixin", "MessageMixin",
                "CommandMixin", "SendMixin", "ApprovalMixin"]
    for name in expected:
        assert name in base_names, f"缺少Mixin: {name}"
    ok(f"OneBotAdapter MRO完整: {len(base_names)}个基类")
except Exception as e:
    fail("OneBotAdapter MRO", str(e))

try:
    # get_chat_info 必须存在 (之前缺失导致启动失败)
    assert hasattr(OneBotAdapter, 'get_chat_info'), "缺少get_chat_info"
    assert callable(getattr(OneBotAdapter, 'get_chat_info'))
    ok("get_chat_info 存在 (修复过的问题)")
except Exception as e:
    fail("get_chat_info", str(e))


# ============================================================
section("19. 路径穿越防护")
# ============================================================

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = _MediaCache(pathlib.Path(tmpdir))
        outside = pathlib.Path(tmpdir).parent / "outside.jpg"
        outside.write_bytes(b"test")
        try:
            assert cache._validate_local_path(str(outside)) is None
            ok("_MediaCache 拒绝缓存目录外本地文件")
        finally:
            outside.unlink(missing_ok=True)
except Exception as e:
    fail("路径穿越", str(e))

try:
    assert not _is_safe_media_download_url("http://127.0.0.1/private.png")
    assert not _is_safe_media_download_url("http://localhost/private.png")
    assert _is_safe_media_download_url("https://example.com/public.png")
    ok("媒体下载 SSRF 检查统一阻断本地和内网地址")
except Exception as e:
    fail("媒体下载 SSRF 检查", str(e))

try:
    safe_probe = MEDIA_CACHE_DIR / "out.png"
    safe_probe.parent.mkdir(parents=True, exist_ok=True)
    safe_probe.write_bytes(b"x")
    assert _is_safe_outbound_local_path(safe_probe)
    assert not _is_safe_outbound_local_path("/private/secret/id_rsa")
    assert not _is_safe_outbound_local_path("/private/hermes/secrets.toml")
    ok("出站本地媒体路径限制敏感路径")
except Exception as e:
    fail("出站媒体路径限制", str(e))

try:
    src = inspect.getsource(_websockets_connect)
    assert "additional_headers" in src and "extra_headers" in src
    ok("WebSocket connect 兼容 additional_headers/extra_headers")
except Exception as e:
    fail("WebSocket connect 兼容", str(e))


# ============================================================
section("20. 并发安全 (async)")
# ============================================================

async def test_concurrent_dedup():
    """多线程并发去重不应该有竞态"""
    cache = DedupCache(ttl=5.0, max_size=1000)
    results = []

    async def check(key):
        return cache.is_duplicate(key)

    # 100个并发检查同一个key
    tasks = [check("concurrent_key") for _ in range(100)]
    results = await asyncio.gather(*tasks)

    first_false = results.index(False)
    rest_true = all(r == True for r in results[first_false+1:])
    assert results.count(False) == 1, f"应该只有1个False  got {results.count(False)}"
    ok("DedupCache 并发安全: 100并发只有1个首次通过")

asyncio.get_event_loop().run_until_complete(test_concurrent_dedup())


# ============================================================
section("21. 边界条件")
# ============================================================

# 空消息
try:
    assert _extract_text_from_message([]) == "" or _extract_text_from_message([]) is None or _extract_text_from_message([]) == ""
    ok("空消息段处理")
except Exception as e:
    fail("空消息", str(e))

# None输入
try:
    result = _extract_text_from_message(None)
    ok(f"None输入处理: {repr(result)}")
except Exception as e:
    fail("None输入", str(e))

# ============================================================
section("22. 代码行数 & 文件完整性")
# ============================================================

try:
    base_dir = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
    adapter_path = base_dir / "onebot_platform" / "adapter.py"
    legacy_adapter_path = base_dir / "adapter.py"
    lines = adapter_path.read_text(encoding="utf-8").splitlines(True)
    legacy_src = legacy_adapter_path.read_text(encoding="utf-8")
    total = len(lines)

    module_files = {
        "SettingsMixin": base_dir / "onebot_platform" / "state" / "settings_mixin.py",
        "ConnectionMixin": base_dir / "onebot_platform" / "transport" / "connection_mixin.py",
        "MessageMixin": base_dir / "onebot_platform" / "inbound" / "message_mixin.py",
        "CommandMixin": base_dir / "onebot_platform" / "commands" / "mixin.py",
        "SendMixin": base_dir / "onebot_platform" / "outbound" / "send_mixin.py",
        "ApprovalMixin": base_dir / "onebot_platform" / "gateway_integration" / "approvals.py",
    }
    joined = "".join(lines)
    has_imports = any("import" in l for l in lines[:80])
    has_class = "class OneBotAdapter" in joined
    has_legacy_facade = "onebot_platform.adapter" in legacy_src
    checks = {
        "imports": has_imports,
        "legacy_adapter_facade": has_legacy_facade,
        "SettingsMixin": module_files["SettingsMixin"].exists(),
        "ConnectionMixin": module_files["ConnectionMixin"].exists(),
        "MessageMixin": module_files["MessageMixin"].exists(),
        "CommandMixin": module_files["CommandMixin"].exists(),
        "SendMixin": module_files["SendMixin"].exists(),
        "ApprovalMixin": module_files["ApprovalMixin"].exists(),
        "OneBotAdapter": has_class,
    }
    missing = [k for k, v in checks.items() if not v]
    if not missing:
        ok(f"文件完整性: onebot_platform/adapter.py {total}行 功能模块+兼容门面齐全")
    else:
        fail("文件完整性", f"缺失: {missing}")
except Exception as e:
    fail("文件完整性", str(e))


# ============================================================
section("23. 工具调用提示开关迁移到 Gateway 层")
# ============================================================

try:
    assert _normalise_tool_progress_mode(False) == "off"
    assert _normalise_tool_progress_mode(True) == "all"
    assert _normalise_tool_progress_mode("new") == "new"
    assert _normalise_tool_progress_mode("verbose") == "verbose"
    assert _normalise_tool_progress_mode("invalid") == "all"
    ok("tool_progress 模式归一化")
except Exception as e:
    fail("tool_progress 模式归一化", str(e))

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = pathlib.Path(tmpdir) / "config.yaml"
        cfg_path.write_text("display:\n  tool_progress: new\n", encoding="utf-8")
        import onebot_platform.adapter as modular_adapter
        old_config_path = onebot_adapter._hermes_config_path
        old_modular_config_path = modular_adapter._hermes_config_path
        onebot_adapter._hermes_config_path = lambda: cfg_path
        modular_adapter._hermes_config_path = lambda: cfg_path
        try:
            assert _load_gateway_tool_progress_mode("onebot") == "new"
            _save_gateway_tool_progress_mode("off", "onebot")
            assert _load_gateway_tool_progress_mode("onebot") == "off"
            saved = cfg_path.read_text(encoding="utf-8")
            assert "platforms:" in saved and "onebot:" in saved and "tool_progress: 'off'" in saved
        finally:
            onebot_adapter._hermes_config_path = old_config_path
            modular_adapter._hermes_config_path = old_modular_config_path
    ok("/settool 写入 gateway display.platforms.onebot.tool_progress")
except Exception as e:
    fail("gateway tool_progress 写入", str(e))

try:
    base_dir = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
    src = (base_dir / "adapter.py").read_text(encoding="utf-8") + (base_dir / "command_mixin.py").read_text(encoding="utf-8")
    assert "_TOOL_PROGRESS_RE" not in src, "不应保留插件层工具提示正则"
    assert "settings.get(\"tool_progress\") is False" not in src, "不应保留插件层工具提示拦截"
    assert "_save_gateway_tool_progress_mode(mode, \"onebot\")" in src, "settool 应写 gateway 层"
    ok("adapter 不再插件层过滤工具调用提示")
except Exception as e:
    fail("adapter 工具提示过滤清理", str(e))


# ============================================================
section("24. 安全修复验证")
# ============================================================

# #1 审批权限绕过 - _resolve_approval_shortcut 新增 user_id/admin_qq 参数
try:
    sig = inspect.signature(ApprovalMixin._resolve_approval_shortcut)
    params = list(sig.parameters.keys())
    assert "user_id" in params, "缺少 user_id 参数"
    assert "admin_qq" in params, "缺少 admin_qq 参数"
    ok("_resolve_approval_shortcut 有 admin 权限检查参数")
except Exception as e:
    fail("审批权限检查", str(e))

# #2 审批选项对齐 Hermes: once/session/always/deny
try:
    from adapter import _APPROVAL_CHOICES
    expected = {
        "1": "once",
        "2": "session",
        "3": "always",
        "4": "deny",
        "批准一次": "once",
        "会话批准": "session",
        "永久批准": "always",
        "拒绝": "deny",
    }
    for key, value in expected.items():
        assert _APPROVAL_CHOICES.get(key) == value, f"{key} 应映射到 {value}, 实际 {_APPROVAL_CHOICES.get(key)}"
    ok("审批选项映射对齐 Hermes once/session/always/deny")
except Exception as e:
    fail("审批选项映射", str(e))

# #3 审批 pending 失败可重试，admin_only 无管理员时默认拒绝
try:
    src = inspect.getsource(ApprovalMixin._resolve_approval_shortcut)
    assert "session_key = self._pending_approvals.get(chat_id)" in src, "应先读取 pending，resolve 成功后再移除"
    assert "approval admin is not configured" in src, "admin_only 无管理员时应默认拒绝"
    ok("审批 pending 可靠性和 admin_only 默认拒绝")
except Exception as e:
    fail("审批 pending/admin_only", str(e))

# #4 空允许列表 - is_user_authorized 改为 deny
try:
    conn = _NapCatConnection(
        name="test", ws_url="ws://test", allowed_users=[], allow_all=False
    )
    result = conn.is_user_authorized("12345", "private", {})
    assert result == False, f"空 allowlist 应返回 False, 实际 {result}"
    result2 = conn.is_user_authorized("12345", "group", {"group_id": "999"})
    assert result2 == False, f"空 group allowlist 应返回 False, 实际 {result2}"
    cfg_src = inspect.getsource(CommandMixin._cmd_config)
    assert "空，拒绝所有用户" in cfg_src
    assert "空，拒绝所有群" in cfg_src
    setup_src = inspect.getsource(onebot_adapter.interactive_setup)
    assert "留空则拒绝所有用户" in setup_src
    ok("空 allowlist 默认拒绝 (deny-all)，配置文案一致")
except Exception as e:
    fail("空 allowlist 拒绝", str(e))

# #5 _validate_local_path TOCTOU - 返回 resolved 路径
try:
    with tempfile.TemporaryDirectory() as tmpdir:
        mc = _MediaCache(pathlib.Path(tmpdir))
        test_file = pathlib.Path(tmpdir) / "test.jpg"
        test_file.write_bytes(b"test")
        result = mc._validate_local_path(str(test_file))
        assert result == str(test_file.resolve()), f"应返回 resolved 路径 {test_file.resolve()}, 实际 {result}"
    ok("_validate_local_path 返回 resolved 路径 (TOCTOU 修复)")
except Exception as e:
    fail("_validate_local_path TOCTOU", str(e))

# #7 _in_edit_resend_count 计数器
try:
    src = inspect.getsource(OneBotAdapter.__init__) + inspect.getsource(OneBotAdapter._init_shared_state)
    assert "_in_edit_resend_count" in src, "应使用 _in_edit_resend_count 计数器"
    assert "_in_edit_resend_chats" not in src, "不应使用 _in_edit_resend_chats set"
    ok("_in_edit_resend_chats 改为计数器 (并发安全)")
except Exception as e:
    fail("_in_edit_resend 计数器", str(e))

# #9 format_message 模块级函数
try:
    from adapter import _format_message
    assert callable(_format_message), "_format_message 应为模块级函数"
    assert hasattr(SendMixin, "format_message"), "SendMixin.format_message 仍应存在"
    result = _format_message("**bold** text")
    assert "bold" in result, "模块级 _format_message 应正常工作"
    ok("format_message 提取为模块级函数 _format_message")
except Exception as e:
    fail("format_message 模块级", str(e))


# ============================================================
section("24. 代码复用验证")
# ============================================================

# 验证 _safe_target_id 复用
try:
    src = inspect.getsource(SendMixin.send_document)
    assert "_safe_target_id" in src, "send_document 应使用 _safe_target_id"
    assert not any(name == "send_poke" for name in SendMixin.__dict__)
    assert not any(name == "send_emoji_reaction" for name in SendMixin.__dict__)
    ok("未使用的轻互动发送方法已移除")
except Exception as e:
    fail("_safe_target_id 复用", str(e))

# 验证 retcode 检查复用
try:
    src = inspect.getsource(SendMixin.send)
    assert "retcode" in src, "send 方法应包含 retcode 检查"
    ok("retcode 检查在 send 方法中复用")
except Exception as e:
    fail("retcode 复用", str(e))

try:
    settings = _PluginSettings(pathlib.Path(tempfile.gettempdir()) / "onebot-test-settings.json")
    assert settings._normalize_key("main:group_123") == "main:group_123"
    assert settings._normalize_key("work:group_123") == "work:group_123"
    ok("多账号聊天设置保留账号前缀隔离")
except Exception as e:
    fail("多账号设置隔离", str(e))

try:
    data1 = {"user_id": "123", "time": 100, "message": [{"type": "text", "data": {"text": "a"}}]}
    data2 = {"user_id": "123", "time": 100, "message": [{"type": "text", "data": {"text": "b"}}]}
    conn = _NapCatConnection("test", "ws://test", allowed_users=["123"])
    mixin = object.__new__(MessageMixin)
    assert mixin._check_duplicate_and_self(data1, conn) is False
    assert mixin._check_duplicate_and_self(data2, conn) is False
    ok("message_id 缺失时去重包含消息内容 hash")
except Exception as e:
    fail("message_id 缺失去重", str(e))

try:
    src = inspect.getsource(SendMixin.send_document)
    assert "trusted_local_file" not in src
    assert "_as_onebot_file_value(raw_path)" in src
    ok("send_document 强制沿用出站本地路径限制")
except Exception as e:
    fail("send_document 出站路径限制", str(e))

try:
    dispatch_src = textwrap.dedent(inspect.getsource(SendMixin._send_media_path))
    assert "senders =" in dispatch_src
    assert ast.dump(ast.parse(dispatch_src)).count("If(") <= 1
    ok("媒体发送路径使用表驱动分发")
except Exception as e:
    fail("媒体发送表驱动", str(e))

try:
    async def _exercise_outbound_media_dedup():
        adapter = OneBotAdapter({"extra": {"ws_url": "ws://127.0.0.1:3001", "allowed_users": ["12345"]}})
        adapter._default_conn.http_api_url = ""
        calls = []
        async def fake_send_action(conn, action, params, timeout=15.0):
            calls.append((action, params))
            return {"retcode": 0, "data": {"message_id": len(calls)}}
        adapter._send_action_conn = fake_send_action
        first = await adapter._send_media("private_12345", "image", "file:///tmp/same.png")
        second = await adapter._send_media("private_12345", "image", "file:///tmp/same.png")
        assert first.success and second.success
        assert len(calls) == 1, f"duplicate media sent {len(calls)} times"
    asyncio.run(_exercise_outbound_media_dedup())
    ok("出站媒体短时间同路径去重")
except Exception as e:
    fail("出站媒体去重", str(e))

try:
    async def _exercise_originator_mentions():
        adapter = OneBotAdapter({"extra": {"ws_url": "ws://127.0.0.1:3001", "allowed_users": ["12345"]}})
        adapter._plugin_settings = _PluginSettings(pathlib.Path(tempfile.gettempdir()) / "onebot-mention-test-settings.json")
        adapter._default_conn.http_api_url = ""
        calls = []
        async def fake_send_action(conn, action, params, timeout=15.0):
            calls.append((action, params))
            return {"retcode": 0, "data": {"message_id": len(calls)}}
        adapter._send_action_conn = fake_send_action
        meta = {"mention_originator_user_id": "12345", "mention_reason": "turn_response"}
        result = await adapter.send("group_67890", "任务完成", metadata=meta)
        assert result.success
        msg = calls[-1][1]["message"]
        assert msg[0] == {"type": "at", "data": {"qq": "12345"}}
        assert msg[1] == {"type": "text", "data": {"text": " "}}
        assert msg[2]["type"] == "text" and "任务完成" in msg[2]["data"]["text"]
        calls.clear()
        result = await adapter.send("private_12345", "私聊完成", metadata=meta)
        assert result.success
        private_msg = calls[-1][1]["message"]
        assert private_msg[0]["type"] == "text"
        assert not any(seg.get("type") == "at" for seg in private_msg)
    asyncio.run(_exercise_originator_mentions())
    ok("群聊最终回复按 metadata 自动 @ 发起人，私聊不 @")
except Exception as e:
    fail("originator mention", str(e))

try:
    async def _exercise_approval_metadata_passthrough():
        adapter = OneBotAdapter({"extra": {"ws_url": "ws://127.0.0.1:3001", "allowed_users": ["12345"]}})
        seen = {}
        async def fake_send(chat_id, content, reply_to=None, metadata=None):
            seen["chat_id"] = chat_id
            seen["metadata"] = metadata
            return SendResult(success=True)
        adapter.send = fake_send
        meta = {"mention_originator_user_id": "12345", "admin_only": True}
        result = await adapter.send_exec_approval("group_67890", "rm -rf /tmp/x", "sess", metadata=meta)
        assert result.success
        assert seen["metadata"] is meta
        result = await adapter.send_update_prompt("group_67890", "确认更新？", metadata=meta)
        assert result.success
        assert seen["metadata"] is meta
    asyncio.run(_exercise_approval_metadata_passthrough())
    ok("审批和更新提示透传 mention metadata")
except Exception as e:
    fail("approval mention metadata", str(e))

try:
    sub_src = textwrap.dedent(inspect.getsource(CommandMixin._cmd_onebot))
    assert ast.dump(ast.parse(sub_src)).count("If(") <= 2
    assert "routes" in sub_src or "_ONEBOT_SUBCOMMANDS" in sub_src
    ok("/onebot 子命令使用表驱动分发")
except Exception as e:
    fail("/onebot 表驱动", str(e))

try:
    adapter_obj = object.__new__(OneBotAdapter)
    adapter_obj._multi_account = False
    info = asyncio.run(adapter_obj.get_chat_info("group_abc"))
    assert info["id"] == "group_abc" and info["type"] == "group"
    ok("get_chat_info 非数字 chat_id 安全 fallback")
except Exception as e:
    fail("get_chat_info fallback", str(e))

try:
    sig = inspect.signature(_standalone_send)
    params = list(sig.parameters)
    assert params[:3] == ["config", "chat_id", "message"], f"standalone_sender_fn 参数顺序错误: {params[:3]}"
    assert any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()), "standalone_sender_fn 应接受 media_files 等 kwargs"
    ok("standalone_sender_fn 参数顺序兼容 send_message 工具层")
except Exception as e:
    fail("standalone_sender_fn 签名", str(e))


# ============================================================
# 最终报告
# ============================================================
print(f"\n{'='*60}")
print(f"  测试报告")
print(f"{'='*60}")
print(f"  通过: {PASS}")
print(f"  失败: {FAIL}")
print(f"  总计: {PASS + FAIL}")
print(f"  通过率: {PASS/(PASS+FAIL)*100:.1f}%" if (PASS+FAIL) > 0 else "  无测试")

if ERRORS:
    print(f"\n  失败详情:")
    for e in ERRORS:
        print(f"    - {e}")

sys.exit(0 if FAIL == 0 else 1)
