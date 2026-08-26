"""插件首页：站点列表操作中心。Lite 禁 VAvatar / VProgressLinear / VApexChart / template。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.core.config import settings

from .parser import format_size

STATUS_TEXT = {
    "idle": "未刷新",
    "ready": "可兑换",
    "running": "兑换中",
    "done": "完成",
    "skipped": "跳过",
    "login": "登录失效",
    "parse": "解析失败",
    "failed": "失败",
    "no_bonus": "魔力不足",
    "no_cookie": "无 Cookie",
    "disabled": "未启用",
}

STATUS_COLOR = {
    "idle": "text-medium-emphasis",
    "ready": "text-success",
    "running": "text-info",
    "done": "text-success",
    "skipped": "text-info",
    "login": "text-error",
    "parse": "text-error",
    "failed": "text-error",
    "no_bonus": "text-warning",
    "no_cookie": "text-warning",
    "disabled": "text-medium-emphasis",
}

WRAP = "white-space: normal; overflow-wrap: anywhere; word-break: break-all;"


def _api(plugin, path: str, extra: str = "") -> str:
    q = f"plugin/{plugin.__class__.__name__}/{path}?apikey={settings.API_TOKEN}"
    if extra:
        q += f"&{extra}"
    return q


def kpi_card(icon: str, label: str, value: str, value_color: str = "", note: str = "") -> dict:
    value_cls = f"text-h6 font-weight-bold text-{value_color}" if value_color else "text-h6 font-weight-bold"
    children = [
        {"component": "div", "props": {"class": "text-h5 mb-1"}, "text": icon},
        {"component": "span", "props": {
            "class": value_cls,
            "style": f"display:block; font-size: clamp(0.8rem, 3.8vw, 1.25rem); {WRAP} line-height: 1.2;",
        }, "text": value},
        {"component": "span", "props": {"class": "text-caption text-medium-emphasis d-block mt-1"}, "text": label},
    ]
    if note:
        children.append({"component": "span", "props": {"class": "text-caption text-medium-emphasis d-block", "style": WRAP}, "text": note})
    return {
        "component": "VCard",
        "props": {"variant": "tonal", "class": "pa-3 text-center h-100 rounded-lg"},
        "content": children,
    }


def _btn(text: str, api: str, color: str = "primary", variant: str = "tonal") -> dict:
    return {
        "component": "VBtn",
        "props": {
            "color": color,
            "variant": variant,
            "size": "small",
            "class": "ma-1",
            "text": text,
        },
        "events": {"click": {"api": api, "method": "post", "confirm": True}},
    }


def _kv(label: str, value: str, value_cls: str = "") -> dict:
    return {
        "component": "VCol",
        "props": {"cols": 6},
        "content": [
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
            {"component": "div", "props": {"class": f"text-body-2 font-weight-medium {value_cls}", "style": WRAP}, "text": value},
        ],
    }


def _num(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        num = float(v)
        if num == int(num):
            return f"{int(num):,}"
        return f"{num:,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _ratio(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _afford_text(times: Any, item: Optional[str], enabled: bool) -> str:
    if not enabled:
        return "未开启"
    try:
        n = int(times or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "0 次"
    return f"{n} 次"


def _cost_text(row: dict) -> str:
    bits = []
    if row.get("item_upload"):
        bits.append(f"↑ {row['item_upload']}")
    if row.get("item_download"):
        bits.append(f"↓ {row['item_download']}")
    return " · ".join(bits) if bits else "—"


def _limit_text(plugin) -> str:
    parts = []
    if plugin._strategy == "fixed":
        parts.append(f"固定 ↑{plugin._fixed_upload} ↓{plugin._fixed_download}")
    else:
        parts.append({"keep": "保留余额", "max": "最大化"}.get(plugin._strategy, plugin._strategy))
    parts.append(f"保留≥{plugin._keep_bonus}")
    if plugin._max_spend:
        parts.append(f"单次≤{plugin._max_spend}")
    parts.append(f"上限↑{plugin._max_upload}/↓{plugin._max_download}")
    return " · ".join(str(x) for x in parts)


def _recommend(row: dict) -> str:
    up_n = int(row.get("plan_upload") or 0)
    down_n = int(row.get("plan_download") or 0)
    bits = []
    if up_n > 0:
        bits.append(f"上传 ×{up_n}")
    if down_n > 0:
        bits.append(f"下载 ×{down_n}")
    if not bits:
        return row.get("plan_reason") or row.get("message") or "暂不兑换"
    reason = row.get("plan_reason") or ""
    return " / ".join(bits) + (f"（{reason}）" if reason else "")


def _last_result(row: dict) -> str:
    return str(row.get("last_result") or row.get("message") or "—")


def _task_status(plugin, row: dict) -> str:
    if plugin._running and row.get("status") == "running":
        return "兑换中"
    if plugin._running:
        return "队列中"
    return STATUS_TEXT.get(row.get("status") or "idle", row.get("status") or "空闲")


def _parse_unit_cost(item_text: Any) -> Optional[float]:
    if not item_text:
        return None
    m = re.search(r"/\s*([\d,]+(?:\.\d+)?)\s*", str(item_text))
    if not m:
        m = re.search(r"([\d,]+(?:\.\d+)?)\s*魔力", str(item_text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _estimate_spend(row: dict) -> Optional[float]:
    if row.get("estimate_spend") is not None:
        try:
            return float(row["estimate_spend"])
        except (TypeError, ValueError):
            pass
    up_n = int(row.get("plan_upload") or 0)
    down_n = int(row.get("plan_download") or 0)
    up_cost = _parse_unit_cost(row.get("item_upload"))
    down_cost = _parse_unit_cost(row.get("item_download"))
    total = 0.0
    known = False
    if up_n and up_cost is not None:
        total += up_n * up_cost
        known = True
    if down_n and down_cost is not None:
        total += down_n * down_cost
        known = True
    return total if known else None


def _fmt_bonus(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        num = float(v)
        if num == int(num):
            return f"{int(num):,}"
        return f"{num:,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _recommend_lines(row: dict) -> List[str]:
    up_n = int(row.get("plan_upload") or 0)
    down_n = int(row.get("plan_download") or 0)
    lines = []
    if up_n > 0:
        lines.append(f"上传 ×{up_n}")
    if down_n > 0:
        lines.append(f"下载 ×{down_n}")
    if not lines:
        reason = row.get("plan_reason") or row.get("message") or "暂不兑换"
        lines.append(str(reason))
    return lines


def _single_exec_panel(plugin, row: dict) -> dict:
    domain = row.get("domain") or ""
    name = row.get("name") or domain or "未命名站点"
    spend = _estimate_spend(row)
    spend_text = _fmt_bonus(spend) if spend is not None else "—"
    lines = _recommend_lines(row)
    reason = row.get("plan_reason") or ""
    run_api = _api(plugin, "run", f"domain={domain}")
    refresh_api = _api(plugin, "refresh", f"domain={domain}")

    body = [
        {"component": "div", "props": {"class": "text-h6 font-weight-bold mb-2", "style": WRAP}, "text": f"☑ {name}"},
        {"component": "div", "props": {"class": "text-body-1 mb-1", "style": WRAP}, "text": f"当前魔力值：{_fmt_bonus(row.get('bonus'))}"},
        {"component": "div", "props": {"class": "text-body-1 font-weight-bold mt-2"}, "text": "推荐方案："},
    ]
    for line in lines:
        body.append({"component": "div", "props": {"class": "text-body-1 ps-2", "style": WRAP}, "text": line})
    if reason and (int(row.get("plan_upload") or 0) > 0 or int(row.get("plan_download") or 0) > 0):
        body.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis ps-2", "style": WRAP}, "text": reason})
    body.append({"component": "div", "props": {"class": "text-body-1 mt-2 text-warning font-weight-bold", "style": WRAP}, "text": f"预计消耗：{spend_text}"})
    body.append({
        "component": "div",
        "props": {"class": "mt-3"},
        "content": [
            _btn("立即兑换", run_api, "primary", "flat"),
            _btn("刷新本站", refresh_api, "info"),
        ],
    })
    body.append({
        "component": "div",
        "props": {"class": "text-caption text-medium-emphasis mt-2", "style": WRAP},
        "text": f"仅处理 {name}，不影响其他站点",
    })

    return {
        "component": "VCard",
        "props": {"variant": "flat", "elevation": 2, "class": "rounded-lg mb-2", "color": "primary"},
        "content": [
            {"component": "VCardTitle", "text": "① 单站点操作"},
            {"component": "VCardSubtitle", "text": "已勾选 1 站 · 点立即兑换只跑这一站"},
            {"component": "VCardText", "content": body},
        ],
    }


def _batch_exec_panel(plugin, rows: List[dict]) -> dict:
    selected = [r for r in rows if r.get("selected")]
    names = "、".join((r.get("name") or r.get("domain") or "?") for r in selected[:6])
    if len(selected) > 6:
        names += f" 等 {len(selected)} 站"
    run_api = _api(plugin, "run", "scope=selected")
    return {
        "component": "VCard",
        "props": {"variant": "flat", "elevation": 2, "class": "rounded-lg mb-2", "color": "info"},
        "content": [
            {"component": "VCardTitle", "text": "② 批量操作"},
            {"component": "VCardSubtitle", "text": f"已勾选 {len(selected)} 站 · 站间并发 / 站内限速"},
            {"component": "VCardText", "content": [
                {"component": "div", "props": {"class": "text-body-1 mb-2", "style": WRAP}, "text": f"目标：{names or '—'}"},
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-2", "style": WRAP},
                 "text": "不同站点并发执行；每个站点内部按自身等待限制串行控制。"},
                {
                    "component": "div",
                    "content": [_btn("批量兑换", run_api, "primary", "flat")],
                },
            ]},
        ],
    }


def _smart_result_panel(plugin, snap: dict) -> dict:
    items = snap.get("items") or []
    cards = []
    for item in items:
        actionable = bool(item.get("actionable"))
        mark = "☑" if actionable else "☐"
        name = item.get("name") or item.get("domain") or "未命名"
        lines = _recommend_lines(item)
        spend = _estimate_spend(item)
        spend_text = _fmt_bonus(spend) if spend is not None else "—"
        rec_text = " / ".join(lines) if actionable else (lines[0] if lines else "暂不兑换")
        cards.append({
            "component": "VCol",
            "props": {"cols": 12, "md": 6},
            "content": [{
                "component": "VCard",
                "props": {"variant": "outlined", "class": "h-100 rounded-lg"},
                "content": [
                    {"component": "VCardTitle", "text": f"{mark} {name}"},
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "text-body-2", "style": WRAP}, "text": f"魔力值：{_fmt_bonus(item.get('bonus'))}"},
                        {"component": "div", "props": {"class": "text-body-2", "style": WRAP}, "text": f"分享率：{_ratio(item.get('ratio'))}"},
                        {"component": "div", "props": {"class": "text-body-2 font-weight-bold mt-1", "style": WRAP}, "text": f"推荐：{rec_text}"},
                        {"component": "div", "props": {"class": "text-body-2 text-warning", "style": WRAP}, "text": f"消耗：{spend_text if actionable else '—'}"},
                    ]},
                ],
            }],
        })

    run_api = _api(plugin, "run", "scope=selected")
    smart_api = _api(plugin, "smart")
    return {
        "component": "VCard",
        "props": {"variant": "flat", "elevation": 2, "class": "rounded-lg mb-2", "color": "success"},
        "content": [
            {"component": "VCardTitle", "text": "③ 智能推荐结果"},
            {"component": "VCardSubtitle", "text": f"{snap.get('time') or ''} · 可兑 {snap.get('actionable') or 0} / 共 {snap.get('total') or 0}"},
            {"component": "VCardText", "content": [
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-2", "style": WRAP},
                 "text": "已按魔力/分享率/价格/保留值/优先级生成方案，并自动勾选可兑换站。确认后可执行智能推荐，或直接一键智能兑换。"},
                {"component": "VRow", "props": {"dense": True}, "content": cards or [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "div", "props": {"class": "text-medium-emphasis"}, "text": "暂无推荐结果，请先点「智能推荐」。"}
                    ]}
                ]},
                {
                    "component": "div",
                    "props": {"class": "mt-3"},
                    "content": [
                        _btn("执行智能推荐", run_api, "primary", "flat"),
                        _btn("一键智能兑换", smart_api, "success", "flat"),
                    ],
                },
            ]},
        ],
    }


def _site_card(plugin, row: dict) -> dict:
    domain = row.get("domain") or ""
    selected = bool(row.get("selected"))
    status = row.get("status") or "idle"
    color = STATUS_COLOR.get(status, "")
    title = row.get("name") or domain or "未命名站点"
    select_api = _api(plugin, "select", f"domain={domain}&selected={'0' if selected else '1'}")
    refresh_api = _api(plugin, "refresh", f"domain={domain}")
    run_api = _api(plugin, "run", f"domain={domain}")

    header = {
        "component": "VRow",
        "props": {"align": "center", "dense": True, "class": "mb-1"},
        "content": [
            {"component": "VCol", "props": {"cols": 8}, "content": [
                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold", "style": WRAP}, "text": title},
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis", "style": WRAP}, "text": domain or "—"},
            ]},
            {"component": "VCol", "props": {"cols": 4, "class": "text-right"}, "content": [
                {"component": "div", "props": {"class": f"text-body-2 font-weight-bold {color}"}, "text": STATUS_TEXT.get(status, status)},
            ]},
        ],
    }

    fields = {
        "component": "VRow",
        "props": {"dense": True},
        "content": [
            _kv("当前魔力", _num(row.get("bonus")), "text-warning"),
            _kv("当前上传", format_size(row.get("upload")) if row.get("upload") is not None else "—"),
            _kv("当前下载", format_size(row.get("download")) if row.get("download") is not None else "—"),
            _kv("当前分享率", _ratio(row.get("ratio"))),
            _kv("可兑换上传", _afford_text(row.get("afford_upload"), row.get("item_upload"), plugin._enable_upload), "text-success"),
            _kv("可兑换下载", _afford_text(row.get("afford_download"), row.get("item_download"), plugin._enable_download)),
            _kv("单次兑换消耗", _cost_text(row)),
            _kv("当前兑换限制", _limit_text(plugin)),
        ],
    }

    recommend = {
        "component": "div",
        "props": {"class": "text-body-2 mt-2", "style": WRAP},
        "text": f"推荐方案：{_recommend(row)}",
    }
    task = {
        "component": "div",
        "props": {"class": "text-body-2", "style": WRAP},
        "text": f"当前任务：{_task_status(plugin, row)}",
    }
    last = {
        "component": "div",
        "props": {"class": "text-caption text-medium-emphasis", "style": WRAP},
        "text": f"最近结果：{_last_result(row)}",
    }

    actions = {
        "component": "div",
        "props": {"class": "mt-2"},
        "content": [
            _btn("取消选择" if selected else "选择本站", select_api, "success" if selected else "secondary", "flat" if selected else "tonal"),
            _btn("刷新", refresh_api, "info"),
            _btn("立即兑换", run_api, "primary", "flat"),
        ],
    }

    return {
        "component": "VCol",
        "props": {"cols": 12, "md": 6},
        "content": [{
            "component": "VCard",
            "props": {
                "variant": "outlined" if not selected else "flat",
                "elevation": 2 if selected else 0,
                "class": "h-100 rounded-lg",
            },
            "content": [
                {"component": "VCardTitle", "text": "☑ 已选" if selected else "☐ 未选"},
                {"component": "VCardText", "props": {"class": "pt-0"}, "content": [header, fields, recommend, task, last, actions]},
            ],
        }],
    }


def build_page(plugin, rows: List[dict], last: dict, totals: dict, records: list, smart_recommend: Optional[dict] = None) -> List[dict]:
    last = last or {}
    totals = totals or {}
    smart_recommend = smart_recommend or {}
    selected_n = sum(1 for r in rows if r.get("selected"))
    last_spent = last.get("spent") or 0
    last_up = format_size(last.get("got_upload"))
    last_down = format_size(last.get("got_download"))
    total_spent = totals.get("spent") or 0

    refresh_all = _api(plugin, "refresh")
    run_selected = _api(plugin, "run", "scope=selected")
    run_all = _api(plugin, "run", "scope=all")
    select_all = _api(plugin, "select", "scope=all&selected=1")
    select_none = _api(plugin, "select", "scope=none")
    select_ready = _api(plugin, "select", "scope=ready")
    select_recommend = _api(plugin, "select", "scope=recommended")
    recommend_api = _api(plugin, "recommend")
    smart_api = _api(plugin, "smart")

    toolbar = {
        "component": "VCard",
        "props": {"variant": "flat", "elevation": 2, "class": "rounded-lg mb-2"},
        "content": [
            {"component": "VCardTitle", "text": "📡 站点操作中心"},
            {"component": "VCardSubtitle", "text": f"已配置 {len(rows)} 站 · 已选 {selected_n} 站 · 看数据 → 勾选 → 选模式 → 执行"},
            {"component": "VCardText", "content": [
                {
                    "component": "div",
                    "props": {"class": "mb-1 text-caption text-medium-emphasis"},
                    "text": "三种模式：① 单站立即兑换  ② 勾选多站批量兑换  ③ 智能推荐 / 一键智能兑换",
                },
                {
                    "component": "div",
                    "props": {"class": "mb-1 text-caption text-medium-emphasis"},
                    "text": "勾选辅助：全选 / 取消全选 / 仅选可兑换 / 仅选有推荐",
                },
                {
                    "component": "div",
                    "content": [
                        _btn("刷新全部", refresh_all, "info"),
                        _btn("全选", select_all, "secondary"),
                        _btn("取消全选", select_none, "secondary"),
                        _btn("仅选可兑换", select_ready, "success"),
                        _btn("仅选有推荐", select_recommend, "success"),
                        _btn("批量兑换", run_selected, "primary", "flat"),
                        _btn("智能推荐", recommend_api, "success"),
                        _btn("一键智能兑换", smart_api, "warning", "flat"),
                        _btn("兑换全部", run_all, "secondary"),
                    ],
                },
            ]},
        ],
    }

    site_block: dict
    if not rows:
        site_block = {
            "component": "VCard",
            "props": {"variant": "flat", "elevation": 2, "class": "rounded-lg"},
            "content": [
                {"component": "VCardTitle", "text": "没有可用站点"},
                {"component": "VCardText", "text": "请先在 MoviePilot 站点管理中添加并启用站点、填写 Cookie。"},
            ],
        }
    else:
        site_block = {
            "component": "VRow",
            "props": {"dense": True},
            "content": [_site_card(plugin, row) for row in rows],
        }

    selected_rows = [r for r in rows if r.get("selected")]
    mode_panels: List[dict] = []
    if len(selected_rows) == 1:
        mode_panels.append(_single_exec_panel(plugin, selected_rows[0]))
    elif len(selected_rows) > 1:
        mode_panels.append(_batch_exec_panel(plugin, rows))
    if smart_recommend.get("items"):
        mode_panels.append(_smart_result_panel(plugin, smart_recommend))

    def run_tr(cells: List[tuple], head: bool = False) -> dict:
        return {
            "component": "tr",
            "content": [
                {"component": "th" if head else "td", "props": {"class": cls}, "text": text}
                for text, cls in cells
            ],
        }

    hist_table = {
        "component": "VTable",
        "props": {"hover": True, "density": "compact", "class": "run-records-table", "style": "min-width: 640px;"},
        "content": [
            {"component": "thead", "content": [run_tr([
                ("时间", "text-body-2 text-start ps-3 text-no-wrap"),
                ("成功", "text-body-2 text-center text-no-wrap"),
                ("失败", "text-body-2 text-center text-no-wrap"),
                ("消耗魔力", "text-body-2 text-center text-no-wrap"),
                ("获得上传", "text-body-2 text-center text-no-wrap"),
                ("获得下载", "text-body-2 text-center text-no-wrap"),
            ], head=True)]},
            {"component": "tbody", "content": []},
        ],
    }
    for r in reversed((records or [])[-12:]):
        hist_table["content"][1]["content"].append(run_tr([
            (str(r.get("time") or "—"), "text-body-2 text-start ps-3 text-no-wrap"),
            (str(r.get("success") or 0), "text-body-2 text-center text-no-wrap text-success"),
            (str(r.get("fail") or 0), "text-body-2 text-center text-no-wrap text-error"),
            (str(r.get("spent") or 0), "text-body-2 text-center text-no-wrap"),
            (format_size(r.get("got_upload")), "text-body-2 text-center text-no-wrap"),
            (format_size(r.get("got_download")), "text-body-2 text-center text-no-wrap"),
        ]))

    page = [
        {
            "component": "VRow",
            "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VCard",
                "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                "content": [
                    {"component": "VCardTitle", "text": "💎 兑换概况"},
                    {"component": "VCardSubtitle", "text": last.get("time") or "尚未执行"},
                    {"component": "VCardText", "props": {"class": "pa-2"}, "content": [{
                        "component": "VRow",
                        "props": {"dense": True},
                        "content": [
                            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                kpi_card("🔁", "累计次数", f"{int(totals.get('exchanges') or 0)}", "info", f"任务 {int(totals.get('runs') or 0)} 轮"),
                            ]},
                            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                kpi_card("💎", "累计消耗", f"{total_spent}", "warning", f"最近 {last_spent}"),
                            ]},
                            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                kpi_card("⬆️", "累计上传", format_size(totals.get("got_upload")), "success", f"最近 {last_up}"),
                            ]},
                            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                kpi_card("⬇️", "累计下载", format_size(totals.get("got_download")), "error", f"最近 {last_down}"),
                            ]},
                        ],
                    }]},
                ],
            }]}],
        },
        {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [toolbar]}]},
    ]
    for panel in mode_panels:
        page.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [panel]}]})
    page.extend([
        {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [site_block]}]},
        {
            "component": "VRow",
            "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VCard",
                "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                "content": [
                    {"component": "VCardTitle", "text": "📋 任务记录"},
                    {"component": "VCardSubtitle", "text": "最近 12 轮"},
                    {"component": "VCardText", "props": {"class": "pa-2", "style": "overflow-x:auto;"}, "content": [
                        hist_table if records else {"component": "div", "props": {"class": "text-medium-emphasis pa-3"}, "text": "暂无记录"}
                    ]},
                ],
            }]}],
        },
    ])
    return page
