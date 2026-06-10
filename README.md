# Hermes OneBot 插件

Hermes Agent 的 OneBot v11 平台适配器，主要面向 NapCat QQ 场景。它负责把 QQ 私聊、群聊、图片、语音、视频、文件、引用等事件接入 Hermes，同时支持把 Hermes 的回复、媒体和审批提示发回 QQ。

## 主要功能

- 私聊和群聊消息收发
- 群聊支持 @机器人 唤醒，也支持斜杠指令
- 图片、语音、视频、文件的接收与发送
- 支持 `MEDIA:本地路径` 发送本地媒体
- 支持多张图片和多媒体批量发送
- 支持中文路径、空格路径和本地 file URI
- 支持引用消息解析，保留上下文预览
- 支持 @提及识别和清理
- 支持 JSON 卡片、XML 卡片、分享卡片解析
- 支持 Markdown 消息段转成可读纯文本
- 支持转发节点提取发送人和内容预览
- 支持合并转发消息解析，最多递归三层
- 支持群文件上传事件和文件内容注入
- 支持文本文件自动注入内容，默认限制 64KB
- 支持私聊、群聊白名单
- 支持动态添加和移除白名单用户、群号
- 支持允许所有人使用开关
- 支持多账号，每个 NapCat 连接独立会话和权限
- 支持自动重连、心跳检测、指数退避
- 支持消息去重，避免重复事件刷屏
- 支持发送限流，避免触发平台风控
- 支持 `/settool on|off` 映射到 Hermes Gateway 层工具调用提示开关
- 支持 Markdown 清理开关
- 支持戳一戳审批危险命令
- 支持给审批提示消息贴表情批准当前会话
- 支持管理员专属审批
- 支持输入状态提示
- 支持消息编辑回退发送
- 支持转发消息等扩展发送能力，转发节点可直接传 `name`、`user_id`、`segments`
- 群聊超长纯文本自动拆分为合并转发，NapCat 不支持时回退普通文本发送
- 支持 HTTP API 作为媒体发送兜底通道
- 支持未连接 WebSocket 但配置 HTTP API 时查询群名和好友昵称
- 支持反向 WebSocket 模式下等待 NapCat 客户端就绪

## 架构概览

插件主体是兼容门面 + 功能模块组合。`adapter.py` 保留旧导入兼容，核心能力由多个 Mixin 和辅助模块组成。

| 模块 | 职责 |
|---|---|
| SettingsMixin | 配置持久化、聊天设置、账号设置恢复 |
| ConnectionMixin | WebSocket 连接、反向连接、重连、心跳、事件分发 |
| transport.lifecycle | 连接任务取消、单连接断开、自身信息拉取、强制关闭 WebSocket |
| MessageMixin | 入站消息主流程、权限检查、事件构造 |
| inbound.files | 文件 URL 解析、文本文件内容注入 |
| inbound.context | 引用消息、合并转发消息上下文解析 |
| CommandMixin | QQ 内管理指令和帮助信息 |
| SendMixin | 出站发送兼容门面，负责文本、媒体、文件、转发等公共入口 |
| outbound.media | 媒体类型识别、出站本地文件安全转换、媒体去重、媒体发送 |
| outbound.notices | OneBot notice 事件文本化和戳一戳审批入口 |
| outbound.deletion | 消息删除、后台删除和删除能力降级 |
| ApprovalMixin | 危险命令审批、更新确认、戳一戳批准 |
| OneBotAdapter | 主适配器，整合全部能力 |

辅助类包括 DedupCache、RateLimiter、MemberCache、_MediaCache、_NapCatConnection、_PluginSettings 和 _CmdDef。

## 安装方式

1. 将插件目录放到 `~/.hermes/plugins/onebot-platform`
2. 启用插件：`hermes plugins enable onebot-platform`
3. 配置 NapCat 的 WebSocket 或 HTTP API
4. 重启 Hermes Gateway

```bash
hermes plugins enable onebot-platform
hermes gateway restart
```

如果 Gateway 是手动运行的，也可以先停止旧进程，再执行：

```bash
hermes gateway run --replace
```

## 单账号配置

可以使用环境变量配置：

```bash
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ALLOWED_USERS=YOUR_QQ_ID
ONEBOT_HOME_CHANNEL=private_YOUR_QQ_ID
ONEBOT_HTTP_API_URL=http://127.0.0.1:5700
```

也可以写入 `~/.hermes/config.yaml`：

```yaml
gateway:
  platforms:
    onebot:
      enabled: true
      extra:
        ws_url: "ws://127.0.0.1:3001"
        allowed_users: ["YOUR_QQ_ID"]
        home_channel: "private_YOUR_QQ_ID"
        http_api_url: "http://127.0.0.1:5700"
        show_qq_id: false
```

