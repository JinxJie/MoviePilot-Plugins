"""站点适配器接口。核心调度只调用这些方法，不关心页面路径和 HTML 结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class SiteAdapter(ABC):
    """一种 PT 架构的兑换适配器。

    同一架构下不同站点的页面差异，由适配器内部消化；
    站点特殊规则可通过 site["overrides"] 传入。
    """

    name: str = "base"
    label: str = "未知架构"

    def detect_score(self, site: dict, html: str) -> float:
        """根据首页/任意 HTML 给出 0~1 匹配分。核心用最高分选适配器。"""
        return 0.0

    @abstractmethod
    def parse_user_stats(self, html: str) -> Dict[str, Any]:
        """解析魔力、上传、下载、分享率、登录态。"""

    @abstractmethod
    def catalog_path(self, site: dict) -> str:
        """魔力商店相对路径。"""

    @abstractmethod
    def parse_catalog(self, html: str, base_url: str, site: dict) -> List[Dict[str, Any]]:
        """解析可兑换项。解析不到价格必须返回空列表，核心会禁止兑换。"""

    @abstractmethod
    def build_exchange(self, site: dict, item: dict) -> Tuple[str, str, dict]:
        """返回 (method, url, form_data)。"""

    @abstractmethod
    def classify_result(self, html: str, status_code: int = 200) -> Dict[str, Any]:
        """判断兑换结果：ok / login / no_bonus / rate_limit / already / http / unknown。"""

    def parse_wait(self, html: str, headers: Optional[dict] = None) -> Optional[float]:
        return None

    def referer_path(self, site: dict) -> str:
        return self.catalog_path(site)
