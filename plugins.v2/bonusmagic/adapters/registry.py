"""适配器注册与自动识别。核心只调用 detect_adapter / get_adapter。"""

from __future__ import annotations

from typing import Dict, List, Optional

from .nexusphp import NexusPHPAdapter

# 后续新架构在此登记即可，不必改核心调度
_ADAPTERS = {
    "nexusphp": NexusPHPAdapter(),
}


def list_adapters() -> List[str]:
    return list(_ADAPTERS.keys())


def get_adapter(name: str):
    key = (name or "").strip().lower()
    if key in _ADAPTERS:
        return _ADAPTERS[key]
    return _ADAPTERS["nexusphp"]


def detect_adapter(site: dict, html: str = "", forced: str = ""):
    """选择适配器：站点单独指定 > 全局强制 > 自动识别。

    识别不到时首期回退 NexusPHP（当前唯一实现），并带上低置信标记。
    """
    overrides = (site or {}).get("overrides") or {}
    pinned = (overrides.get("architecture") or forced or "").strip().lower()
    if pinned and pinned in _ADAPTERS:
        adapter = _ADAPTERS[pinned]
        return adapter, 1.0, "pinned"

    best = None
    best_score = -1.0
    for adapter in _ADAPTERS.values():
        score = float(adapter.detect_score(site, html) or 0)
        if score > best_score:
            best_score = score
            best = adapter
    if best is None or best_score <= 0:
        return _ADAPTERS["nexusphp"], 0.0, "fallback"
    return best, best_score, "auto"