## 多账号配置

多账号适合同时连接多个 NapCat 实例。每个账号有独立连接、独立白名单和独立会话。

```yaml
gateway:
  platforms:
    onebot:
      enabled: true
      extra:
        accounts:
          - name: "main"
            ws_url: "ws://127.0.0.1:3001"
            allowed_users: ["YOUR_QQ_ID"]
            home_channel: "private_YOUR_QQ_ID"
            http_api_url: "http://127.0.0.1:5700"
          - name: "work"
            ws_url: "ws://127.0.0.1:3002"
            allowed_users: ["YOUR_QQ_ID"]
            home_channel: "private_YOUR_QQ_ID"
            http_api_url: "http://127.0.0.1:5701"
```

多账号模式下，聊天 ID 会带账号前缀，例如 `main:private_123456` 或 `work:group_123456`。

## 常用指令

| 指令 | 说明 | 管理员 |
|---|---|---|
| `/onebot` 或 `/onebot help` | 查看 OneBot 插件帮助 | 否 |
| `/onebot help media` | 查看媒体能力说明 | 否 |
| `/onebot help admin` | 查看管理指令说明 | 否 |
| `/onebot config` | 查看 OneBot 当前聊天配置 | 是 |
| `/adduser <QQ号>` | 添加用户白名单 | 是 |
| `/removeuser <QQ号>` | 移除用户白名单 | 是 |
| `/listusers` | 查看用户白名单 | 是 |
| `/addgroup <群号>` | 添加群白名单 | 是 |
| `/rmgroup <群号>` | 移除群白名单 | 是 |
| `/listgroups` | 查看群白名单 | 是 |
| `/settool on|off` | 直接修改 Gateway 层 `display.platforms.onebot.tool_progress` | 是 |
| `/setmd on|off` | 开关 Markdown 清理 | 是 |
| `/setallowall on|off` | 开关允许所有人使用 | 是 |

说明：`/help`、`/config`、`/status` 是 Hermes/Gateway 原生命令；OneBot 插件自己的帮助和配置入口统一放在 `/onebot ...` 下，避免抢占原生命令。

## 媒体发送

插件支持通过 OneBot 消息段发送多种媒体：

- 图片使用 image 段
- 语音使用 record 段
- 视频使用 video 段
- 文件使用 OneBot 文件上传接口
- 本地媒体会转成安全的 `file://` URI
- HTTP API 可作为 WebSocket 超时或反向连接不稳定时的兜底通道

Hermes 的跨平台发送工具可以使用：

```text
MEDIA:/tmp/example.png
```

也可以在一条消息中混合文字和多个媒体路径。

## 文件内容注入

当用户上传文本类文件时，插件会尝试读取文件内容并注入给 Hermes，方便直接分析文件。

默认支持扩展名包括：

- txt
- md
- csv
- json
- yaml
- yml
- xml
- log
- py
- js
- ts
- html
- css
- ini
- cfg
- toml
- sh
- sql
- env

安全限制：

- 只允许读取媒体缓存目录内的文件
- 默认最大 64KB
- 拒绝路径穿越
- 拒绝符号链接逃逸
- 拒绝非白名单路径

## 审批系统

当 Hermes 触发危险命令审批时，插件会把审批提示发到 QQ。

支持方式：

- 戳一戳机器人：单次批准
- 给审批提示消息贴表情：会话批准
- 回复 `1`：单次批准
- 回复 `2`：会话批准
- 回复 `3`：永久批准
- 回复 `4`：拒绝

群聊里的戳一戳会优先匹配当前群的待审批任务，不会只查私聊。这个映射与 Hermes 审批系统的 `once`、`session`、`always`、`deny` 对齐。

## 安全设计

- SSRF 防护：所有媒体下载都会先解析域名并检查 IP 网段，复用 HTTP client 时同样执行，阻止本地和内网地址
- 媒体路径防护：入站本地媒体只接受缓存目录内已存在文件，出站本地图片、语音、视频和文档默认限制在媒体缓存和临时目录内，拒绝目录逃逸
- 文件注入白名单：只注入允许扩展名的小文件，默认最大 64KB
- Token 校验：使用常量时间比较，降低时序侧信道风险
- 并发限制：每个聊天最多同时 5 个任务
- 去重缓存：5 秒窗口，最多 2000 条
- 成员缓存：默认 300 秒 TTL，最多 5000 条
- 发送限流：默认 5 条每秒，突发 10 条

## 维护状态

当前适配器已清理确认无运行引用的旧辅助代码：

- `_sanitize_log`
- `_is_path_safe`
- `_MediaCache.get_path`

