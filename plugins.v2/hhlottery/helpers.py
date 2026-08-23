"""工具函数：CookieCloud/站点管理兼容层 + 域名/数字处理"""

from typing import Optional, Tuple


# ── CookieCloud 多路径兼容 ──────────────────────────────────────────

def _load_cookiecloud_helper():
    """CookieCloud 的模块路径在 MoviePilot 各版本间搬过家，挨个试。"""
    for module_path in (
        "app.helper.cookiecloud",
        "app.modules.cookiecloud.cookiecloud",
        "app.adapters.external.cookiecloud",
    ):
        try:
            module = __import__(module_path, fromlist=["CookieCloudHelper"])
            helper = getattr(module, "CookieCloudHelper", None)
            if helper:
                return helper
        except Exception:
            continue
    return None


def _load_site_oper():
    """MoviePilot 站点管理里的 Cookie。"""
    for module_path in ("app.db.site_oper", "app.db.siteoper"):
        try:
            module = __import__(module_path, fromlist=["SiteOper"])
            oper = getattr(module, "SiteOper", None)
            if oper:
                return oper
        except Exception:
            continue
    return None


# ── 域名处理 ────────────────────────────────────────────────────────

def _short_domain(host: str) -> str:
    """把用户填的 host 收成 CookieCloud / 站点库里用的域名 key（末两级）。"""
    text = (host or "").strip().lower()
    text = text.split("://")[-1].split("/")[0].split("?")[0].split("@")[-1].split(":")[0]
    parts = [p for p in text.split(".") if p]
    if len(parts) < 2:
        return text
    return ".".join(parts[-2:])


# ── 数字解析 ────────────────────────────────────────────────────────

def _number(value, default, cast=float):
    """把配置页交上来的值收成数字。

    空不等于 0。界面输入框清空时前端交 ""，`int(x or 0)` 会把空变成 0。
    对「每次抽多少次」来说 0 是一抽到底，所以空/None/认不出来的一律退回默认值。
    """
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default
