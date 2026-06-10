from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime

globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith("__")})

from onebot_platform.state.settings_mixin import SettingsMixin
from onebot_platform.transport.connection_mixin import ConnectionMixin
from onebot_platform.inbound.message_mixin import MessageMixin
from onebot_platform.commands.mixin import CommandMixin
from onebot_platform.outbound.send_mixin import SendMixin
from onebot_platform.gateway_integration.approvals import ApprovalMixin, _APPROVAL_CHOICES, _UPDATE_CHOICES
def _onebot_platform_blocks(yaml_cfg: dict) -> List[dict]:
    if not isinstance(yaml_cfg, dict):
        return []
    blocks: List[dict] = []
    nested_platforms = yaml_cfg.get("platforms") if isinstance(yaml_cfg.get("platforms"), dict) else {}
    gateway = yaml_cfg.get("gateway") if isinstance(yaml_cfg.get("gateway"), dict) else {}
    gateway_platforms = gateway.get("platforms") if isinstance(gateway.get("platforms"), dict) else {}
    for block in (yaml_cfg.get("onebot"), nested_platforms.get("onebot"), gateway_platforms.get("onebot")):
        if isinstance(block, dict):
            blocks.append(block)
    return blocks
def _merge_onebot_platform_blocks(yaml_cfg: dict, platform_cfg: dict = None) -> dict:
    merged: dict = {}
    merged_extra: dict = {}
    for block in [*_onebot_platform_blocks(yaml_cfg), platform_cfg or {}]:
        if not isinstance(block, dict):
            continue
        extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
        merged.update({k: v for k, v in block.items() if k != "extra"})
        merged_extra.update(extra)
    if merged_extra:
        merged["extra"] = merged_extra
    return merged
