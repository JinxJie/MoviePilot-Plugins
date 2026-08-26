"""站点适配层。核心调度只认 SiteAdapter，不关心具体架构。"""

from .base import SiteAdapter
from .registry import detect_adapter, get_adapter, list_adapters

__all__ = ["SiteAdapter", "detect_adapter", "get_adapter", "list_adapters"]
