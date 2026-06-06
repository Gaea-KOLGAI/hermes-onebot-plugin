"""OneBot (NapCat) platform plugin for Hermes Agent.

Supports both single-account and multi-account modes:

Single-account (backward compatible)::

    platforms:
      onebot:
        enabled: true
        extra:
          ws_url: ws://127.0.0.1:3001
          allowed_users: ["YOUR_QQ_ID"]
          home_channel: "private_YOUR_QQ_ID"

Multi-account (each NapCat instance = isolated session)::

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

In multi-account mode:
- Each account gets its own WebSocket connection (one NapCat = one session)
- Chat IDs are prefixed with account name (e.g., "main:private_YOUR_QQ_ID")
- Each account has its own allowed_users file (allowed_users_{name}.json)
- Admin commands only affect the account they are sent to
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _detect_multi_account() -> Optional[list]:
    """Read config.yaml and return the accounts list if multi-account mode is configured.

    Returns the accounts list (list[dict]) if found, or None for single-account mode.
    """
    try:
        import yaml

        
        try:
            from hermes_constants import get_hermes_home
            config_path = get_hermes_home() / "config.yaml"
        except Exception:
            config_path = Path.home() / ".hermes" / "config.yaml"
        if not config_path.exists():
            return None

        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        # Check gateway.platforms.onebot.extra.accounts
        platforms = cfg.get("gateway", {}).get("platforms", {})
        if not platforms:
            # Also check top-level platforms (some configs use this format)
            platforms = cfg.get("platforms", {})

        onebot = platforms.get("onebot", {})
        if not isinstance(onebot, dict):
            return None

        extra = onebot.get("extra", {})
        if not isinstance(extra, dict):
            return None

        accounts = extra.get("accounts", [])
        if isinstance(accounts, list) and accounts:
            return accounts

    except Exception as e:
        logger.debug("OneBot: could not detect multi-account config: %s", e)

    return None


def register(ctx):
    """Plugin entry point — registers OneBot platform adapter.

    Detects multi-account mode from config.yaml and adjusts registration
    accordingly. In multi-account mode, ONEBOT_WS_URL is not required
    (each account specifies its own ws_url in the accounts list).
    """
    from .adapter import (
        OneBotAdapter,
        check_requirements,
        validate_config,
        is_configured,
        interactive_setup,
        _env_enablement,
        _standalone_send,
        _apply_yaml_config,
    )

    # Detect multi-account mode
    accounts = _detect_multi_account()
    multi_account = accounts is not None

    if multi_account:
        account_names = [a.get("name", "?") for a in accounts]
        logger.info(
            "OneBot: multi-account mode detected (%d accounts: %s)",
            len(accounts),
            ", ".join(account_names),
        )

    ctx.register_platform(
        name="onebot",
        label="OneBot (NapCat)",
        adapter_factory=lambda cfg: OneBotAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_configured,
        # In multi-account mode, ws_url comes from accounts list, not env var
        required_env=[] if multi_account else ["ONEBOT_WS_URL"],
        install_hint="pip install websockets (usually already available)",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="ONEBOT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ONEBOT_ALLOWED_USERS",
        allow_all_env="ONEBOT_ALLOW_ALL_USERS",
        emoji="🐧",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via QQ (OneBot/NapCat). "
            "QQ supports basic formatting. Messages can be up to 4500 characters. "
            "Images can be sent via OneBot image segments. "
            "You can send voice, video, files, poke, forward messages, and more. "
            "Keep responses conversational and concise. "
            "In groups, messages are prefixed with [sender_name]. "
            "Use @mentions to address specific users."
        ),
    )


__all__ = ["register"]
