# hermes-onebot-plugin

OneBot v11 (NapCat) platform adapter plugin for Hermes Agent.

## Features

- Private & group messages (group requires @bot or `/` commands)
- Image/voice/video/file sending & receiving (parallel image download, SSRF-protected)
- Reply & @mention with context trace (up to 300 chars preview)
- Forward messages (send & receive, merged forward parsing up to 3 layers deep)
- JSON/XML card parsing (multimsg preview, title extraction)
- **HTTP API dual channel** (WS + HTTP, avoids timeout issues for image sends)
- **Text file content auto-injection** (.txt/.md/.py/.json etc., 64KB limit, allowlist-only)
- **show_qq_id** option (append QQ number to display name)
- **retcode=200 tolerance** for image sends in reverse WS mode
- Automatic reconnection with exponential backoff (2s→60s, ±20% jitter)
- Message deduplication (5s window, LRU cache, 2000 max)
- Rate limiting (5msg/s, burst 10, token bucket)
- Multi-account support with isolated state per connection
- Tool progress hints (delete+resend strategy, anti-spam)
- Markdown stripping (configurable per-chat)
- Path traversal prevention (allowlist-based), SSRF protection (DNS-resolved IP blocklist), log injection sanitization
- 11 management slash commands
- Hermes approval system integration (poke-to-approve, admin-only approval)
- Concurrent task limit per chat (max 5, prevents memory exhaustion)
- Constant-time token comparison (`hmac.compare_digest`)
- Member cache (300s TTL, 5000 max)

## Architecture

Single-file, 6 Mixins + main adapter, 2363 lines:

| Class | Responsibility | Methods |
|-------|---------------|---------|
| SettingsMixin | Config persistence & restore | 9 |
| ConnectionMixin | WS connect/reconnect/heartbeat/dispatch | 16 |
| MessageMixin | Message parsing, auth, image download, file injection | 18 |
| CommandMixin | 11 slash commands | 10 |
| SendMixin | Outbound messages, media, editing, typing, notice handling | 29 |
| ApprovalMixin | Task approval (approve/deny) & update confirmation | 4 |
| OneBotAdapter | Main class combining all Mixins | 7 |

Supporting classes: DedupCache, RateLimiter, MemberCache, _MediaCache, _NapCatConnection, _PluginSettings, _CmdDef

## Installation

1. Copy the `onebot/` directory to `~/.hermes/plugins/`
2. Enable: `hermes plugins enable onebot-platform`
3. Configure env vars or `config.yaml`
4. Restart gateway: `systemctl restart hermes-gateway`

## Configuration

### Single Account (env vars)

```bash
ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=your_token_here
ONEBOT_ALLOWED_USERS=YOUR_QQ_ID
ONEBOT_HTTP_API_URL=http://127.0.0.1:5700
```

### Single Account (config.yaml)

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

### Multi Account (config.yaml)

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
          - name: "work"
            ws_url: "ws://127.0.0.1:3002"
            allowed_users: ["YOUR_QQ_ID"]
            home_channel: "private_YOUR_QQ_ID"
```

## Commands

| Command | Description | Admin |
|---------|-------------|-------|
| `/help` | Show command list | No |
| `/config` | Show current config | Yes |
| `/adduser <QQ>` | Add to whitelist | Yes |
| `/removeuser <QQ>` | Remove from whitelist | Yes |
| `/listusers` | Show whitelist | Yes |
| `/addgroup <ID>` | Add group to whitelist | Yes |
| `/rmgroup <ID>` | Remove group from whitelist | Yes |
| `/listgroups` | Show group whitelist | Yes |
| `/settool on\|off` | Toggle tool progress hints | Yes |
| `/setmd on\|off` | Toggle Markdown stripping | Yes |
| `/setallowall on\|off` | Toggle allow-all mode | Yes |

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `ONEBOT_WS_URL` | WebSocket URL (single-account) | Single-account |
| `ONEBOT_ACCESS_TOKEN` | NapCat auth token | No |
| `ONEBOT_ALLOWED_USERS` | Comma-separated QQ numbers | No |
| `ONEBOT_ALLOW_ALL_USERS` | Allow everyone (true/false) | No |
| `ONEBOT_HOME_CHANNEL` | Default chat for cron/notifications | No |
| `ONEBOT_GROUP_IDS` | Comma-separated group IDs | No |
| `ONEBOT_HTTP_API_URL` | HTTP API endpoint (e.g. http://127.0.0.1:5700) | No |
| `ONEBOT_WS_MODE` | forward or reverse | No (default: forward) |
| `ONEBOT_ADMIN_QQ` | Admin QQ for commands | No |

## Security

- **SSRF protection**: DNS-resolved IP blocklist (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, IPv6 loopback/link-local/ULA/IPv4-mapped)
- **File injection**: Allowlist-only (media cache directory), 64KB size limit, symlink rejection
- **Token auth**: Constant-time comparison via `hmac.compare_digest`
- **Path traversal**: Cache directory validation with resolve-based checks
- **Log sanitization**: ANSI escape codes, control characters, Unicode bidi overrides stripped
- **Concurrent task limit**: Max 5 tasks per chat ID to prevent memory exhaustion

## Requirements

- Hermes Agent
- NapCat (or any OneBot v11 implementation) with WebSocket enabled
- `pip install websockets`
- `pip install httpx` (optional, for media downloads)
