"""OneBot (NapCat) platform plugin for Hermes Agent."""

import logging
import sys
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

logger = logging.getLogger(__name__)


def _detect_multi_account() -> Optional[list]:
    try:
        import yaml
        try:
            from hermes_constants import get_hermes_home
            config_path = get_hermes_home() / "config.yaml"
        except Exception:
            config_path = Path.home() / ".hermes" / "config.yaml"
        if not config_path.exists():
            return None
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        platforms = (cfg.get("gateway", {}).get("platforms") or cfg.get("platforms") or {})
        onebot = platforms.get("onebot", {}) if isinstance(platforms, dict) else {}
        extra = onebot.get("extra", {}) if isinstance(onebot, dict) else {}
        accounts = extra.get("accounts", []) if isinstance(extra, dict) else []
        valid = [a for a in accounts if isinstance(a, dict) and str(a.get("name", "")).strip()]
        return valid or None
    except Exception as e:
        logger.debug("OneBot: could not detect multi-account config: %s", e)
        return None


def register(ctx):
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
            "You can send voice, video, files, forward messages, and more. "
            "Keep responses conversational and concise. "
            "In groups, messages are prefixed with [sender_name]. "
            "Use @mentions to address specific users."
        ),
    )


__all__ = ["register"]
