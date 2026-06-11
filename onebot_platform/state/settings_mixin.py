from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})

class SettingsMixin:
    async def _ensure_settings_loaded(self):
        if self._settings_loaded:
            return
        async with self._settings_lock:
            if self._settings_loaded:
                return
            self._plugin_settings = _PluginSettings(self._settings_path)
            self._plugin_settings.load()
            self._settings_loaded = True
            self._apply_persisted_settings()
    async def _save_settings(self):
        await self._plugin_settings.save()
    def _restore_account_list(self, conn: _NapCatConnection, val, attr: str) -> None:
        if attr == "allowed_users" and isinstance(val, list):
            conn.allowed_users = list(set(conn.allowed_users + [str(u) for u in val]))
        elif attr == "allow_all":
            conn.allow_all = bool(val)
    def _apply_persisted_settings(self):
        try:
            if not self._plugin_settings.data:
                return
            gs = self._plugin_settings.data.get("_global", {})
            group_ids_by_account = gs.get("group_ids_by_account")
            if isinstance(group_ids_by_account, dict) and group_ids_by_account:
                for name, gids in group_ids_by_account.items():
                    conn = self._connections.get(name)
                    if conn and isinstance(gids, list):
                        conn.group_ids = [str(g) for g in gids]
            elif gs.get("group_ids") is not None:
                group_list = [str(g) for g in gs["group_ids"]]
                for conn in self._connections.values():
                    conn.group_ids = list(group_list)
            for key, attr in [("allowed_users_by_account", "allowed_users"),
                              ("allow_all_by_account", "allow_all")]:
                acct_dict = gs.get(key, {})
                if not isinstance(acct_dict, dict):
                    continue
                for name, val in acct_dict.items():
                    conn = self._connections.get(name)
                    if conn is not None:
                        self._restore_account_list(conn, val, attr)
        except Exception as e:
            logger.debug("Failed to apply persisted settings: %s", e)
    async def _persist_account_setting(self, conn: _NapCatConnection, key: str, value_list):
        gs = self._get_global_settings()
        gs.setdefault(key, {})[conn.name] = list(value_list)
        await self._save_settings()
    async def _persist_allowed_users(self, conn: _NapCatConnection):
        await self._persist_account_setting(conn, "allowed_users_by_account", conn.allowed_users)
    def _get_chat_settings(self, chat_id: str) -> dict:
        return self._plugin_settings.ensure_chat(chat_id)
    def _get_global_settings(self) -> dict:
        return self._plugin_settings.get_global()
    def _get_conn_for_chat(self, chat_id: str) -> _NapCatConnection:
        if not self._multi_account:
            return self._default_conn
        account = _extract_account_from_chat_id(chat_id)
        if account:
            if account in self._connections:
                return self._connections[account]
            raise ValueError(f"unknown OneBot account: {account}")
        return self._default_conn
