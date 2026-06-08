"""Compatibility facade for onebot_platform.outbound.send_mixin."""

from importlib import import_module as _import_module

_module = _import_module("onebot_platform.outbound.send_mixin")
for _name, _value in vars(_module).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

__all__ = [_name for _name in globals() if not _name.startswith("__")]
