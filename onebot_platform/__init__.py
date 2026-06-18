"""Modular namespace for the OneBot platform plugin.

The flat legacy modules remain compatible while implementation migrates here.
Avoid importing adapter at package import time to keep legacy imports acyclic.
"""

__all__ = ["OneBotAdapter"]


def __getattr__(name):
    if name == "OneBotAdapter":
        from .adapter import OneBotAdapter

        return OneBotAdapter
    raise AttributeError(name)
