"""Provider registry."""
from __future__ import annotations

_PROVIDERS = {}


def get_provider(name: str = ""):
    provider_cls = _PROVIDERS.get((name or "").lower())
    if provider_cls is None:
        raise ValueError(f"Unsupported data provider: {name}")
    return provider_cls()