保留 `send_forward_message` 等实际使用的扩展发送方法。未使用且未实测的轻互动发送能力已移除，避免继续堆积维护成本。

适配器已拆成兼容门面和功能模块，当前约 60 个 Python 文件。`adapter.py` 已压缩为轻量装配门面，`outbound/send_mixin.py` 已进一步瘦身为出站兼容入口；媒体、notice、删除逻辑分别拆到 `outbound/media.py`、`outbound/notices.py`、`outbound/deletion.py`；入站文件注入、引用/合并转发上下文、连接生命周期分别拆到 `inbound/files.py`、`inbound/context.py`、`transport/lifecycle.py`。更新后需至少通过 `py_compile`、`pytest` 回归测试和 `test_full_chain.py`。

## 环境变量

| 变量 | 说明 | 必需 |
|---|---|---|
| `ONEBOT_WS_URL` | NapCat WebSocket 地址，单账号模式需要 | 单账号需要 |
| `ONEBOT_ACCESS_TOKEN` | NapCat 访问令牌 | 否 |
| `ONEBOT_ALLOWED_USERS` | 允许使用的 QQ 号，逗号分隔 | 否 |
| `ONEBOT_ALLOW_ALL_USERS` | 是否允许所有人使用 | 否 |
| `ONEBOT_HOME_CHANNEL` | 默认通知频道 | 否 |
| `ONEBOT_GROUP_IDS` | 允许使用的群号，逗号分隔 | 否 |
| `ONEBOT_HTTP_API_URL` | OneBot HTTP API 地址 | 否 |
| `ONEBOT_WS_MODE` | `forward` 或 `reverse` | 否 |
| `ONEBOT_ADMIN_QQ` | 管理员 QQ 号 | 否 |

## NapCat 建议

推荐同时开启：

- WebSocket 客户端或服务端
- HTTP API
- 图片、语音、视频、文件事件上报
- 群消息和私聊消息上报

如果图片发送经常超时，建议配置 `ONEBOT_HTTP_API_URL` 或 `http_api_url`。

## 故障排查

查看插件是否启用：

```bash
hermes plugins list
```

查看 Gateway 状态：

```bash
hermes gateway status
```

查看日志：

```bash
tail -100 ~/.hermes/logs/gateway.log
```

常见问题：

- 收不到群消息：确认群号在白名单内，或开启允许所有人使用
- 群聊不响应：确认消息里 @ 了机器人，或使用 `/` 指令
- 图片发不出去：配置 HTTP API 作为兜底通道
- 反向 WebSocket 不稳定：检查 NapCat 是否已连回 Hermes
- 管理指令无效：确认当前 QQ 是管理员或白名单第一位
- 文件无法注入：确认文件类型和大小在允许范围内

## 测试

在插件目录运行：

```bash
PYTHONPATH=/root/.local/share/pipx/venvs/hermes-agent/lib/python3.12/site-packages:$PWD python3 -m py_compile adapter.py __init__.py
PYTHONPATH=/root/.local/share/pipx/venvs/hermes-agent/lib/python3.12/site-packages:$PWD python3 -m pytest -q test_feature_completion.py test_review_hardening.py test_optimizations.py
PYTHONPATH=/root/.local/share/pipx/venvs/hermes-agent/lib/python3.12/site-packages:$PWD python3 test_full_chain.py
PYTHONPATH=/root/.local/share/pipx/venvs/hermes-agent/lib/python3.12/site-packages:$PWD python3 test_optimizations.py
```

当前全链路测试覆盖：

- 模块导入
- 文本处理
- 消息段解析
- Markdown 消息段和转发节点解析
- JSON/XML 卡片
- 文件段解析
- chat_id 构造与解析
- 去重缓存
- 限流器
- 成员缓存
- XML ReDoS 防护
- 设置持久化
- 连接对象
- 混合消息段
- 合并转发
- 出站长文本自动合并转发和失败回退
- 发送方法签名
- 扩展转发消息发送
- 主适配器完整性
- 路径穿越防护
- 并发安全
- 边界条件
- 安全修复验证
- 代码复用验证
- HTTP API 群/好友资料查询
- 出站本地文件安全
- OneBot HTTP 响应限长
- WebSocket 失败 HTTP 兜底
- 媒体发送失败重试和成功后去重

## 适用场景

- QQ 私聊助手
- QQ 群聊机器人
- 服务器运维通知
- Hermes 审批流
- 文件和日志分析
- 图片、语音、视频交互
- 多 NapCat 账号并行接入

## 隐私说明

仓库示例均使用占位符，不应提交真实 QQ 号、群号、访问令牌或 API Key。更新仓库前建议做一次隐私扫描，确认没有真实凭据和个人标识被提交。