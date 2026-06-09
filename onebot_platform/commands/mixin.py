from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})

class CommandMixin:
    def _register_commands(self):
        _lm = self._cmd_list_mutate
        cmds = [
            # Keep OneBot management commands under the /onebot namespace so
            # core Hermes slash commands (/help, /config, /status, …) continue
            # to reach the gateway command dispatcher. Older broad aliases made
            # the adapter swallow core commands before Hermes could see them.
            ("/onebot", self._cmd_onebot, False),
            ("/adduser", functools.partial(_lm, entity_type="user", action="add"), True),
            ("/removeuser", functools.partial(_lm, entity_type="user", action="remove"), True),
            ("/listusers", functools.partial(_lm, entity_type="user", action="list"), True),
            ("/addgroup", functools.partial(_lm, entity_type="group", action="add"), True),
            ("/rmgroup", functools.partial(_lm, entity_type="group", action="remove"), True),
            ("/listgroups", functools.partial(_lm, entity_type="group", action="list"), True),
            ("/settool", self._cmd_settool, True),
            ("/setmd", self._cmd_setmd, True),
            ("/setallowall", self._cmd_setallowall, True),
        ]
        for name, handler, admin in cmds:
            self._commands[name] = _CmdDef(name, handler, admin_only=admin)
    async def _try_handle_command(self, data: dict, conn, text_for_cmd: str,
                                   msg_type: str, user_id: str, admin_qq) -> bool:
        matches = [(name, defn) for name, defn in self._commands.items()
                   if text_for_cmd == name or text_for_cmd.startswith(name + " ")]
        if not matches:
            return False
        cmd_name, cmd_def = max(matches, key=lambda m: len(m[0]))
        cmd_args = text_for_cmd[len(cmd_name):].strip()
        if cmd_def.admin_only and user_id != admin_qq:
            await self._send_reply_async_conn(conn, data, "✗ 只有管理员可以执行此命令")
            return True
        if cmd_def.group_only and msg_type != "group":
            await self._send_reply_async_conn(conn, data, "✗ 此命令只能在群聊中使用")
            return True
        await cmd_def.handler(conn, data, cmd_args, user_id, admin_qq)
        return True
    async def _cmd_onebot(self, conn, data, args, user_id, admin_qq):
        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "help"
        rest = parts[1] if len(parts) > 1 else ""
        routes = {
            "": (self._cmd_help, False),
            "help": (self._cmd_help, False),
            "h": (self._cmd_help, False),
            "?": (self._cmd_help, False),
            "config": (self._cmd_config, True),
        }
        route = routes.get(sub)
        if not route:
            await self._send_reply_async_conn(conn, data, "✗ 未知OneBot子命令。用 /onebot help 查看")
            return
        handler, admin_only = route
        if admin_only and user_id != admin_qq:
            await self._send_reply_async_conn(conn, data, "✗ 只有管理员可以执行此命令")
            return
        await handler(conn, data, rest, user_id, admin_qq)
    async def _cmd_list_mutate(self, conn, data, args, user_id, admin_qq,
                                entity_type: str, action: str):
        labels = {"user": ("白名单", "用户", "QQ号", "adduser", "removeuser"),
                  "group": ("群白名单", "群", "群号", "addgroup", "rmgroup")}
        list_label, entity_label, id_label, add_cmd, rm_cmd = labels[entity_type]
        _reply = lambda msg: self._send_reply_async_conn(conn, data, msg)
        get_list = conn.list_allowed_users if entity_type == "user" else lambda: list(conn.group_ids)
        persist = self._persist_allowed_users if entity_type == "user" else self._persist_group_ids
        val = args.strip()
        handlers = {
            "list": self._handle_list_command,
            "add": self._handle_add_command,
            "remove": self._handle_remove_command,
        }
        await handlers[action](
            conn, val, _reply, persist, get_list,
            list_label, entity_label, id_label, add_cmd, rm_cmd, admin_qq,
        )
    async def _handle_list_command(self, conn, val, reply, persist, get_list,
                                   list_label, entity_label, id_label, add_cmd, rm_cmd, admin_qq):
        items = get_list()
        msg = f"当前{list_label}：\n" + "\n".join(f"• {u}" for u in items) if items else f"{list_label}为空"
        await reply(msg)
    async def _handle_add_command(self, conn, val, reply, persist, get_list,
                                  list_label, entity_label, id_label, add_cmd, rm_cmd, admin_qq):
        if not val:
            await reply(f"✗ 用法: /{add_cmd} <{id_label}>")
            return
        items = get_list()
        if id_label == "群号" and not val.isdigit():
            await reply(f"✗ {id_label}格式错误")
            return
        if val in items:
            await reply(f"✗ {entity_label} {val} 已在{list_label}中")
            return
        if id_label == "QQ号" and not conn.add_allowed_user(val):
            await reply(f"✗ 添加失败，{entity_label}可能已存在或格式错误")
            return
        if id_label == "群号":
            conn.group_ids.append(val)
        await persist(conn)
        suffix = f" 到{list_label}" if id_label == "QQ号" else ""
        await reply(f"✓ 已添加{entity_label} {val}{suffix}")
    async def _handle_remove_command(self, conn, val, reply, persist, get_list,
                                     list_label, entity_label, id_label, add_cmd, rm_cmd, admin_qq):
        if not val:
            await reply(f"✗ 用法: /{rm_cmd} <{id_label}>")
            return
        if val == admin_qq:
            await reply("✗ 不能移除管理员账户")
            return
        items = get_list()
        if val not in items:
            await reply(f"✗ {entity_label} {val} 不在{list_label}中" if id_label == "群号" else f"✗ 移除失败，{entity_label}可能不存在")
            return
        if id_label == "QQ号":
            conn.remove_allowed_user(val)
        else:
            conn.group_ids.remove(val)
        await persist(conn)
        await reply(f"✓ 已从{list_label}移除{entity_label} {val}" if id_label == "QQ号" else f"✓ 已移除{entity_label} {val}")
    async def _cmd_help(self, conn, data, args, user_id, admin_qq):
        topic = args.strip().lower()
        sections = {
            "basic": [
                "基础",
                "/onebot  查看OneBot插件帮助",
                "/onebot help  查看完整指令",
                "/onebot help admin  查看管理指令",
                "/onebot config  查看OneBot当前聊天配置",
            ],
            "access": [
                "权限与白名单",
                "/adduser <QQ号>  加入用户白名单",
                "/removeuser <QQ号>  移出用户白名单",
                "/listusers  查看用户白名单",
                "/addgroup <群号>  加入群白名单",
                "/rmgroup <群号>  移出群白名单",
                "/listgroups  查看群白名单",
                "/setallowall on|off  是否允许所有人使用",
            ],
            "display": [
                "显示与体验",
                "/settool on|off  工具调用提示",
                "/setmd on|off  Markdown清理",
            ],
        }
        aliases = {"admin": "access", "perm": "access", "config": "basic", "display": "display"}
        keys = [aliases[topic]] if topic in aliases else ["basic", "access", "display"]
        lines = ["OneBot指令中心", "用法：/onebot help admin", ""]
        for key in keys:
            section = sections[key]
            lines.append(f"【{section[0]}】")
            lines.extend(f"  {item}" for item in section[1:])
            lines.append("")
        await self._send_reply_async_conn(conn, data, "\n".join(lines).rstrip())
    async def _persist_group_ids(self, conn):
        await self._persist_account_setting(conn, "group_ids_by_account", conn.group_ids)
    async def _onoff_arg(self, conn, data, args, cmd_name):
        val = args.strip().lower()
        if val not in ("on", "off"):
            await self._send_reply_async_conn(conn, data, f"✗ 用法: /{cmd_name} on|off")
            return None
        return val
    async def _cmd_toggle_setting(self, conn, data, args, setting_key, label, cmd_name, is_global=False):
        val = await self._onoff_arg(conn, data, args, cmd_name)
        if val is None:
            return
        cs = self._get_global_settings() if is_global else self._get_chat_settings(_make_chat_id(data, conn.name if self._multi_account else ""))
        cs[setting_key] = (val == "on")
        await self._save_settings()
        await self._send_reply_async_conn(conn, data, f"✓ {label}: {'开启' if val == 'on' else '关闭'}")
    async def _cmd_settool(self, conn, data, args, user_id, admin_qq):
        val = await self._onoff_arg(conn, data, args, "settool")
        if val is None:
            return
        mode = "all" if val == "on" else "off"
        try:
            _save_gateway_tool_progress_mode(mode, "onebot")
        except Exception as e:
            logger.warning("Failed to save gateway tool_progress mode: %s", e)
            await self._send_reply_async_conn(conn, data, f"✗ 工具调用提示保存失败: {e}")
            return
        await self._send_reply_async_conn(conn, data, f"✓ 工具调用提示: {'开启' if val == 'on' else '关闭'}（gateway层）")
    async def _cmd_setmd(self, conn, data, args, user_id, admin_qq):
        await self._cmd_toggle_setting(conn, data, args, "strip_markdown", "Markdown清理", "setmd")
    async def _cmd_setallowall(self, conn, data, args, user_id, admin_qq):
        val = await self._onoff_arg(conn, data, args, "setallowall")
        if val is None:
            return
        conn.allow_all = (val == "on")
        gs = self._get_global_settings()
        if "allow_all_by_account" not in gs:
            gs["allow_all_by_account"] = {}
        gs["allow_all_by_account"][conn.name] = conn.allow_all
        await self._save_settings()
        await self._send_reply_async_conn(conn, data,
            f"✓ 允许所有人使用: {'开启' if val == 'on' else '关闭'}")
    async def _cmd_config(self, conn, data, args, user_id, admin_qq):
        account_name = conn.name if self._multi_account else ""
        _cfg_chat_id = _make_chat_id(data, account_name)
        cs = self._plugin_settings.get_chat(_cfg_chat_id)
        gs = self._plugin_settings.get_chat("_global")
        def _state(v, default="默认"):
            if v is None:
                return default
            return "开启" if v else "关闭"
        allow_all_accounts = gs.get("allow_all_by_account", {})
        conn_allow_all = allow_all_accounts.get(conn.name, conn.allow_all)
        lines = [
            "OneBot当前配置",
            f"聊天：{_cfg_chat_id}",
            f"账号：{conn.name}",
            "",
            "【开关】",
            f"  工具调用提示：{_load_gateway_tool_progress_mode('onebot')}（gateway层）",
            f"  Markdown清理：{_state(cs.get('strip_markdown'))}",
            f"  允许所有人：{'开启' if conn_allow_all else '关闭'}",
            f"  显示QQ号：{'开启' if self._show_qq_id else '关闭'}",
            "",
            "【连接】",
            f"  WebSocket：{conn.ws_mode}",
            f"  HTTP API：{'已配置' if conn.http_api_url else '未配置'}",
            f"  主页频道：{conn.home_channel or '未设置'}",
            "",
            "【权限】",
            f"  群白名单：{', '.join(conn.group_ids) if conn.group_ids else '空，拒绝所有群'}",
            f"  用户白名单：{', '.join(conn.allowed_users) if conn.allowed_users else '空，拒绝所有用户'}",
            "",
            "提示：输入 /help 查看可用指令",
        ]
        await self._send_reply_async_conn(conn, data, "\n".join(lines))
