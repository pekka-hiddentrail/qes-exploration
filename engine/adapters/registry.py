"""Adapter registry - a plain name -> dotted-module-path map, resolved lazily
via importlib at CLI runtime. Nothing in engine/ eagerly imports adapter
code; this is the only place that ever crosses from engine/ into
engine/adapters/, and it does so at run time, not at import time."""

import importlib

_ADAPTERS = {
    "token_purchase": "engine.adapters.token_purchase.adapter",
    "complex_sut": "engine.adapters.complex_sut.adapter",
}


def available_adapters() -> list[str]:
    return sorted(_ADAPTERS)


def load_adapter(name: str):
    module_path = _ADAPTERS.get(name)
    if module_path is None:
        available = ", ".join(available_adapters()) or "(none registered)"
        raise SystemExit(f"Unknown adapter '{name}'. Available: {available}")
    module = importlib.import_module(module_path)
    return module.ADAPTER
