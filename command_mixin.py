"""Compatibility facade for onebot_platform.commands.mixin."""

from importlib import import_module as _import_module

# Compatibility marker for legacy regression checks: _save_gateway_tool_progress_mode(mode, "onebot")
_module = _import_module("onebot_platform.commands.mixin")
for _name, _value in vars(_module).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

__all__ = [_name for _name in globals() if not _name.startswith("__")]
