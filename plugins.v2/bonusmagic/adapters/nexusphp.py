"""NexusPHP 传统架构适配器。页面差异（独立 form / 整页 radio）在 parser 内消化。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from .base import SiteAdapter
from ..parser import (
    classify_result as nphp_classify,
    parse_exchange_items,
    parse_user_stats as nphp_stats,
    parse_wait_seconds,
)


class NexusPHPAdapter(SiteAdapter):
    name = "nexusphp"
    label = "NexusPHP"

    SIGNATURES = (
        "mybonus.php",
        "usercp.php",
        "torrents.php",
        "logout.php",
        "nexusphp",
        "power by nexusphp",
        "powered by nexusphp",
        "class=\"nexusphp\"",
    )

    def detect_score(self, site: dict, html: str) -> float:
        overrides = (site or {}).get("overrides") or {}
        if (overrides.get("architecture") or "").lower() == self.name:
            return 1.0
        blob = ((html or "") + " " + str((site or {}).get("url") or "")).lower()
        hits = sum(1 for s in self.SIGNATURES if s in blob)
        if hits >= 3:
            return 0.95
        if hits >= 2:
            return 0.8
        if hits >= 1:
            return 0.55
        return 0.0

    def parse_user_stats(self, html: str) -> Dict[str, Any]:
        return nphp_stats(html)

    def catalog_path(self, site: dict) -> str:
        overrides = (site or {}).get("overrides") or {}
        return (overrides.get("catalog_path") or "mybonus.php").lstrip("/")

    def parse_catalog(self, html: str, base_url: str, site: dict) -> List[Dict[str, Any]]:
        return parse_exchange_items(html, base_url)

    def build_exchange(self, site: dict, item: dict) -> Tuple[str, str, dict]:
        overrides = (site or {}).get("overrides") or {}
        default_path = overrides.get("exchange_path") or "mybonus.php?action=exchange"
        url = item.get("action") or urljoin(site["url"].rstrip("/") + "/", default_path)
        method = (item.get("method") or "post").lower()
        data = dict(item.get("fields") or {"option": item.get("option"), "submit": "交换"})
        extra = overrides.get("extra_fields") or {}
        if isinstance(extra, dict):
            data.update({str(k): str(v) for k, v in extra.items()})
        return method, url, data

    def classify_result(self, html: str, status_code: int = 200) -> Dict[str, Any]:
        return nphp_classify(html, status_code)

    def parse_wait(self, html: str, headers: Optional[dict] = None) -> Optional[float]:
        return parse_wait_seconds(html, headers)

    def referer_path(self, site: dict) -> str:
        return self.catalog_path(site)
