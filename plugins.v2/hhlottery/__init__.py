"""
HHCLUB 自动抽奖插件 - MoviePilot V2/V3 兼容

功能：
- 自动抽奖（自适应间隔）
- 大奖即时通知
- 站内信自动清理
- Cron 定时运行
- 抽奖统计与历史记录
"""

import re
import time
import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType
from app.utils.http import RequestUtils

from app.plugins import _PluginBase
from .helpers import _load_cookiecloud_helper, _load_site_oper, _short_domain, _number


class HHLottery(_PluginBase):
    """
    HHCLUB 自动抽奖插件
    """

    # 插件元信息
    plugin_name = "HHCLUB 自动抽奖"
    plugin_desc = "HHCLUB 自动抽奖增强版 · 大奖即时通知、站内信自动清理、Cron 定时运行 · 今日/历史汇总、大奖摘要与命中详情 · 或者使用我的油猴脚本：HHCLUB 自动抽奖 · 庆典版 https://greasyfork.org/zh-CN/scripts/591722"
    plugin_icon = "hhlottery.png"
    plugin_version = "1.0.7"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "hhlottery_"
    plugin_order = 0
    auth_level = 2

    # ======================== 常量定义 ========================

    # 默认浏览器 User-Agent
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # CookieCloud/站点管理的兼容多路径（来自 helpers）
    _cookiecloud_helper_cls = None
    _site_oper_cls = None

    def _get_cookiecloud_helper(self):
        if self._cookiecloud_helper_cls is None:
            self._cookiecloud_helper_cls = _load_cookiecloud_helper()
        return self._cookiecloud_helper_cls

    def _get_site_oper(self):
        if self._site_oper_cls is None:
            self._site_oper_cls = _load_site_oper()
        return self._site_oper_cls

    def _resolve_cookie(self) -> Tuple[str, str]:
        """
        返回 (cookie, 来源说明)。cookie 为空时第二项就是错误原因。
        """
        domain = _short_domain(self._host)

        if self._cookie_source == "site":
            oper_cls = self._get_site_oper()
            if not oper_cls:
                return "", "当前 MoviePilot 版本里没找到站点管理模块，请改用手动填写"
            try:
                site = oper_cls().get_by_domain(domain)
            except Exception as err:
                return "", f"读取站点 Cookie 出错：{err}"
            if not site or not getattr(site, "cookie", None):
                return "", f"MoviePilot 站点管理里没有 {domain}，或该站点没有 Cookie"
            return site.cookie, f"站点管理（{getattr(site, 'name', domain)}）"

        # 手动填写
        cookie = self._cookie.strip()
        if not cookie:
            return "", "没有填写 Cookie"
        return cookie, "手动填写"

    # 请求 headers（抽奖用）
    DRAW_HEADERS = {
        "accept": "*/*",
        "origin": "https://hhanclub.net",
        "referer": "https://hhanclub.net/lucky.php",
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    # 默认抽奖间隔（秒）
    DEFAULT_INTERVAL = 8

    # 最大间隔（秒）
    MAX_INTERVAL = 30

    # 间隔递增系数
    INTERVAL_MULTIPLIER = 1.5

    # 连续异常停止阈值
    MAX_CONSECUTIVE_ERRORS = 5

    # 连续限流停止阈值
    MAX_CONSECUTIVE_THROTTLE = 12

    # 余额校准间隔（每 N 次抽奖校准一次余额）
    BALANCE_CHECK_INTERVAL = 20

    # 站内信清理间隔（每 N 次抽奖清理一次）
    MAIL_CLEAN_INTERVAL = 20

    # 大额憨豆阈值
    BIG_BEANS_THRESHOLD = 780000


    # ======================== 实例变量 ========================

    # 配置项
    _enabled: bool = False
    _cron: str = "5 0 * * *"
    _cookie_source: str = "manual"   # manual / site
    _host: str = "hhanclub.net"
    _cookie: str = ""
    _site_url: str = "https://hhanclub.net"
    _active_cookie: str = ""
    _interval: int = DEFAULT_INTERVAL
    _max_count: int = 0
    _reserve_beans: int = 0
    _notify: bool = True
    _big_prize_keywords: str = "VIP,邀请,780000"
    _stats_migrated: bool = False
    _clean_mail: bool = True
    _onlyonce: bool = False
    _stop_current: bool = False
    _save_marker: str = ""
    _seen_save_marker: str = ""

    # 运行状态
    _running: bool = False
    _stop_requested: bool = False
    _config_seq: int = 0
    _active_seq: int = 0
    _save_id: str = ""
    _save_epoch: int = 0
    _current_save_version: str = ""

    def init_plugin(self, config: dict = None):
        """
        初始化插件，加载配置
        """
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "5 0 * * *")
            self._cookie_source = config.get("cookie_source", "manual")
            self._host = config.get("host", "hhanclub.net")
            self._cookie = config.get("cookie", "")
            self._site_url = config.get("site_url", "https://hhanclub.net").rstrip("/")
            self._interval = int(config.get("interval") or self.DEFAULT_INTERVAL)
            self._max_count = int(config.get("max_count") or 0)
            self._reserve_beans = int(config.get("reserve_beans") or 0)
            self._log_lines = 200
            self._notify = config.get("notify", True)
            self._big_prize_keywords = config.get("big_prize_keywords", "VIP,邀请,780000")
            self._clean_mail = config.get("clean_mail", True)
            self._grand_stop = config.get("grand_stop", True)
            self._gambler_mode = config.get("gambler_mode", False)
            self._onlyonce = config.get("onlyonce", False)
            self._stop_current = config.get("stop_current", False)
            self._save_marker = config.get("save_marker", "")
            self._seen_save_marker = config.get("seen_save_marker", "")
            if self._gambler_mode:
                self._max_count = 0

            self._config_seq = int(config.get("config_seq") or (self._config_seq + 1))
            self._save_epoch = int(config.get("save_epoch") or self._save_epoch)
            self._save_id = config.get("save_id", "")
            self._current_save_version = config.get("current_save_version", "")
            logger.info(f"🧩 载入配置序号：{self._config_seq}，save_id={self._save_id!r}，current_save_version={self._current_save_version!r}，stop_current={self._stop_current}，onlyonce={self._onlyonce}")
            if self._save_marker:
                logger.info(f"🪪 当前保存标记：{self._save_marker}，已消费标记：{self._seen_save_marker or '-'}")

        # 最新保存接管：任何一次新的保存都先让旧任务退出，再按最新配置决定是否启动
        if self._cookie or self._cookie_source == "site":
            # 生成本次保存版本
            self._save_epoch += 1
            self._current_save_version = f"{self._config_seq}-{self._save_epoch}"
            logger.info(f"🧭 保存事件到达：version={self._current_save_version}，当前运行={self._running}，onlyonce={self._onlyonce}，stop_current={self._stop_current}")

            # 如果当前有任务，先停掉旧任务
            if self._running:
                logger.info(f"🛑 新保存接管：先停止旧任务（version={self._current_save_version}）")
                self._stop_requested = True
                self._running = False
                self._config_seq += 1

            # 停止开关：只停不启
            if self._stop_current:
                logger.info(f"🛑 stop_current 触发：停止当前抽奖并清除启动指令（version={self._current_save_version}）")
                self._stop_current = False
                self.update_config({
                    "enabled": self._enabled,
                    "cron": self._cron,
                    "cookie": self._cookie,
                    "site_url": self._site_url,
                    "interval": self._interval,
                    "max_count": 0 if self._gambler_mode else self._max_count,
                    "reserve_beans": self._reserve_beans,
                    "notify": self._notify,
                    "big_prize_keywords": self._big_prize_keywords,
                    "clean_mail": self._clean_mail,
                    "grand_stop": self._grand_stop,
                    "gambler_mode": self._gambler_mode,
                    "onlyonce": False,
                    "stop_current": False,
                    "config_seq": self._config_seq,
                    "save_id": self._save_id,
                    "current_save_version": self._current_save_version,
                })
                self._api_stop_lottery()
                return

            # onlyonce：只允许当前保存版本触发一次
            if self._onlyonce:
                logger.info(f"▶️ 最新保存触发 onlyonce：version={self._current_save_version}")
                self._onlyonce = False
                self.update_config({
                    "enabled": self._enabled,
                    "cron": self._cron,
                    "cookie": self._cookie,
                    "site_url": self._site_url,
                    "interval": self._interval,
                    "max_count": 0 if self._gambler_mode else self._max_count,
                    "reserve_beans": self._reserve_beans,
                    "notify": self._notify,
                    "big_prize_keywords": self._big_prize_keywords,
                    "clean_mail": self._clean_mail,
                    "grand_stop": self._grand_stop,
                    "gambler_mode": self._gambler_mode,
                    "onlyonce": False,
                    "stop_current": False,
                    "config_seq": self._config_seq,
                    "save_id": self._save_id,
                    "current_save_version": self._current_save_version,
                })
                if not self._running:
                    self._stop_requested = False
                    self._active_seq = self._config_seq
                    logger.info(f"▶️ 立即运行一次：配置序号 {self._config_seq}，version={self._current_save_version}")
                    import threading
                    threading.Thread(target=self._lottery_job, daemon=True).start()
                return

            # 普通保存：只更新，不自动起轮
            logger.info(f"💾 普通保存完成，不自动启动（version={self._current_save_version}）")
            self.update_config({
                "enabled": self._enabled,
                "cron": self._cron,
                "cookie": self._cookie,
                "site_url": self._site_url,
                "interval": self._interval,
                "max_count": 0 if self._gambler_mode else self._max_count,
                "reserve_beans": self._reserve_beans,
                "notify": self._notify,
                "big_prize_keywords": self._big_prize_keywords,
                "clean_mail": self._clean_mail,
                "grand_stop": self._grand_stop,
                "gambler_mode": self._gambler_mode,
                "onlyonce": False,
                "stop_current": False,
                "config_seq": self._config_seq,
                "save_id": self._save_id,
                "current_save_version": self._current_save_version,
            })
            return

    def get_state(self) -> bool:
        """
        获取插件启用状态
        """
        return self._enabled and (self._cookie_source == "site" or bool(self._cookie))

    def get_command(self) -> List[Dict[str, Any]]:
        """
        注册命令（供消息平台调用）
        """
        return [
            {
                "cmd": "/hhlottery",
                "event": EventType.PluginAction,
                "desc": "HHCLUB 立即抽奖",
                "category": "站点",
                "data": {
                    "action": "hhlottery_run"
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册 API 路由
        """
        return [
            {
                "path": "/hhlottery/run",
                "summary": "立即运行抽奖",
                "description": "触发一次抽奖任务",
                "method": "POST",
                "func": self._api_run_lottery,
            },
            {
                "path": "/hhlottery/stats",
                "summary": "获取抽奖统计",
                "description": "获取当前统计和历史记录",
                "method": "GET",
                "func": self._api_get_stats,
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单"""
        from .config_form import build_form
        return build_form()
    def get_page(self) -> List[dict]:
        """
        统计页面：
        - 我的抽奖信息（含盈亏率、最近运行）
        - 大奖摘要
        - 今日汇总
        - 今日大奖命中详情
        - 历史汇总
        - 历史大奖命中详情
        - 运行记录
        """
        data = self._load_data()
        stats = data.get("stats", {})
        round_records = data.get("round_records", [])

        last_balance = stats.get("last_balance", 0)
        total_count = stats.get("total_count", 0)
        total_earned = stats.get("total_earned", 0)
        total_cost = stats.get("total_cost", 0)
        total_pnl = total_earned - total_cost

        today = datetime.now().strftime("%Y-%m-%d")
        today_records = [r for r in round_records if str(r.get("time") or "")[:10] == today]

        def sum_records(records: List[dict]) -> dict:
            return {
                "count": sum(int(r.get("count", 0) or 0) for r in records),
                "cost": sum(int(r.get("cost", 0) or 0) for r in records),
                "earned": sum(int(r.get("earned", 0) or 0) for r in records),
                "wins": sum(int(r.get("wins", 0) or 0) for r in records),
            }

        def sum_metrics(records: List[dict]) -> dict:
            return {
                "vip_hits": sum(int(r.get("vip_hits", 0) or 0) for r in records),
                "invite_hits": sum(int(r.get("invite_hits", 0) or 0) for r in records),
                "vip_converted_earned": sum(int(r.get("vip_converted_earned", 0) or 0) for r in records),
                "big_beans_earned": sum(int(r.get("big_beans_earned", 0) or 0) for r in records),
            }

        today_sum = sum_records(today_records)
        history_sum = sum_records(round_records)
        today_metrics = sum_metrics(today_records)
        history_metrics = sum_metrics(round_records)
        today_pnl = today_sum["earned"] - today_sum["cost"]
        history_pnl = history_sum["earned"] - history_sum["cost"]

        # 盈亏率（盈亏 ÷ 消耗 × 100%，消耗为 0 时按 0%）
        def pnl_rate(pnl: int, cost: int) -> float:
            return pnl / cost * 100 if cost > 0 else 0.0

        today_rate = pnl_rate(today_pnl, today_sum["cost"])
        total_rate = pnl_rate(total_pnl, total_cost)

        # 盈亏颜色：正绿负红
        def pnl_color(v) -> str:
            return "success" if v >= 0 else "error"

        # 最近一次运行信息
        last_record = round_records[-1] if round_records else None
        last_time = str(last_record.get("time") or "—") if last_record else "—"
        last_stop_reason = str(last_record.get("stop_reason") or "") if last_record else ""

        # 指标块：label 左 + 值右（带颜色），下方小字说明；big=True 放大数字
        def metric_block(label: str, value: str, value_color: str = "", note: str = "", note_color: str = "", big: bool = False) -> dict:
            size = "text-h5" if big else "text-body-1"
            value_cls = f"{size} font-weight-bold text-{value_color}" if value_color else f"{size} font-weight-bold"
            rows = [{
                "component": "div",
                "props": {"class": "d-flex justify-space-between align-center py-1"},
                "content": [
                    {"component": "span", "props": {"class": "text-body-1 text-medium-emphasis"}, "text": label},
                    {"component": "span", "props": {"class": value_cls}, "text": value},
                ],
            }]
            if note:
                note_cls = f"text-caption pl-8 text-{note_color}" if note_color else "text-caption text-medium-emphasis pl-8"
                rows.append({"component": "div", "props": {"class": note_cls}, "text": note})
            return {"component": "div", "content": rows}

        # 详情行：支持值带颜色
        def detail_lines(items: List[tuple]) -> List[dict]:
            return [
                {"component": "div", "props": {"class": "d-flex justify-space-between text-body-2 py-1"}, "content": [
                    {"component": "span", "text": k},
                    {"component": "span",
                     "props": {"class": f"font-weight-bold text-{c}"} if c else {},
                     "text": v},
                ]}
                for k, v, c in items
            ]

        # 大奖摘要卡（带图标）
        def summary_card(title: str, metrics: dict) -> dict:
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [
                    {"component": "VCardTitle", "text": title},
                    {"component": "VCardText", "props": {"class": "pa-3"}, "content": detail_lines([
                        ("👑 VIP 次数", f"{metrics.get('vip_hits', 0):,}", None),
                        ("✉️ 邀请次数", f"{metrics.get('invite_hits', 0):,}", None),
                        ("💱 VIP 折算憨豆", f"{metrics.get('vip_converted_earned', 0):,}", None),
                        ("💰 大额憨豆", f"{metrics.get('big_beans_earned', 0):,}", None),
                    ])},
                ],
            }

        # 奖品分组定义（顺序即显示顺序）
        def _prize_group_defs() -> List[dict]:
            return [
                {"key": "beans", "icon": "🫘", "label": "憨豆总数", "unit": "", "items": []},
                {"key": "upload", "icon": "📤", "label": "总上传量", "unit": " GB", "items": []},
                {"key": "rainbow", "icon": "🌈", "label": "彩虹ID", "unit": " 天", "items": []},
                {"key": "makeup", "icon": "✅", "label": "补签卡", "unit": " 个", "items": []},
                {"key": "invite", "icon": "✉️", "label": "邀请", "unit": " 个", "items": []},
                {"key": "vip", "icon": "👑", "label": "VIP", "unit": " 天", "items": []},
                {"key": "big_beans", "icon": "💰", "label": "大额憨豆", "unit": "", "items": []},
            ]

        def build_prize_groups(records: List[dict]) -> tuple:
            """汇总奖品并按类别分组，返回 (groups, total_wins)。"""
            agg = {}
            for r in records:
                for name, val in (r.get("prizes") or {}).items():
                    if isinstance(val, dict):
                        ptype = val.get("type", "unknown")
                        cnt = int(val.get("count", 0) or 0)
                        total = int(val.get("total", 0) or 0)
                        value = int(val.get("value", 0) or 0)
                    else:
                        cnt = int(val or 0)
                        ptype, _, value = self._parse_prize(name)
                        total = cnt * value
                    e = agg.get(name)
                    if e is None:
                        e = {"type": ptype, "count": 0, "total": 0, "value": value}
                    e["count"] += cnt
                    e["total"] += total
                    e["type"] = ptype
                    e["value"] = value
                    agg[name] = e

            groups = _prize_group_defs()
            gmap = {g["key"]: g for g in groups}
            for name, e in agg.items():
                ptype = e["type"]
                if ptype == "beans":
                    gmap["big_beans" if e["value"] >= self.BIG_BEANS_THRESHOLD else "beans"]["items"].append((name, e))
                elif ptype == "upload":
                    gmap["upload"]["items"].append((name, e))
                elif ptype == "rainbow":
                    gmap["rainbow"]["items"].append((name, e))
                elif ptype == "makeup":
                    gmap["makeup"]["items"].append((name, e))
                elif ptype == "invite":
                    gmap["invite"]["items"].append((name, e))
                elif ptype == "vip":
                    gmap["vip"]["items"].append((name, e))
                else:
                    gmap["beans"]["items"].append((name, e))

            total_wins = sum(e["count"] for e in agg.values())
            return groups, total_wins

        # 分组明细卡片（次数 / 累计 / 占比）
        def prize_detail_card(title: str, groups: List[dict], total_wins: int) -> dict:
            def _col(text: str, cls: str, md: int) -> dict:
                return {"component": "VCol", "props": {"cols": 12, "md": md, "class": cls}, "text": text}

            def _row(cells: List[tuple]) -> dict:
                return {"component": "VRow", "props": {"class": "pa-1"}, "content": [_col(t, c, m) for t, c, m in cells]}

            rows = [_row([
                ("奖项", "text-caption font-weight-bold text-medium-emphasis", 4),
                ("次数", "text-caption font-weight-bold text-medium-emphasis text-right", 2),
                ("累计", "text-caption font-weight-bold text-medium-emphasis text-right", 3),
                ("占比", "text-caption font-weight-bold text-medium-emphasis text-right", 3),
            ])]
            for g in groups:
                items = g["items"]
                if not items:
                    continue
                g_count = sum(int(e["count"]) for _, e in items)
                g_total = sum(int(e["total"]) for _, e in items)
                g_ratio = g_count / total_wins * 100 if total_wins > 0 else 0.0
                rows.append(_row([
                    (f"{g['icon']} {g['label']}", "text-body-2 font-weight-bold", 4),
                    (f"{g_count:,}", "text-body-2 font-weight-bold text-right", 2),
                    (f"{g_total:,}{g['unit']}", "text-body-2 font-weight-bold text-right", 3),
                    (f"{g_ratio:.1f}%", "text-body-2 font-weight-bold text-right", 3),
                ]))
                for name, e in items:
                    ratio = e["count"] / total_wins * 100 if total_wins > 0 else 0.0
                    rows.append(_row([
                        (f"　　{name}", "text-body-2 text-medium-emphasis", 4),
                        (f"{e['count']:,}", "text-body-2 text-right", 2),
                        (f"{e['total']:,}{g['unit']}", "text-body-2 text-right", 3),
                        (f"{ratio:.1f}%", "text-body-2 text-right", 3),
                    ]))
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [
                    {"component": "VCardTitle", "text": title},
                    {"component": "VCardText", "props": {"class": "pa-3"}, "content": rows},
                ],
            }

        today_groups, today_wins = build_prize_groups(today_records)
        history_groups, history_wins = build_prize_groups(round_records)

        # 运行记录表格（最新在上，最多 10 次）
        run_columns = [("结束时间", 3), ("抽奖次数", 1), ("消耗", 2), ("获得", 2), ("盈亏/盈亏率", 2), ("余额", 2)]

        def run_row(cells: List[tuple]) -> dict:
            return {
                "component": "VRow",
                "props": {"class": "pa-1"},
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": md, "class": cls}, "text": text}
                    for text, cls, md in cells
                ],
            }

        header_cells = [(h, "text-caption font-weight-bold text-medium-emphasis", md) for h, md in run_columns]
        run_rows = [run_row(header_cells)]
        recent_records = list(reversed(round_records[-10:]))
        for r in recent_records:
            p = int(r.get("pnl", 0) or 0)
            c = int(r.get("cost", 0) or 0)
            rr = pnl_rate(p, c)
            pc = pnl_color(p)
            run_rows.append(run_row([
                (str(r.get("time", "—")), "text-body-2", 3),
                (f"{int(r.get('count', 0) or 0):,}", "text-body-2", 1),
                (f"{int(r.get('cost', 0) or 0):,}", "text-body-2", 2),
                (f"{int(r.get('earned', 0) or 0):,}", "text-body-2", 2),
                (f"{p:+,} / {rr:+.1f}%", f"text-body-2 font-weight-bold text-{pc}", 2),
                (f"{int(r.get('balance', 0) or 0):,}", "text-body-2", 2),
            ]))

        page = [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                            {"component": "VCardTitle", "text": "🎰 我的抽奖信息"},
                            {"component": "VCardText", "props": {"class": "pa-3"}, "content": [
                                metric_block("💰 当前憨豆", f"{last_balance:,}", "info", f"截至 {last_time}", big=True),
                                metric_block("🎲 总抽奖数", f"{total_count:,}", "", "历史以来累计抽奖数", big=True),
                                metric_block("📈 今日盈亏", f"{today_pnl:+,}", pnl_color(today_pnl),
                                             f"盈亏率 {today_rate:+.1f}%", pnl_color(today_rate), big=True),
                                metric_block("📈 总盈亏", f"{total_pnl:+,}", pnl_color(total_pnl),
                                             f"盈亏率 {total_rate:+.1f}%", pnl_color(total_rate), big=True),
                                metric_block("🕐 最近运行", last_time, "", last_stop_reason or "—"),
                            ]},
                        ]}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        summary_card("🏆 大奖摘要（总览）", history_metrics)
                    ]},
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                            {"component": "VCardTitle", "text": "📅 今日汇总"},
                            {"component": "VCardText", "props": {"class": "pa-3"}, "content": detail_lines([
                                ("今日轮次", f"{len(today_records):,}", None),
                                ("今日抽奖", f"{today_sum['count']:,}", None),
                                ("今日消耗", f"{today_sum['cost']:,}", None),
                                ("今日收益", f"{today_sum['earned']:,}", "success"),
                                ("今日盈亏", f"{today_pnl:+,}", pnl_color(today_pnl)),
                            ])},
                        ]}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        prize_detail_card("🎯 今日抽奖命中明细", today_groups, today_wins)
                    ]},
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                            {"component": "VCardTitle", "text": "📚 历史汇总"},
                            {"component": "VCardText", "props": {"class": "pa-3"}, "content": detail_lines([
                                ("历史轮次", f"{len(round_records):,}", None),
                                ("历史抽奖", f"{history_sum['count']:,}", None),
                                ("历史消耗", f"{history_sum['cost']:,}", None),
                                ("历史收益", f"{history_sum['earned']:,}", "success"),
                                ("历史盈亏", f"{history_pnl:+,}", pnl_color(history_pnl)),
                            ])},
                        ]}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                        prize_detail_card("🎯 历史抽奖命中明细", history_groups, history_wins)
                    ]},
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                            {"component": "VCardTitle", "text": "📋 运行记录（最近 10 次）"},
                            {"component": "VCardText", "props": {"class": "pa-3"}, "content": run_rows},
                        ]}
                    ]},
                ],
            },
        ]
        return page

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册定时服务
        """
        if self._enabled and (self._cookie_source == "site" or self._cookie):
            return [
                {
                    "id": "hhlottery",
                    "name": "HHCLUB 自动抽奖",
                    "trigger": "cron",
                    "cron": self._cron,
                    "func": self._lottery_job,
                    "kwargs": {},
                }
            ]
        return []

    # ======================== 核心逻辑 ========================

    def _lottery_job(self):
        """
        抽奖主任务入口
        流程：获取余额 → 循环抽奖 → 清理站内信 → 发送汇总通知
        """
        if self._running:
            logger.warning("HHCLUB 抽奖任务正在运行，跳过本次")
            return

        self._running = True
        self._stop_requested = False
        active_seq = self._active_seq = self._config_seq
        logger.info(f"🎰 HHCLUB 自动抽奖任务开始（配置序号 {active_seq}）")
        logger.info(f"🔐 当前活跃序号={self._active_seq}，最新配置序号={self._config_seq}")

        # 解析 Cookie（手动填写 / 站点管理）
        self._active_cookie, cookie_note = self._resolve_cookie()
        if not self._active_cookie:
            stop_reason = f"❌ 取不到 Cookie：{cookie_note}"
            logger.error(stop_reason)
            self._send_notification(self._format_notification("HHCLUB 抽奖异常", stop_reason))
            self._running = False
            return
        logger.info(f"🔑 Cookie 来源：{cookie_note}")

        # 初始化本轮统计
        round_stats = {
            "count": 0,
            "cost": 0,
            "wins": 0,
            "earned": 0,
            "start_balance": 0,
            "prize_detail": {},
            "history": [],
            "vip_hits": 0,
            "invite_hits": 0,
            "vip_converted_earned": 0,
            "big_beans_earned": 0,
        }

        # 停止原因
        stop_reason = ""

        # 自适应间隔
        current_interval = self._interval

        # 连续异常计数
        consecutive_errors = 0

        # 连续限流计数
        consecutive_throttle = 0

        # 是否命中大奖
        big_prize_hit = False

        try:
            # 1. 获取初始余额
            balance, cost_per_draw = self._fetch_balance()
            if balance is None:
                stop_reason = "无法获取余额，请检查 Cookie 是否有效"
                logger.error(stop_reason)
                self._send_notification(self._format_notification("❌ HHCLUB 抽奖异常", stop_reason))
                self._running = False
                return

            logger.info(f"💰 当前余额：{balance:,} 憨豆，单次消耗：{cost_per_draw:,} 憨豆")
            logger.info(f"⚙️ 开关状态：大奖止损={self._grand_stop}，赌徒模式={self._gambler_mode}，目标关键词={self._big_prize_keywords}，保留憨豆={self._reserve_beans}，最大次数={self._max_count}")
            if self._gambler_mode:
                logger.info("🎲 赌徒模式已开启：最大抽奖次数将按 0 处理并锁定为不可修改")
            round_stats["start_balance"] = balance
            before_balance = balance
            vip_converted_total = 0

            # 更新余额到统计
            self._update_stats_field("last_balance", balance)

            def _prize_tags(prize_text: str, prize_type: str):
                text = str(prize_text or "")
                tags = {
                    "vip": prize_type == "vip" or "VIP" in text,
                    "invite": "邀请" in text,
                    "big_beans": "780000" in text or "大额憨豆" in text,
                }
                return tags

            draw_count = 0

            # 2. 抽奖循环
            while True:
                if self._stop_requested or self._active_seq != self._config_seq:
                    stop_reason = f"检测到更新的配置，当前任务退出（活跃={self._active_seq}，最新={self._config_seq}）"
                    logger.info(f"♻️ {stop_reason}")
                    break
                # 检查停止条件：余额不足
                if balance < cost_per_draw:
                    stop_reason = f"余额不足（余额 {balance:,}，需要 {cost_per_draw:,}）"
                    break

                # 检查停止条件：保留憨豆
                if self._reserve_beans > 0 and balance - cost_per_draw < self._reserve_beans:
                    stop_reason = f"余额低于保留线（余额 {balance:,}，保留线 {self._reserve_beans:,}）"
                    break

                # 检查停止条件：最大次数
                if not self._gambler_mode and self._max_count > 0 and draw_count >= self._max_count:
                    stop_reason = f"达到最大抽奖次数（{self._max_count}）"
                    break

                # 检查停止条件：连续异常
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    stop_reason = f"连续 {consecutive_errors} 次异常"
                    break

                # 检查停止条件：连续限流
                if consecutive_throttle >= self.MAX_CONSECUTIVE_THROTTLE:
                    stop_reason = f"连续 {consecutive_throttle} 次被限流"
                    break

                # 执行抽奖
                try:
                    result = self._do_draw()
                except Exception as e:
                    logger.error(f"抽奖请求异常：{e}")
                    consecutive_errors += 1
                    consecutive_throttle = 0
                    time.sleep(current_interval)
                    continue

                if result is None:
                    # 请求失败
                    consecutive_errors += 1
                    consecutive_throttle = 0
                    time.sleep(current_interval)
                    continue

                # 解析结果
                ret = result.get("ret")
                data = result.get("data", {})

                if ret == -1 or ret == "throttle":
                    # 被限流
                    consecutive_throttle += 1
                    consecutive_errors = 0
                    logger.warning(f"⚠️ 被限流（第 {consecutive_throttle} 次），增加间隔")

                    # 自适应增加间隔
                    current_interval = min(
                        current_interval * self.INTERVAL_MULTIPLIER,
                        self.MAX_INTERVAL,
                    )
                    logger.info(f"⏱️ 间隔调整为 {current_interval:.1f} 秒")
                    time.sleep(current_interval)
                    continue

                if ret != 0 and ret != "0":
                    # 其他错误
                    msg = data.get("msg", "") or data.get("message", "") or str(result)
                    logger.warning(f"抽奖返回异常：ret={ret}, msg={msg}")
                    consecutive_errors += 1
                    consecutive_throttle = 0
                    time.sleep(current_interval)
                    continue

                # 成功抽奖
                consecutive_errors = 0
                consecutive_throttle = 0

                # 恢复默认间隔
                if current_interval > self._interval:
                    current_interval = self._interval
                    logger.info(f"⏱️ 间隔恢复为 {current_interval} 秒")

                draw_count += 1
                balance -= cost_per_draw

                # 解析奖品
                prize_text = data.get("prize_text", "")
                prize_type, prize_name, prize_value = self._parse_prize(prize_text)

                logger.info(f"🎰 第 {draw_count} 抽：{prize_name}（{prize_text}）")

                # 更新统计
                round_stats["count"] += 1
                round_stats["cost"] += cost_per_draw

                prize_tags = _prize_tags(prize_text, prize_type)
                if prize_tags["invite"]:
                    round_stats["invite_hits"] += 1
                if prize_tags["vip"]:
                    round_stats["vip_hits"] += 1
                if prize_tags["big_beans"]:
                    round_stats["big_beans_earned"] += prize_value if prize_type == "beans" else 0

                # 奖品统计（所有类型都记录）
                if prize_type == "beans":
                    balance += prize_value
                    round_stats["earned"] += prize_value
                    if prize_tags["big_beans"]:
                        round_stats["big_beans_earned"] += prize_value
                elif prize_type == "vip":
                    # 先按脚本逻辑：校准余额，判断是否发生 VIP→憨豆 转换
                    # 理论余额 = 抽奖前余额 - 单次消耗
                    # 如果实际余额比理论余额多出约 100 万，则认定 VIP 已转换为憨豆
                    real_balance, _ = self._fetch_balance()
                    if real_balance is not None:
                        theoretical_balance = before_balance - cost_per_draw
                        extra_beans = real_balance - theoretical_balance
                        if 900000 <= extra_beans <= 1100000:
                            converted = int(round(extra_beans))
                            balance = real_balance
                            round_stats["earned"] += converted
                            round_stats["vip_converted_earned"] += converted
                            vip_converted_total += converted
                            logger.info(f"🔄 VIP奖励已转换为憨豆 {converted:,}（余额校验确认）")
                        else:
                            balance = real_balance
                            logger.info(f"ℹ️ VIP 未转换为憨豆，按 VIP 天数统计（额外差值 {extra_beans:,}）")
                    else:
                        # 余额取不到时，退回到文案判断；只有文本里带憨豆/1000000 才算转换
                        if "憨豆" in prize_text or "1000000" in prize_text:
                            converted = prize_value or 1000000
                            round_stats["earned"] += converted
                            round_stats["vip_converted_earned"] += converted
                            vip_converted_total += converted
                            logger.info(f"🔄 VIP奖励已转换为憨豆 {converted:,}（文案判断）")
                round_stats["wins"] += 1
                entry = round_stats["prize_detail"].get(prize_name)
                if not isinstance(entry, dict):
                    entry = {"type": prize_type, "count": 0, "total": 0, "value": int(prize_value or 0)}
                entry["count"] = int(entry.get("count", 0) or 0) + 1
                entry["total"] = int(entry.get("total", 0) or 0) + int(prize_value or 0)
                entry["type"] = prize_type
                entry["value"] = int(prize_value or 0)
                round_stats["prize_detail"][prize_name] = entry

                # 记录历史
                history_item = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "prize": prize_text,
                    "type": prize_type,
                    "balance": balance,
                }
                round_stats["history"].append(history_item)

                # 检查大奖
                if self._is_big_prize(prize_text, prize_type, prize_value):
                    big_prize_hit = True
                    stop_reason = f"命中大奖：{prize_text}"
                    logger.info(f"🏆 {stop_reason}")

                    # 立即通知（喜庆风格）
                    self._send_notification(self._format_notification("🎊✨ 恭喜恭喜！天选之人！✨🎊", f"🏆 命中大奖：{prize_text}\n💰 当前余额：{balance:,} 憨豆\n🎲 本轮已抽：{draw_count} 次\n\n🎯 建议去买彩票，今天运势拉满！"))

                    # 赌徒模式开启：中奖后继续抽，不止损
                    if self._gambler_mode:
                        logger.info("🎲 逻辑结果：赌徒模式=开，大奖止损=无效，命中大奖后继续抽奖")
                        stop_reason = ""
                        big_prize_hit = False
                    elif self._grand_stop:
                        logger.info("🏆 逻辑结果：赌徒模式=关，大奖止损=开，命中大奖后停止抽奖")
                        break
                    else:
                        logger.info("ℹ️ 逻辑结果：赌徒模式=关，大奖止损=关，命中大奖后继续抽奖")


                # 定期校准余额
                if draw_count % self.BALANCE_CHECK_INTERVAL == 0:
                    real_balance, _ = self._fetch_balance()
                    if real_balance is not None:
                        balance = real_balance
                        logger.info(f"💰 余额校准：{balance:,}")

                    # 自动清理站内信
                    if self._clean_mail:
                        self._clean_messages()

                # 等待间隔
                time.sleep(current_interval)

            # 3. 最终清理站内信
            if self._clean_mail and draw_count > 0:
                self._clean_messages()

            # 3.1 抽奖结束后再校准一次最新余额
            if self._active_seq != self._config_seq:
                stop_reason = f"检测到更新的配置，停止保存旧任务结果（活跃={self._active_seq}，最新={self._config_seq}）"
                logger.info(f"♻️ {stop_reason}")
                return
            final_balance, _ = self._fetch_balance()
            if final_balance is not None:
                logger.info(f"💰 抽奖结束最新余额校准：{final_balance:,}（保存前原余额 {balance:,}）")
                balance = final_balance

            # 4. 保存统计数据
            self._save_round_stats(round_stats, balance, stop_reason)

            # 4.1 对账日志
            data = self._load_data()
            stats = data.get("stats", {})
            round_pnl = round_stats.get("earned", 0) - round_stats.get("cost", 0)
            history_pnl = stats.get("total_earned", 0) - stats.get("total_cost", 0)
            logger.info(
                "🧾 对账结果："
                f"本轮消耗={round_stats.get('cost', 0):,}，"
                f"本轮收益={round_stats.get('earned', 0):,}，"
                f"本轮盈亏={round_pnl:+,}；"
                f"历史消耗={stats.get('total_cost', 0):,}，"
                f"历史收益={stats.get('total_earned', 0):,}，"
                f"历史盈亏={history_pnl:+,}"
            )

            # 5. 发送汇总通知
            if self._notify:
                summary = self._build_summary(round_stats, balance, stop_reason)
                self._send_notification(self._format_notification("🎰 HHCLUB 抽奖结束", summary))
                overview = self._build_overview_summary(round_stats, balance)
                self._send_notification(self._format_notification("📊 HHCLUB 抽奖汇总", overview))

        except Exception as e:
            logger.error(f"HHCLUB 抽奖任务异常：{e}", exc_info=True)
            self._send_notification(self._format_notification("❌ HHCLUB 抽奖异常停止", str(e)))
        finally:
            self._running = False
            logger.info("🎰 HHCLUB 自动抽奖任务结束")

    def _do_draw(self) -> Optional[dict]:
        """
        执行一次抽奖请求

        Returns:
            dict: JSON 响应，或 None（失败）
        """
        url = f"{self._site_url}/plugin/lucky-draw"
        headers = {**self.DRAW_HEADERS, "User-Agent": self.DEFAULT_UA}

        try:
            req = RequestUtils(
                headers=headers,
                cookies=self._active_cookie,
            )
            res = req.post_res(url=url, data="")

            if res is None:
                logger.warning("抽奖请求返回 None")
                return None

            text = res.text if hasattr(res, "text") else str(res)
            if not text:
                logger.warning("抽奖响应为空")
                return None

            stripped = text.lstrip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    logger.warning(f"抽奖响应 JSON 解析失败：{e}，原始响应前 300 字：{text[:300]!r}")
                    return None

            logger.warning(f"抽奖响应不是 JSON，前 300 字：{text[:300]!r}")
            return None

        except Exception as e:
            logger.error(f"抽奖请求异常：{e}")
            return None

    def _fetch_balance(self) -> Tuple[Optional[int], int]:
        """
        获取当前余额和单次消耗

        Returns:
            (余额, 单次消耗) 或 (None, 0)
        """
        url = f"{self._site_url}/lucky.php"
        headers = {
            "User-Agent": self.DEFAULT_UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            req = RequestUtils(
                headers=headers,
                cookies=self._active_cookie,
            )
            res = req.get_res(url=url)

            if res is None:
                return None, 0

            html = res.text if hasattr(res, "text") else str(res)

            # 解析余额：.bean-number
            balance = 0
            balance_match = re.search(
                r'class=["\']bean-number["\'][^>]*>([^<]+)<', html
            )
            if balance_match:
                balance_text = balance_match.group(1).strip()
                balance = self._parse_number(balance_text)

            # 解析单次消耗：.use-bean
            cost = 0
            cost_match = re.search(
                r'class=["\']use-bean["\'][^>]*>([^<]+)<', html
            )
            if cost_match:
                cost_text = cost_match.group(1).strip()
                cost = self._parse_number(cost_text)

            # 备选解析方式
            if balance == 0:
                # 尝试其他选择器
                alt_match = re.search(r'bean[^>]*>\s*(\d[\d,]*)', html)
                if alt_match:
                    balance = self._parse_number(alt_match.group(1))

            if cost == 0:
                alt_match = re.search(r'use[-_]?bean[^>]*>\s*(\d[\d,]*)', html)
                if alt_match:
                    cost = self._parse_number(alt_match.group(1))

            return balance, cost

        except Exception as e:
            logger.error(f"获取余额异常：{e}")
            return None, 0
    def _clean_messages(self):
        """
        清理站内信（包含"幸运大转盘"主题的信件）
        完全复刻油猴脚本 sweepLotteryMail + parseMailboxPage + isLotteryMail
        反复清第一页，因为删掉后后面的信会移到第一页
        """
        logger.info("🧹 开始清理站内信...")
        MAIL_KEYWORD = "幸运大转盘"
        MAX_ROUNDS = 20
        total_deleted = 0

        try:
            # 反复清第一页（复刻油猴脚本 sweepLotteryMail）
            for round_num in range(1, MAX_ROUNDS + 1):
                url = (
                    f"{self._site_url}/messages.php?"
                    f"action=viewmailbox&box=1&page=0"
                )
                headers = {
                    "User-Agent": self.DEFAULT_UA,
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }

                req = RequestUtils(
                    headers=headers,
                    cookies=self._active_cookie,
                )
                res = req.get_res(url=url)

                if res is None:
                    logger.warning("🧹 第一页请求失败（返回 None）")
                    break

                if hasattr(res, "status_code") and res.status_code in (301, 302):
                    logger.warning("🧹 Cookie 已过期，被重定向到登录页")
                    break

                html = res.text if hasattr(res, "text") else str(res)

                if len(html) < 100:
                    logger.warning(f"🧹 第一页内容过短（{len(html)} 字节）")
                    break

                # ── 解析信件（参考油猴脚本 parseMailboxPage）──
                # 每封信的结构：
                #   <input type="checkbox" name="messages[]" value="12345">
                #   ... 所在行里有 <a href="...viewmessage...">主题文本</a>
                #
                # 步骤1：按 <input name="messages[]" value="ID"> 切分页面
                # 步骤2：对每段，找最近的 viewmessage 链接文本

                # 用正则找到所有 input checkbox 的 value
                input_pattern = re.compile(
                    r'<input[^>]*name=["\']messages\[\]["\'][^>]*value=["\']?(\d+)["\']?',
                    re.IGNORECASE
                )
                # 找所有 viewmessage 链接及其文本
                link_pattern = re.compile(
                    r'<a[^>]*href=["\'][^"\']*viewmessage[^"\']*["\'][^>]*>([^<]+)</a>',
                    re.IGNORECASE
                )

                # 把页面按 input 切分，每段包含该 input 及其后续内容（到下一个 input 为止）
                input_matches = list(input_pattern.finditer(html))
                if not input_matches:
                    # 备用：value 在前 name 在后
                    input_pattern2 = re.compile(
                        r'<input[^>]*value=["\']?(\d+)["\']?[^>]*name=["\']messages\[\]',
                        re.IGNORECASE
                    )
                    input_matches = list(input_pattern2.finditer(html))

                if not input_matches:
                    logger.info(f"🧹 第 {round_num} 轮：第一页无信件，清理完成")
                    break

                # 对每个 input，取它到下一个 input 之间的文本，找 viewmessage 链接
                delete_ids = []
                for i, m in enumerate(input_matches):
                    msg_id = m.group(1)
                    # 取当前 input 到下一个 input 之间的文本
                    start = m.start()
                    end = input_matches[i + 1].start() if i + 1 < len(input_matches) else len(html)
                    segment = html[start:end]

                    # 在这段里找 viewmessage 链接的文本
                    link_match = link_pattern.search(segment)
                    if link_match:
                        subject = link_match.group(1).strip()
                        if MAIL_KEYWORD in subject:
                            delete_ids.append(msg_id)

                if not delete_ids:
                    logger.info(f"🧹 第 {round_num} 轮：第一页无「{MAIL_KEYWORD}」信件，清理完成")
                    break

                # 删除匹配的信件
                del_data = "action=moveordel"
                for mid in delete_ids:
                    del_data += f"&messages%5B%5D={mid}"
                del_data += "&delete=%E5%88%A0%E9%99%A4"  # "删除" URL 编码

                headers_post = {
                    "User-Agent": self.DEFAULT_UA,
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": url,
                }

                resp = RequestUtils(
                    headers=headers_post,
                    cookies=self._active_cookie,
                ).post_res(
                    url=f"{self._site_url}/messages.php",
                    data=del_data,
                )

                status = resp.status_code if resp and hasattr(resp, "status_code") else "?"
                total_deleted += len(delete_ids)
                logger.info(f"🧹 第 {round_num} 轮：删除 {len(delete_ids)} 封站内信（HTTP {status}）")
                time.sleep(0.5)

        except Exception as e:
            logger.error(f"清理站内信异常：{e}", exc_info=True)

        if total_deleted > 0:
            logger.info(f"🧹 共清理 {total_deleted} 封站内信")
        else:
            logger.info("🧹 未发现需要清理的站内信")

    def _parse_prize(self, prize_text: str) -> Tuple[str, str, int]:
        """
        解析奖品文本

        Args:
            prize_text: 原始奖品文本，如 "憨豆 × 2,000"、"VIP × 7 天"

        Returns:
            (奖品类型, 奖品名称, 数值)
        """
        if not prize_text:
            return "unknown", "未知", 0

        text = prize_text.strip()

        # 憨豆 / 魔力（都是豆子）
        if "憨豆" in text or "魔力" in text:
            value = self._extract_number(text)
            return "beans", f"魔力 {value:,}", value

        # VIP
        if "VIP" in text.upper() or "vip" in text:
            value = self._extract_number(text)
            if value > 0:
                return "vip", f"VIP × {value} 天", value
            return "vip", "VIP 会员", 1

        # 邀请卡
        if "邀请" in text:
            return "invite", "邀请卡", 1

        # 彩虹 ID / 彩虹糖
        if "彩虹" in text:
            value = self._extract_number(text)
            return "rainbow", f"彩虹ID × {value} 天", value

        # 补签卡
        if "补签" in text:
            return "makeup", "补签卡", 1

        # 上传量
        if "上传" in text or "GB" in text.upper():
            value = self._extract_number(text)
            return "upload", f"上传量 × {value} GB", value

        # 未识别，作为憨豆处理（VIP 可能被折算为 1000000 憨豆）
        value = self._extract_number(text)
        if value >= 1000000:
            return "beans", f"憨豆 × {value:,}（VIP折算）", value

        return "unknown", text, 0

    def _is_big_prize(
        self, prize_text: str, prize_type: str, prize_value: int
    ) -> bool:
        """
        判断是否为大奖

        大奖条件：
        - VIP 会员
        - 邀请卡
        - 大额憨豆（≥780000）
        - 命中自定义关键词
        """
        # VIP
        if prize_type == "vip":
            return True

        # 邀请卡
        if prize_type == "invite":
            return True

        # 大额憨豆
        if prize_type == "beans" and prize_value >= self.BIG_BEANS_THRESHOLD:
            return True

        # 自定义关键词匹配
        if self._big_prize_keywords:
            keywords = [
                kw.strip()
                for kw in self._big_prize_keywords.split(",")
                if kw.strip()
            ]
            for keyword in keywords:
                # 支持数字关键词（匹配大额）
                if keyword.isdigit():
                    if prize_value >= int(keyword):
                        return True
                elif keyword.upper() in prize_text.upper():
                    return True

        return False

    def _parse_number(self, text: str) -> int:
        """
        从文本中提取数字（支持千分位逗号）
        """
        if not text:
            return 0
        # 移除逗号和空格
        cleaned = text.replace(",", "").replace(" ", "").replace("，", "")
        match = re.search(r"(\d+)", cleaned)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return 0
        return 0

    def _extract_number(self, text: str) -> int:
        """
        从奖品文本中提取数字
        如 "憨豆 × 2,000" → 2000
        """
        return self._parse_number(text)

    def _migrate_stats_once(self):
        """
        启动时对现有 round_records 做一次重算迁移，避免旧版本历史统计污染。
        """
        if self._stats_migrated:
            return
        try:
            data = self._load_data()
            if not data:
                self._stats_migrated = True
                return
            data = self._rebuild_stats_from_round_records(data)
            self._save_data(data)
            self._stats_migrated = True
            logger.info("🛠️ 历史统计重算迁移完成")
        except Exception as e:
            logger.error(f"历史统计重算迁移失败：{e}", exc_info=True)
            self._stats_migrated = True

    # ======================== 数据管理 ========================

    def _load_data(self) -> dict:
        """
        从插件数据存储加载数据
        格式：{"stats": {...}, "history": [...]}
        """
        try:
            return self.get_data("hhlottery_data") or {}
        except Exception:
            return {}

    def _save_data(self, data: dict):
        """
        保存数据到插件数据存储
        """
        try:
            self.save_data("hhlottery_data", data)
        except Exception as e:
            logger.error(f"保存数据异常：{e}")

    def _rebuild_stats_from_round_records(self, data: dict) -> dict:
        """
        根据 round_records 重建总账，避免旧版本累计口径污染。
        """
        stats = data.get("stats", {})
        round_records = data.get("round_records", [])

        total_count = 0
        total_cost = 0
        total_wins = 0
        total_earned = 0
        prize_detail = {}
        for r in round_records:
            total_count += int(r.get("count", 0) or 0)
            total_cost += int(r.get("cost", 0) or 0)
            total_wins += int(r.get("wins", 0) or 0)
            total_earned += int(r.get("earned", 0) or 0)
            for name, val in (r.get("prizes") or {}).items():
                if isinstance(val, dict):
                    # 新结构 {type, count, total, value}
                    ptype = val.get("type", "unknown")
                    cnt = int(val.get("count", 0) or 0)
                    total = int(val.get("total", 0) or 0)
                    value = int(val.get("value", 0) or 0)
                else:
                    # 旧结构 {name: count}，重新解析类型和单次值
                    cnt = int(val or 0)
                    ptype, _, value = self._parse_prize(name)
                    total = cnt * value
                entry = prize_detail.get(name)
                if not isinstance(entry, dict):
                    entry = {"type": ptype, "count": 0, "total": 0, "value": value}
                entry["count"] = int(entry.get("count", 0) or 0) + cnt
                entry["total"] = int(entry.get("total", 0) or 0) + total
                entry["type"] = ptype
                entry["value"] = value
                prize_detail[name] = entry

        stats["total_count"] = total_count
        stats["total_cost"] = total_cost
        stats["total_wins"] = total_wins
        stats["total_earned"] = total_earned
        stats["prize_detail"] = prize_detail
        stats["total_pnl"] = total_earned - total_cost
        data["stats"] = stats
        return data

    def _save_round_stats(self, round_stats: dict, final_balance: int, stop_reason: str = ""):
        """
        保存本轮统计到累计数据
        """
        data = self._load_data()
        history = data.get("history", [])

        # 保存轮次记录（最多 50 轮）
        data["round_count"] = data.get("round_count", 0) + 1
        round_number = data["round_count"]
        round_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pnl = round_stats.get("earned", 0) - round_stats["cost"]
        round_records = data.get("round_records", [])
        round_records.append({
            "number": round_number,
            "time": round_time,
            "count": round_stats["count"],
            "cost": round_stats["cost"],
            "earned": round_stats.get("earned", 0),
            "pnl": pnl,
            "balance": final_balance,
            "stop_reason": stop_reason,
            "wins": round_stats["wins"],
            "prizes": round_stats.get("prize_detail", {}),
            "vip_hits": round_stats.get("vip_hits", 0),
            "invite_hits": round_stats.get("invite_hits", 0),
            "vip_converted_earned": round_stats.get("vip_converted_earned", 0),
            "big_beans_earned": round_stats.get("big_beans_earned", 0),
        })
        round_records = round_records[-self._log_lines:]
        data["round_records"] = round_records

        # 合并历史（保留最近 200 条）
        history.extend(round_stats.get("history", []))
        history = history[-self._log_lines:]
        data["history"] = history

        # 统一按 round_records 重建总账，避免旧版本污染/重复累加
        data = self._rebuild_stats_from_round_records(data)
        stats = data.get("stats", {})
        stats["last_balance"] = final_balance
        stats["round"] = {
            "count": round_stats["count"],
            "cost": round_stats["cost"],
            "earned": round_stats.get("earned", 0),
            "pnl": pnl,
            "wins": round_stats["wins"],
            "time": round_time,
        }
        data["stats"] = stats

        self._save_data(data)

    def _update_stats_field(self, key: str, value):
        """
        更新统计字段
        """
        data = self._load_data()
        stats = data.get("stats", {})
        stats[key] = value
        data["stats"] = stats
        self._save_data(data)

    # ======================== 通知 ========================

    def _format_notification(self, title: str, body: str) -> str:
        """
        统一通知格式：时间戳 + 标题 + 正文
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (body or "").strip()
        if body:
            return f"🕒 {ts}\n{title}\n\n{body}"
        return f"🕒 {ts}\n{title}"

    def _send_notification(self, message: str):
        """
        通过 MoviePilot 通知系统发送消息（Telegram/微信/飞书等）
        """
        if not self._notify:
            return

        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="HHCLUB 自动抽奖",
                text=message,
            )
        except Exception as e:
            logger.error(f"发送通知失败：{e}")

    def _build_summary(
        self,
        round_stats: dict,
        final_balance: int,
        stop_reason: str,
    ) -> str:
        """
        构建抽奖结束汇总消息
        """
        pnl = round_stats.get("earned", 0) - round_stats.get("cost", 0)
        pnl_text = f"🟢 本轮盈亏：+{pnl:,} 憨豆" if pnl >= 0 else f"🔴 本轮盈亏：{pnl:,} 憨豆"

        lines = []
        lines.append(f"🎲 完成次数：{round_stats['count']:,}")
        lines.append(f"💸 本轮消耗：{round_stats['cost']:,} 憨豆")
        lines.append(f"🫘 当前余额：{final_balance:,} 憨豆")
        lines.append(pnl_text)

        if stop_reason:
            lines.append(f"⏹️ {stop_reason}")

        prize_detail = round_stats.get("prize_detail", {})
        if prize_detail:
            lines.append("")
            lines.append("🎁 奖品统计：")

            def _count_of(v) -> int:
                return int(v.get("count", 0) or 0) if isinstance(v, dict) else int(v or 0)

            for name, val in sorted(prize_detail.items(), key=lambda x: -_count_of(x[1])):
                lines.append(f"• {name} × {_count_of(val):,}")

        return "\n".join(lines).strip()

    def _build_overview_summary(self, round_stats: dict, final_balance: int) -> str:
        """
        构建历史+今日汇总消息（A 版：今日在前，历史在后）
        """
        data = self._load_data()
        stats = data.get("stats", {})
        round_records = data.get("round_records", [])

        today = datetime.now().strftime("%Y-%m-%d")
        today_records = [r for r in round_records if str(r.get("time") or "")[:10] == today]

        today_prizes = Counter()
        today_pnl = 0
        today_cost = 0
        today_count = 0
        for r in today_records:
            today_pnl += r.get("pnl", 0)
            today_cost += r.get("cost", 0)
            today_count += r.get("count", 0)
            for name, val in (r.get("prizes") or {}).items():
                cnt = int(val.get("count", 0) or 0) if isinstance(val, dict) else int(val or 0)
                today_prizes[name] += cnt

        overall_prizes = Counter()
        for r in round_records:
            for name, val in (r.get("prizes") or {}).items():
                cnt = int(val.get("count", 0) or 0) if isinstance(val, dict) else int(val or 0)
                overall_prizes[name] += cnt

        total_count = stats.get("total_count", 0)
        total_cost = stats.get("total_cost", 0)
        total_wins = stats.get("total_wins", 0)
        total_earned = stats.get("total_earned", 0)
        total_pnl = total_earned - total_cost

        lines = []
        lines.append("📅 今日汇总")
        lines.append(f"🗓️ 今日轮次：{len(today_records):,}")
        lines.append(f"🎲 今日抽奖：{today_count:,}")
        lines.append(f"💸 今日消耗：{today_cost:,} 憨豆")
        today_pnl_icon = "🟢" if today_pnl >= 0 else "🔴"
        lines.append(f"{today_pnl_icon} 今日盈亏：{today_pnl:+,} 憨豆")

        if today_prizes:
            lines.append("")
            lines.append("📅 今日奖品汇总：")
            for name, cnt in sorted(today_prizes.items(), key=lambda x: -x[1]):
                lines.append(f"• {name} × {cnt}")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━")
        lines.append("")
        lines.append("📚 历史汇总")
        lines.append(f"🧾 历史轮次：{len(round_records):,}")
        lines.append(f"🎲 历史抽奖：{total_count:,}")
        lines.append(f"💰 历史消耗：{total_cost:,} 憨豆")
        history_pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(f"{history_pnl_icon} 历史盈亏：{total_pnl:+,} 憨豆")
        lines.append(f"🏆 历史中奖：{total_wins:,}")
        lines.append(f"🫘 当前余额：{final_balance:,} 憨豆")

        if overall_prizes:
            lines.append("")
            lines.append("📚 历史奖品汇总：")
            for name, cnt in sorted(overall_prizes.items(), key=lambda x: -x[1]):
                lines.append(f"• {name} × {cnt}")

        return "\n".join(lines).strip()

    # ======================== API 处理 ========================

    def _api_run_lottery(self, *args, **kwargs) -> dict:
        """
        API: 立即运行抽奖
        """
        logger.info(f"▶️ API 立即运行请求：running={self._running}，配置序号={self._config_seq}，version={self._current_save_version!r}")
        if self._running:
            return {"success": False, "message": "抽奖任务正在运行中"}

        if not self._cookie and self._cookie_source != "site":
            return {"success": False, "message": "未配置 Cookie（且未选择站点管理）"}

        self._stop_requested = False
        self._active_seq = self._config_seq
        logger.info(f"▶️ API 立即运行：配置序号 {self._config_seq}，version={self._current_save_version!r}")
        import threading
        threading.Thread(target=self._lottery_job, daemon=True).start()
        return {"success": True, "message": "抽奖任务已启动"}

    def _api_stop_lottery(self, *args, **kwargs) -> dict:
        """
        API: 手动停止抽奖
        """
        self._stop_requested = True
        self._running = False
        self._config_seq += 1
        logger.info(f"🛑 服务停止，配置序号推进到 {self._config_seq}，version={self._current_save_version!r}")
        logger.info(f"🛑 收到手动停止请求，配置序号推进到 {self._config_seq}，version={self._current_save_version!r}")
        return {"success": True, "message": "已请求停止抽奖"}

    def _api_get_stats(self, *args, **kwargs) -> dict:
        """
        API: 获取统计信息
        """
        data = self._load_data()
        return {
            "success": True,
            "data": data,
        }

    def stop_service(self):
        """
        停止插件服务
        """
        self._stop_requested = True
        self._running = False
        self._config_seq += 1
        logger.info(f"🛑 服务停止，配置序号推进到 {self._config_seq}，version={self._current_save_version!r}")
