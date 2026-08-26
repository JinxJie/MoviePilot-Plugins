"""NexusPHP mybonus.php 动态解析。"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

_NUM = r"[\d,]+(?:\.\d+)?"
_SIZE = rf"({_NUM})\s*([KMGTPE]i?B)"

UPLOAD_HINTS = ("上传量", "上傳量", "上传", "上傳", "uploaded", "upload")
DOWNLOAD_HINTS = ("下载量", "下載量", "下载", "下載", "downloaded", "download")
BONUS_HINTS = ("魔力值", "魔力", "積分", "积分", "bonus", "karma", "karma points")

WAIT_PATTERNS = [
    re.compile(r"请等待\s*(\d+(?:\.\d+)?)\s*秒", re.I),
    re.compile(r"請等待\s*(\d+(?:\.\d+)?)\s*秒", re.I),
    re.compile(r"wait\s*(\d+(?:\.\d+)?)\s*seconds?", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*秒后再[试試]", re.I),
    re.compile(r"间隔\s*(\d+(?:\.\d+)?)\s*秒", re.I),
    re.compile(r"冷却\s*(\d+(?:\.\d+)?)\s*秒", re.I),
    re.compile(r"retry[- ]after[:\s]*(\d+(?:\.\d+)?)", re.I),
]

LOGIN_HINTS = (
    "login.php",
    "type=\"password\"",
    "type='password'",
    "未登录",
    "尚未登錄",
    "請登錄",
    "请登录",
    "cookie已失效",
    "cookie 已失效",
)

SUCCESS_HINTS = ("兑换成功", "兌換成功", "成功兑换", "成功兌換", "交换成功", "交換成功", "祝贺你", "祝賀你")
FAIL_BONUS_HINTS = ("魔力值不足", "魔力不足", "积分不足", "積分不足", "bonus not enough")
FAIL_LIMIT_HINTS = ("过于频繁", "過於頻繁", "频率限制", "操作过快", "稍后再试", "稍後再試", "rate limit")
ALREADY_HINTS = ("今日已兑换", "已兑换过", "已经兑换", "已兌換過")
# NexusPHP 官方反作弊 die() 文案（mybonus.php: $_POST 带 userid/points/bonus/art 时）
REJECT_HINTS = ("想作弊", "没门", "cheat")
# 非流量类兑换行（借鉴油猴脚本词库）：含这些词的行不参与上传/下载解析，防止慈善/赠送行误判
EXCLUDE_ITEM_HINTS = ("捐赠", "捐贈", "赠送", "贈送", "慈善", "gift", "donate", "charity", "invite", "邀请", "邀請")

SIZE_UNIT = {
    "B": 1,
    "KB": 1024,
    "KIB": 1024,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
    "PB": 1024 ** 5,
    "PIB": 1024 ** 5,
    "EB": 1024 ** 6,
    "EIB": 1024 ** 6,
}


def strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|tr|div|li|h\d|td)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def parse_number(text: Any, default: Optional[float] = None) -> Optional[float]:
    if text is None:
        return default
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text).strip().replace(",", "").replace(" ", "")
    if not raw:
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def parse_size_bytes(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(_SIZE, text, re.I)
    if not m:
        return None
    num = parse_number(m.group(1))
    unit = m.group(2).upper().replace("Ｉ", "I")
    if num is None:
        return None
    factor = SIZE_UNIT.get(unit)
    if not factor:
        return None
    return int(num * factor)


def format_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def is_logged_in(html: str) -> bool:
    text = html or ""
    low = text.lower()
    if "type=\"password\"" in low or "type='password'" in low:
        if "logout" not in low and "mybonus" not in low and "usercp" not in low:
            return False
    if any(h in text for h in ("logout.php", "mybonus.php", "usercp.php", "userdetails.php")):
        return True
    if any(h in text for h in LOGIN_HINTS):
        if "logout" not in low:
            return False
    return "logout" in low


def parse_wait_seconds(html: str, headers: Optional[dict] = None) -> Optional[float]:
    if headers:
        retry = headers.get("Retry-After") or headers.get("retry-after")
        if retry:
            n = parse_number(retry)
            if n is not None and n >= 0:
                return float(n)
    text = strip_tags(html or "")
    for pat in WAIT_PATTERNS:
        m = pat.search(html or "") or pat.search(text)
        if m:
            n = parse_number(m.group(1))
            if n is not None and n >= 0:
                return float(n)
    return None


def parse_user_stats(html: str) -> Dict[str, Any]:
    text = strip_tags(html or "")
    compact = html or ""
    stats = {
        "bonus": None,
        "upload": None,
        "download": None,
        "ratio": None,
        "logged_in": is_logged_in(html or ""),
    }

    bonus_pats = [
        re.compile(r"(?:魔力值|魔力|積分|积分|bonus|karma)\s*(?:\[[^\]]+\])?\s*[:：]?\s*(" + _NUM + r")", re.I),
        re.compile(r"mybonus[^>]*>\s*(" + _NUM + r")", re.I),
    ]
    for pat in bonus_pats:
        m = pat.search(text) or pat.search(compact)
        if m:
            stats["bonus"] = parse_number(m.group(1))
            break

    up = re.search(r"(?:[^总]上[传傳]量?)\s*[:：]?\s*" + _SIZE, text, re.I)
    if up:
        stats["upload"] = parse_size_bytes(up.group(0))
    down = re.search(r"(?:[^总子]下[载載]量?)\s*[:：]?\s*" + _SIZE, text, re.I)
    if down:
        stats["download"] = parse_size_bytes(down.group(0))
    ratio = re.search(r"分享率\s*[:：]?\s*(" + _NUM + r"|无限|無限|inf)", text, re.I)
    if ratio:
        raw = ratio.group(1)
        if re.search(r"无限|無限|inf", raw, re.I):
            stats["ratio"] = 9999.0
        else:
            stats["ratio"] = parse_number(raw)

    if stats["ratio"] is None and stats["upload"] and stats["download"]:
        if stats["download"] > 0:
            stats["ratio"] = round(stats["upload"] / stats["download"], 3)
        else:
            stats["ratio"] = 9999.0
    return stats


def _classify_kind(text: str) -> Optional[str]:
    low = text.lower()
    has_up = any(h.lower() in low or h in text for h in UPLOAD_HINTS)
    has_down = any(h.lower() in low or h in text for h in DOWNLOAD_HINTS)
    if has_up and not has_down:
        return "upload"
    if has_down and not has_up:
        return "download"
    if has_up and has_down:
        up_pos = min((text.find(h) for h in UPLOAD_HINTS if h in text), default=10**9)
        down_pos = min((text.find(h) for h in DOWNLOAD_HINTS if h in text), default=10**9)
        return "upload" if up_pos <= down_pos else "download"
    return None


def _extract_cost(text: str) -> Optional[float]:
    # 先剔除体积数字（如 100.0 GB），避免把流量大小当价格（PTzone 实测踩坑）
    cleaned = re.sub(r"(" + _NUM + r")\s*[KMGTPE]i?B", " ", text or "", flags=re.I)
    pats = [
        re.compile(r"(?:需要|消耗|花费|花費|cost)\s*(" + _NUM + r")\s*(?:魔力|積分|积分|bonus|karma)?", re.I),
        re.compile(r"(" + _NUM + r")\s*(?:魔力值|魔力|積分|积分|bonus)", re.I),
        # 表格独立价格列：<td align="center">3,000</td>（LongPT 等）
        re.compile(r"(?:价格|price)\s*[:：]?\s*(" + _NUM + r")", re.I),
    ]
    for pat in pats:
        m = pat.search(cleaned)
        if m:
            n = parse_number(m.group(1))
            if n is not None and n > 0:
                return n
    # 兜底：独立价格列是裸数字，取第一个像价格的（>=100，避开序号）
    for m in re.finditer(r"(" + _NUM + r")", cleaned):
        n = parse_number(m.group(1))
        if n is not None and n >= 100:
            return n
    return None


def _extract_hidden_fields(form_html: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for m in re.finditer(
        r"<input\b([^>]*)>",
        form_html or "",
        re.I,
    ):
        attrs = m.group(1)
        itype = _attr(attrs, "type") or "text"
        name = _attr(attrs, "name")
        value = _attr(attrs, "value") or ""
        if not name:
            continue
        if itype.lower() in ("hidden", "submit") or name.lower() in ("option", "submit", "other"):
            fields[name] = value
    return fields


def _attr(attrs: str, key: str) -> Optional[str]:
    m = re.search(rf"""{key}\s*=\s*(['\"])(.*?)\1""", attrs or "", re.I | re.S)
    if m:
        return html_lib.unescape(m.group(2))
    m = re.search(rf"""{key}\s*=\s*([^\s>]+)""", attrs or "", re.I)
    if m:
        return html_lib.unescape(m.group(1))
    return None


def _option_context(html: str, start: int, end: int) -> str:
    """取 option 附近一段 HTML 作为描述（同行/相邻单元格）。"""
    left = html.rfind("<tr", 0, start)
    right = html.find("</tr>", end)
    if left != -1 and right != -1 and right - left < 8000:
        return html[left:right + 5]
    left = html.rfind("<li", 0, start)
    right = html.find("</li>", end)
    if left != -1 and right != -1 and right - left < 4000:
        return html[left:right + 5]
    left = html.rfind("<td", 0, start)
    right = html.find("</td>", end)
    if left != -1 and right != -1:
        # 再吃右边一格，经典表格是 radio | 描述
        nxt = html.find("</td>", right + 5)
        if nxt != -1 and nxt - left < 4000:
            return html[left:nxt + 5]
        return html[left:right + 5]
    return html[max(0, start - 200): min(len(html), end + 800)]


def _build_item(
    option: str,
    text: str,
    action_url: str,
    method: str,
    extra_fields: Dict[str, str],
    submit_value: str,
    seen: set,
    cost_override: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if option in seen:
        return None
    low_text = (text or "").lower()
    if any(h in low_text for h in EXCLUDE_ITEM_HINTS):
        return None
    kind = _classify_kind(text)
    if kind not in ("upload", "download"):
        return None
    cost = cost_override if cost_override else _extract_cost(text)
    size_bytes = parse_size_bytes(text)
    if cost is None or cost <= 0 or not size_bytes:
        return None
    seen.add(option)
    return {
        "option": str(option),
        "kind": kind,
        "cost": float(cost),
        "size_bytes": int(size_bytes),
        "size_text": format_size(size_bytes),
        "title": text.split("\n")[0][:80] if text else f"{kind} {format_size(size_bytes)}",
        "action": action_url,
        "method": method if method in ("get", "post") else "post",
        "fields": {
            "option": str(option),
            "submit": submit_value,
            **{k: v for k, v in extra_fields.items() if k not in ("option", "submit")},
        },
    }


def _parse_official_table(html: str, base_url: str, seen: set) -> List[Dict[str, Any]]:
    """按 NexusPHP 官方 mybonus.php 结构解析兑换表。

    官方规格（xiaomlove/nexusphp master 分支实测源码）：
    - 表头四列：项目(option) | 简介(description) | 价格(points) | 交换(trade)
      （lang: col_option/col_description/col_points/col_trade，繁体为 價格/交換）
    - 每行一个 <form action="?action=exchange" method="post">（非法位置但浏览器容忍），
      内含 <input type="hidden" name="option" value="N">
    - 价格单元格是 number_format($points)，纯千分位数字
    - 不可兑换时按钮带 disabled（需要更多魔力值 / 分享率已很高 / 等级不足）
    """
    items: List[Dict[str, Any]] = []
    if not html or "<td" not in html.lower():
        return items
    low_html = html.lower()
    if "mybonus" not in low_html and "action=exchange" not in low_html:
        return items

    # 定位官方表头：项目/简介/价格(/交換)
    header_m = re.search(
        r"(?is)<tr[^>]*>(?:(?!</tr>).)*?(?:項目|项目)(?:(?!</tr>).)*?(?:簡介|简介)(?:(?!</tr>).)*?(?:價格|价格)(?:(?!</tr>).)*?</tr>",
        html,
    )
    if not header_m:
        return items
    # 找到表头所在的 table
    tbl_start = html.rfind("<table", 0, header_m.start())
    tbl_end = html.find("</table>", header_m.end())
    if tbl_start == -1 or tbl_end == -1:
        return items
    table = html[tbl_start:tbl_end + 8]

    # 价格列序号：从 0 数
    price_col = None
    col_idx = -1
    for cm in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", html[header_m.start():header_m.end()]):
        cell = strip_tags(cm.group(1))
        col_idx += 1
        if re.fullmatch(r"(?:價格|价格|points)", cell.strip(), re.I):
            price_col = col_idx
            break
    if price_col is None:
        price_col = 2  # 官方默认第三列

    # 逐行处理
    for tr_m in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", table):
        row = tr_m.group(1)
        opt_m = re.search(
            r"""<input\b[^>]*type\s*=\s*['"]hidden['"][^>]*name\s*=\s*['"]option['"][^>]*>""",
            row,
            re.I,
        ) or re.search(
            r"""<input\b[^>]*name\s*=\s*['"]option['"][^>]*type\s*=\s*['"]hidden['"][^>]*>""",
            row,
            re.I,
        )
        if not opt_m:
            continue
        option = _attr(opt_m.group(0), "value")
        if not option:
            continue
        cells = list(re.finditer(r"(?is)<td\b[^>]*>(.*?)(?:</td>|$)", row))
        texts = [strip_tags(c.group(1)) for c in cells]
        # 简介：跳过纯数字格（option 序号）与价格列
        desc = next(
            (
                t for i, t in enumerate(texts)
                if t.strip() and i != price_col and not re.fullmatch(r"\s*\d+\s*", t)
            ),
            "",
        )
        # 价格：优先取表头定位的列，否则取第一个纯数字单元格
        cost_val = None
        if 0 <= price_col < len(texts):
            cost_val = parse_number(texts[price_col])
        if cost_val is None:
            for t in texts:
                m2 = re.fullmatch(r"\s*" + _NUM + r"\s*", t or "")
                if m2:
                    cost_val = parse_number(t)
                    break
        submit_m = re.search(r"""<input\b[^>]*type\s*=\s*['"]submit['"][^>]*>""", row, re.I)
        disabled = bool(submit_m and re.search(r"""\bdisabled\b""", submit_m.group(0), re.I))
        item = _build_item(
            str(option),
            f"{desc}\n{texts[price_col] if 0 <= price_col < len(texts) else ''}",
            urljoin(base_url, "?action=exchange"),
            "post",
            {},
            _attr(submit_m.group(0), "value") if submit_m and _attr(submit_m.group(0), "value") else "交换",
            seen,
            cost_override=cost_val,
        )
        if item:
            item["disabled"] = disabled
            items.append(item)
    return items


def parse_exchange_items(html: str, base_url: str = "") -> List[Dict[str, Any]]:
    """从 mybonus 页面动态解析可兑换项目。解析失败返回空列表。

    兼容两种常见结构：
    1. 每项一个 form + hidden option
    2. 整页一个 form + radio name=option（经典 NexusPHP）
    """
    items: List[Dict[str, Any]] = []
    if not html:
        return items
    seen: set = set()
    # 官方四列表格优先（xiaomlove/nexusphp 标准 mybonus.php）
    official = _parse_official_table(html, base_url, seen)
    if official:
        return official
    forms = list(re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", html))
    scan_regions: List[Tuple[str, str, str, Dict[str, str]]] = []
    if forms:
        for fm in forms:
            form_attrs = fm.group(1)
            body = fm.group(2)
            action = _attr(form_attrs, "action") or ""
            method = (_attr(form_attrs, "method") or "post").lower()
            if "option" not in body.lower() and "exchange" not in (action + " " + body).lower():
                continue
            extra = {k: v for k, v in _extract_hidden_fields(body).items() if k.lower() not in ("option",)}
            # 兼容空 form（如 <form action="?action=exchange" method="post"></form> 后紧跟 td/input）
            if len(body.strip()) < 80:
                tr_start = html.rfind("<tr", 0, fm.start())
                tr_end = html.find("</tr>", fm.end())
                if tr_start != -1 and tr_end != -1 and (tr_end + 5 - tr_start) < 8000:
                    body = html[tr_start:tr_end + 5]
            scan_regions.append((body, action, method, extra))
    else:
        scan_regions.append((html, "", "post", {}))

    for body, action, method, extra in scan_regions:
        submit_value = extra.get("submit") or "交换"
        action_url = urljoin(base_url, action) if action else urljoin(base_url, "mybonus.php?action=exchange")
        radios = list(re.finditer(
            r"<input\b([^>]*name\s*=\s*['\"](?:option|bonusoption)['\"][^>]*)>",
            body,
            re.I,
        ))
        if radios:
            for rm in radios:
                attrs = rm.group(1)
                itype = (_attr(attrs, "type") or "radio").lower()
                if itype not in ("radio", "hidden", "checkbox"):
                    continue
                option = _attr(attrs, "value")
                if option is None or option == "":
                    continue
                ctx = _option_context(body, rm.start(), rm.end())
                item = _build_item(str(option), strip_tags(ctx), action_url, method, extra, submit_value, seen)
                if item:
                    items.append(item)
            continue
        # 单项 hidden option 的独立 form
        option = extra.get("option")
        # extra 已去掉 option；从 body 再抽一次
        hidden = _extract_hidden_fields(body)
        option = hidden.get("option") or hidden.get("bonusoption")
        extra2 = {k: v for k, v in hidden.items() if k.lower() not in ("option", "bonusoption")}
        if option is None:
            continue
        item = _build_item(str(option), strip_tags(body), action_url, method, extra2, extra2.get("submit") or submit_value, seen)
        if item:
            items.append(item)
    return items


def classify_result(html: str, status_code: int = 200) -> Dict[str, Any]:
    text = strip_tags(html or "")
    low = (html or "") + "\n" + text
    wait = parse_wait_seconds(html or "")
    result = {
        "success": False,
        "code": "unknown",
        "message": text[:200] if text else f"HTTP {status_code}",
        "wait_seconds": wait,
        "logged_in": is_logged_in(html or ""),
    }
    if status_code == 429 or wait:
        result["code"] = "rate_limit"
        result["message"] = f"站点限制，需等待 {wait if wait is not None else '未知'} 秒"
        return result
    if not result["logged_in"] or any(h in low for h in LOGIN_HINTS if h not in ("login.php",)):
        if "type=\"password\"" in (html or "").lower() or "未登录" in low or "请登录" in low:
            result["code"] = "login"
            result["message"] = "登录失效，Cookie 无效"
            result["logged_in"] = False
            return result
    if any(h in low for h in FAIL_BONUS_HINTS):
        result["code"] = "no_bonus"
        result["message"] = "魔力值不足"
        return result
    if any(h in low for h in ALREADY_HINTS):
        result["code"] = "already"
        result["success"] = True
        result["message"] = "今日已兑换过"
        return result
    if any(h in low for h in FAIL_LIMIT_HINTS):
        result["code"] = "rate_limit"
        result["message"] = "站点频率限制"
        return result
    if any(h in low for h in SUCCESS_HINTS) or ("增加" in text and ("上传" in text or "下载" in text or "上傳" in text or "下載" in text)):
        result["code"] = "ok"
        result["success"] = True
        result["message"] = "兑换成功"
        return result
    if any(h in low for h in REJECT_HINTS):
        result["code"] = "reject"
        result["message"] = "站点拒绝（反作弊校验）"
        return result
    # 借鉴 HTTP 兑换脚本的判定：POST 后站点通常直接回渲染魔力页；
    # 已登录 + 返回页仍带兑换表单 + 无任何错误提示 => 视为已受理成功
    raw = html or ""
    if status_code == 200 and result["logged_in"] and re.search(r"""name\s*=\s*['"](?:option|bonusoption)['"]""", raw, re.I):
        result["code"] = "ok"
        result["success"] = True
        result["message"] = "站点已受理（返回魔力页且无错误提示）"
        return result
    if status_code >= 400:
        result["code"] = "http"
        result["message"] = f"HTTP {status_code}"
        return result
    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if title_m:
        result["message"] = f"无法识别站点响应：{strip_tags(title_m.group(1))[:120]}"
    result["code"] = "unknown"
    return result