class OneBotAdapter(SettingsMixin, ConnectionMixin, MessageMixin, CommandMixin, SendMixin, ApprovalMixin, BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    def __init__(self, config, **kwargs):
        platform = Platform("onebot")
        super().__init__(config=config, platform=platform)
        extra = _config_extra(config)
        self._init_connections(extra)
        self._init_shared_state(extra, kwargs)
        self._settings_path = kwargs.get("settings_path", DATA_DIR / "settings.json")
        self._plugin_settings = kwargs.get("settings")
        self._settings_loaded = self._plugin_settings is not None
        if self._settings_loaded:
            self._apply_persisted_settings()
        self._commands: Dict[str, _CmdDef] = {}
        self._register_commands()
    def _init_connections(self, extra: dict):
        accounts_cfg = extra.get("accounts", [])
        self._connections: Dict[str, _NapCatConnection] = {}
        self._multi_account: bool = False
        if isinstance(accounts_cfg, list) and accounts_cfg:
            self._multi_account = True
            for acct in accounts_cfg:
                if not isinstance(acct, dict):
                    continue
                name = str(acct.get("name", "default")).strip()
                if not name:
                    continue
                conn = _NapCatConnection(
                    name=name, ws_url=acct.get("ws_url", ""),
                    access_token=acct.get("access_token", ""),
                    ws_mode=acct.get("ws_mode", "forward"),
                    allowed_users=[str(u) for u in acct.get("allowed_users", [])],
                    group_ids=[str(g) for g in acct.get("group_ids", [])],
                    home_channel=str(acct.get("home_channel", "")),
                    allow_all=_truthy(acct.get("allow_all"), False),
                    admin_qq=str(acct.get("admin_qq", "")).strip(),
                    http_api_url=str(acct.get("http_api_url", "")).strip(),
                )
                self._connections[name] = conn
        if not self._connections:
            p = _parse_single_account_env(extra)
            conn = _NapCatConnection(
                name="default", ws_url=p["ws_url"], access_token=p["access_token"],
                ws_mode=p["ws_mode"], allowed_users=p["allowed_users"], group_ids=p["group_ids"],
                home_channel=p["home_channel"], allow_all=p["allow_all"], admin_qq=p["admin_qq"],
                http_api_url=p["http_api_url"],
            )
            self._connections["default"] = conn
        self._default_conn: _NapCatConnection = next(iter(self._connections.values()))
    def _init_shared_state(self, extra: dict, kwargs: dict):
        self._http_client = kwargs.get("http_client")
        self._show_qq_id = bool(extra.get("show_qq_id", False))
        self._settings_lock = asyncio.Lock()
        for attr in (
            "_chat_msg_seq", "_msg_receive_seq", "_last_msg_id",
            "_pending_approvals", "_pending_approval_admin", "_pending_approval_messages", "_approval_locks",
            "_pending_update_chats", "_last_progress_msg", "_in_edit_resend_count",
            "_active_input_status", "_active_tasks", "_reject_notified",
            "_recent_outbound_media",
        ):
            setattr(self, attr, {})
        self._unsupported_actions = set()
        self._delete_msg_supported = True
        self._bg_delete_tasks = set()
        self._last_seq_cleanup_time = 0
        self._media_cache = kwargs.get("media_cache") or _MediaCache(MEDIA_CACHE_DIR)
    @property
    def name(self) -> str:
        return "OneBot"
    @property
    def allowed_users(self) -> List[str]:
        return self._default_conn.allowed_users
    @property
    def is_connected(self) -> bool:
        return any(conn.is_connected for conn in self._connections.values())
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        msg_type, raw_id = _parse_chat_id(chat_id)
        if msg_type == "group":
            name = f"group_{raw_id}"
            chat_type = "group"
            try:
                action, params, name_key = "get_group_info", {"group_id": int(raw_id)}, "group_name"
            except (ValueError, TypeError):
                return {"id": chat_id, "name": name, "type": chat_type}
        else:
            name = f"user_{raw_id}"
            chat_type = "dm"
            try:
                action, params, name_key = "get_stranger_info", {"user_id": int(raw_id)}, "nickname"
            except (ValueError, TypeError):
                return {"id": chat_id, "name": name, "type": chat_type}
        conn = self._get_conn_for_chat(chat_id)
        if conn.is_connected or conn.http_api_url:
            try:
                resp = await self._send_action_conn(conn, action, params, timeout=5.0)
                rdata = resp.get("data") or {}
                if rdata.get(name_key):
                    name = rdata[name_key]
            except Exception:
                pass
        return {"id": chat_id, "name": name, "type": chat_type}
def _hermes_config_path() -> Path:
    return _runtime._hermes_config_path()


def _load_gateway_tool_progress_mode(platform_key: str = "onebot") -> str:
    return _runtime._config_utils_load_gateway_tool_progress_mode(
        platform_key, config_path_getter=_hermes_config_path
    )


def _save_gateway_tool_progress_mode(mode: str, platform_key: str = "onebot") -> None:
    _runtime._config_utils_save_gateway_tool_progress_mode(
        mode, platform_key, config_path_getter=_hermes_config_path
    )


def _config_extra(config) -> dict:
    return _runtime._config_extra(config)


def check_requirements() -> bool:
    return WEBSOCKETS_AVAILABLE


def validate_config(config) -> bool:
    return _runtime._config_utils_validate_config(config)


def is_configured(config) -> bool:
    return _runtime._config_utils_is_configured(config)


def _env_enablement() -> Optional[dict]:
    return _runtime._config_utils_env_enablement()


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict:
    """Bridge config.yaml onebot settings into env/extra for Hermes gateway."""
    return _runtime._config_utils_apply_yaml_config(
        yaml_cfg,
        platform_cfg,
        merge_platform_blocks=_merge_onebot_platform_blocks,
    )
async def _standalone_send(
    config: Any, chat_id: str, message: str, **kwargs,
) -> dict:
    return await _send_utils_standalone_send(
        config,
        chat_id,
        message,
        media_cache_factory=_MediaCache,
        media_cache_dir=MEDIA_CACHE_DIR,
        parse_chat_id=_parse_chat_id,
        extract_account_from_chat_id=_extract_account_from_chat_id,
        guess_media_segment_type=_guess_media_segment_type,
        websockets_available=WEBSOCKETS_AVAILABLE,
        websockets_connect=_websockets_connect,
        ws_connect_kwargs=_WS_CONNECT_KWARGS,
        **kwargs,
    )
def interactive_setup() -> dict:
    print("\n=== OneBot (NapCat) Setup ===")
    print("  Forward WS: Hermes connects to NapCat's WS server")
    print("  Reverse WS: NapCat connects to Hermes' WS server\n")
    mode = input("Mode [forward/reverse] (default: forward): ").strip().lower()
    mode = mode or "forward"
    prompt, default = ("NapCat WebSocket URL [ws://127.0.0.1:3001]: ", "ws://127.0.0.1:3001") if mode == "forward" else ("Listen address [ws://0.0.0.0:8082]: ", "ws://0.0.0.0:8082")
    ws_url = input(prompt).strip() or default
    token = input("Access token (leave empty if none): ").strip()
    allowed = input("Allowed QQ numbers (comma-separated, 留空则拒绝所有用户): ").strip()
    groups = input("Group IDs to listen (comma-separated, 留空则拒绝所有群): ").strip()
    env_vars = {
        "ONEBOT_WS_URL": ws_url,
        "ONEBOT_WS_MODE": mode,
    }
    if token:
        env_vars["ONEBOT_ACCESS_TOKEN"] = token
    if allowed:
        env_vars["ONEBOT_ALLOWED_USERS"] = allowed
    if groups:
        env_vars["ONEBOT_GROUP_IDS"] = groups
    return env_vars
