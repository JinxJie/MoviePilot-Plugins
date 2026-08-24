"""
NodeSeek 论坛签到插件 - MoviePilot V2

功能：
- 每日定时自动签到（curl_cffi 模拟 Chrome 指纹过 Cloudflare）
- 鸡腿收益统计与签到历史记录
- 签到成功 / 失败 / Cookie 失效消息通知
- 手动签到命令与 API

说明：
NodeSeek 使用 Cloudflare 防护，纯 requests 的 TLS 指纹会被拦截（403），
本插件优先使用 curl_cffi 的浏览器指纹模拟，未安装时回退 requests 并提示。
"""

import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.schemas.types import EventType, NotificationType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase

try:
    from .yescaptcha import YesCaptchaSolver, YesCaptchaSolverError
except Exception:
    YesCaptchaSolver = None
    YesCaptchaSolverError = Exception

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except Exception:
    curl_requests = None  # type: ignore
    HAS_CURL_CFFI = False


class NodeSeek(_PluginBase):
    """
    NodeSeek 论坛签到插件
    """

    # 插件元信息
    plugin_name = "NodeSeek 自动签到"
    plugin_desc = "NodeSeek 论坛每日自动签到 · curl_cffi 浏览器指纹过 Cloudflare · 鸡腿收益统计、签到记录与消息通知"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/nodeseek.png"
    plugin_version = "1.0.1"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "nodeseek_"
    plugin_order = 0
    auth_level = 2

    # ======================== 常量定义 ========================

    # NodeSeek 签到 API（POST）
    SIGN_API = "https://www.nodeseek.com/api/attendance"

    # 默认浏览器 User-Agent
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    )

    # 完整浏览器请求头（缺 Origin/Referer 等会被 WAF 拦截）
    SIGN_HEADERS = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Length": "0",
        "Content-Type": "application/json",
        "Origin": "https://www.nodeseek.com",
        "Referer": "https://www.nodeseek.com/board",
        "Sec-CH-UA": '"Chromium";v="136", "Not:A-Brand";v="24", "Google Chrome";v="136"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": DEFAULT_UA,
    }

    # ======================== 初始化 ========================

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._cookie = ""
        self._cron = "30 0 * * *"          # 默认每天 00:30（刷新后可抢前排排名）
        self._notify = True
        self._use_proxy = False
        self._ns_random = False
        self._cookie_first = True
        self._accounts = ""
        self._solver_type = "yescaptcha"
        self._client_key = ""
        self._api_base_url = ""
        self._auto_save_cookie = True
        self._running = False

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置
        """
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cookie = (config.get("cookie") or "").strip()
            self._cron = config.get("cron") or "30 0 * * *"
            self._notify = config.get("notify", True)
            self._use_proxy = config.get("use_proxy", False)
            self._ns_random = config.get("ns_random", False)
            self._cookie_first = config.get("cookie_first", True)
            self._accounts = (config.get("accounts") or "").strip()
            self._solver_type = (config.get("solver_type") or "yescaptcha").strip().lower()
            self._client_key = (config.get("client_key") or "").strip()
            self._api_base_url = (config.get("api_base_url") or "").strip()
            self._auto_save_cookie = config.get("auto_save_cookie", True)

        # 立即运行一次（一次性开关，运行后自动复位）
        if self._onlyonce:
            scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("NodeSeek 自动签到：立即运行一次")
            scheduler.add_job(
                func=self._do_sign,
                trigger="date",
                run_date=datetime.now() + timedelta(seconds=3),
                name="NodeSeek 立即签到",
                kwargs={"source": "once"},
            )
            scheduler.start()
            # 关闭一次性开关
            self._onlyonce = False
            self.__update_config()

        if not HAS_CURL_CFFI:
            logger.warning(
                "NodeSeek 自动签到：未检测到 curl_cffi，将回退 requests 发送请求，"
                "可能被 Cloudflare 拦截（403），建议安装 curl_cffi"
            )

    def __update_config(self):
        """保存配置（用于一次性开关自动复位）"""
        try:
            self.update_config(
                {
                    "enabled": self._enabled,
                    "onlyonce": self._onlyonce,
                    "cookie": self._cookie,
                    "cron": self._cron,
                    "notify": self._notify,
                    "use_proxy": self._use_proxy,
                    "ns_random": self._ns_random,
                    "cookie_first": self._cookie_first,
                    "accounts": self._accounts,
                    "solver_type": self._solver_type,
                    "client_key": self._client_key,
                    "api_base_url": self._api_base_url,
                    "auto_save_cookie": self._auto_save_cookie,
                }
            )
        except Exception as e:
            logger.error(f"保存配置失败：{e}")

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    # ======================== 命令 / API ========================

    def get_command(self) -> List[Dict[str, Any]]:
        """注册命令（供消息平台调用）"""
        return [
            {
                "cmd": "/nodeseek",
                "event": EventType.PluginAction,
                "desc": "NodeSeek 立即签到",
                "category": "站点",
                "data": {
                    "action": "nodeseek_sign"
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册 API 路由"""
        return [
            {
                "path": "/nodeseek/sign",
                "summary": "立即执行签到",
                "description": "手动触发一次 NodeSeek 签到",
                "endpoint": self._api_sign,
                "methods": ["POST"],
            },
            {
                "path": "/nodeseek/records",
                "summary": "获取签到记录",
                "description": "获取最近签到历史记录",
                "endpoint": self._api_records,
                "methods": ["GET"],
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单"""
        from .config_form import build_form
        return build_form()

    def get_page(self) -> List[dict]:
        """统计页面：签到概况 KPI + 最近签到记录"""
        records = self._load_records()

        # ---- 汇总统计 ----
        today = datetime.now().strftime("%Y-%m-%d")
        this_month = today[:7]
        success_records = [r for r in records if r.get("success")]
        total_days = len({str(r.get("date") or "")[:10] for r in success_records})
        total_beans = sum(int(r.get("gain", 0) or 0) for r in success_records)
        month_days = len({str(r.get("date") or "")[:10] for r in success_records if str(r.get("date") or "").startswith(this_month)})

        # 最近一次签到状态
        last_status = "—"
        last_status_color = "secondary"
        last_note = "暂无记录"
        if records:
            last = records[-1]
            last_status = "成功" if last.get("success") else ("已签到" if last.get("already") else "失败")
            last_status_color = "success" if last.get("success") else ("info" if last.get("already") else "error")
            last_note = f"{(last.get('date') or '—')} {(last.get('time') or '')}".strip()
        else:
            last_status = "—"
            last_note = "暂无记录"

        # 今日已签到标记
        today_signed = any(
            (r.get("success") or r.get("already"))
            and str(r.get("date") or "")[:10] == today
            for r in records
        )

        # ---- KPI 卡 ----
        def kpi_card(icon: str, label: str, value: str, value_color: str = "", note: str = "") -> dict:
            value_cls = f"text-h6 font-weight-bold text-{value_color}" if value_color else "text-h6 font-weight-bold"
            value_props = {
                "class": value_cls,
                "style": "font-size: clamp(0.78rem, 3.8vw, 1.25rem); max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: normal; overflow-wrap: anywhere; line-height: 1.25;",
            }
            right = [
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                {"component": "div", "props": {"class": "d-flex align-center flex-wrap"}, "content": [
                    {"component": "span", "props": value_props, "text": value},
                ]},
            ]
            if note:
                right.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis", "style": "max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"}, "text": note})
            return {
                "component": "VCard",
                "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                "content": [
                    {"component": "VCardText", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VAvatar", "props": {"rounded": True, "variant": "tonal", "color": "primary", "size": "x-large", "class": "me-3 flex-shrink-0"}, "content": [
                            {"component": "span", "props": {"style": "font-size: 2.25rem; line-height: 1;"}, "text": icon},
                        ]},
                        {"component": "div", "props": {"class": "flex-grow-1", "style": "min-width: 0;"}, "content": right},
                    ]},
                ],
            }

        # ---- 签到记录表 ----
        def run_tr(cells: List[tuple], head: bool = False) -> dict:
            return {
                "component": "tr",
                "content": [
                    {"component": "th" if head else "td", "props": {"class": cls}, "text": text}
                    for text, cls in cells
                ],
            }

        run_columns = [
            ("日期", "text-body-2 text-start ps-3 text-no-wrap"),
            ("时间", "text-body-2 text-center text-no-wrap"),
            ("结果", "text-body-2 text-center text-no-wrap"),
            ("鸡腿", "text-body-2 text-center text-no-wrap"),
            ("余额", "text-body-2 text-center text-no-wrap"),
            ("说明", "text-body-2 text-start text-truncate"),
        ]
        run_table = {
            "component": "VTable",
            "props": {"hover": True, "density": "compact", "class": "run-records-table", "style": "min-width: 680px;"},
            "content": [
                {"component": "thead", "content": [run_tr(run_columns, head=True)]},
                {"component": "tbody", "content": []},
            ],
        }
        for r in reversed(records[-12:]):
            ok = r.get("success")
            result_txt = "✅ 成功" if ok else ("ℹ️ 已签" if r.get("already") else "❌ 失败")
            result_cls = "text-success" if ok else ("text-info" if r.get("already") else "text-error")
            gain = int(r.get("gain", 0) or 0)
            run_table["content"][1]["content"].append(run_tr([
                (str(r.get("date", "—")), "text-body-2 text-start ps-3 text-no-wrap"),
                (str(r.get("time", "—")), "text-body-2 text-center text-no-wrap"),
                (result_txt, f"text-body-2 font-weight-bold text-center text-no-wrap {result_cls}"),
                (f"+{gain:,}" if gain > 0 else "—", "text-body-2 text-center text-no-wrap text-success"),
                (f"{int(r.get('current', 0) or 0):,}" if r.get("current") else "—", "text-body-2 text-center text-no-wrap"),
                (str(r.get("message", "—"))[:40], "text-body-2 text-start text-truncate text-medium-emphasis"),
            ]))

        page = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12}, "content": [
                            {
                                "component": "VCard", "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"}, "content": [
                                    {"component": "VCardItem", "props": {"class": "pb-1"}, "content": [
                                        {"component": "template", "props": {"v-slot:prepend": True}, "content": [
                                            {"component": "VAvatar", "props": {"color": "primary", "variant": "tonal", "rounded": "lg"}, "content": [{"component": "span", "props": {"style": "font-size: 1.35rem"}, "text": "🗓️"}]}
                                        ]},
                                        {"component": "VCardTitle", "text": "签到概况"},
                                        {"component": "VCardSubtitle", "text": "持续记录每一次签到结果与收益"},
                                    ]},
                                    {"component": "VCardText", "props": {"class": "pa-2"}, "content": [
                                        {"component": "VRow", "props": {"dense": True}, "content": [
                                            {"component": "VCol", "props": {"cols": 6}, "content": [
                                                kpi_card("🗓️", "累计签到", f"{total_days:,}", "info", "历史成功签到天数"),
                                            ]},
                                            {"component": "VCol", "props": {"cols": 6}, "content": [
                                                kpi_card("🍗", "累计鸡腿", f"{total_beans:,}", "success", "签到累计收益"),
                                            ]},
                                            {"component": "VCol", "props": {"cols": 6}, "content": [
                                                kpi_card("📅", "本月签到", f"{month_days:,}", "", "当月成功签到天数"),
                                            ]},
                                            {"component": "VCol", "props": {"cols": 6}, "content": [
                                                kpi_card("📌", "最近签到", last_status, last_status_color, last_note),
                                            ]},
                                        ]},
                                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                                         "text": f"⏰ 今日状态：{'✅ 已签到' if today_signed else '⏳ 尚未签到'}　·　⏲ 定时任务：每日 {self._cron}"},
                                    ]},
                                ]
                            }
                        ]
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12}, "content": [
                            {
                                "component": "VCard", "props": {"variant": "flat", "elevation": 2, "class": "rounded-lg"}, "content": [
                                    {"component": "VCardItem", "props": {"class": "pb-1"}, "content": [
                                        {"component": "VAvatar", "props": {"color": "warning", "variant": "tonal", "rounded": "lg"}, "content": [{"component": "span", "props": {"style": "font-size: 1.35rem"}, "text": "📋"}]},
                                        {"component": "VCardTitle", "text": "签到记录"},
                                        {"component": "VCardSubtitle", "text": "最近 12 次运行明细"},
                                    ]},
                                    {"component": "VCardText", "props": {"class": "pa-0 overflow-x-auto"}, "content": [run_table]},
                                ]
                            }
                        ]
                    },
                ],
            },
        ]
        return page

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时服务（兼容旧版调度）"""
        if self._enabled and self._cookie:
            return [
                {
                    "id": "nodeseek_sign",
                    "name": "NodeSeek 每日签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._sign_job,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """停止服务"""
        pass

    # ======================== 核心逻辑 ========================

    def _sign_job(self):
        """定时签到任务入口，固定等待约 1 分钟，降低固定时刻请求特征。"""
        if self._running:
            logger.warning("NodeSeek 签到任务正在运行，跳过本次")
            return
        delay = random.randint(50, 70)
        logger.info("NodeSeek 签到：定时任务已触发，约 %d 秒后开始", delay)
        time.sleep(delay)
        self._running = True
        try:
            self._do_sign(source="cron")
        finally:
            self._running = False

    def _do_sign(self, source: str = "cron") -> dict:
        """
        执行签到
        """
        ts = datetime.now()
        date = ts.strftime("%Y-%m-%d")
        clock = ts.strftime("%H:%M:%S")

        result: Dict[str, Any] = {
            "success": False,
            "already": False,
            "cookie_invalid": False,
            "message": "",
            "gain": 0,
            "current": 0,
        }

        if not self._cookie:
            if self._cookie_first and self._refresh_cookie_if_needed():
                logger.info("NodeSeek：当前无 Cookie，已通过干净浏览器取得新 Cookie")
            else:
                result["message"] = "未配置 Cookie，且未能通过账号密码刷新"
                logger.error(f"NodeSeek 签到失败：{result['message']}")
                self._notify_result(result, date, clock)
                return result

        headers = dict(self.SIGN_HEADERS)
        headers["Cookie"] = self._cookie
        proxies = self._get_proxies()

        sign_url = self.SIGN_API + ("?random=true" if self._ns_random else "")
        logger.info("NodeSeek 签到模式：%s", "随机 1~11 鸡腿" if self._ns_random else "固定 5 鸡腿")
        try:
            response = self._smart_post(
                url=sign_url,
                headers=headers,
                data=b"",
                proxies=proxies,
                timeout=30,
            )
        except Exception as e:
            result["message"] = f"请求失败：{str(e)}"
            logger.error(f"NodeSeek 签到请求出错：{e}")
            self._notify_result(result, date, clock)
            return result

        # Cookie 优先：只有明确判定失效时，才用干净浏览器账密刷新并重试一次。
        result.update(self._parse_response(response))
        if result["cookie_invalid"] and self._cookie_first and self._refresh_cookie_if_needed():
            logger.info("NodeSeek：新 Cookie 已取得，重试签到一次")
            headers["Cookie"] = self._cookie
            response = self._smart_post(
                url=sign_url,
                headers=headers,
                data=b"",
                proxies=proxies,
                timeout=30,
            )
            result = {"success": False, "already": False, "cookie_invalid": False, "message": "", "gain": 0, "current": 0}
            result.update(self._parse_response(response))
        logger.info(
            f"NodeSeek 签到结果：success={result['success']} already={result['already']} "
            f"cookie_invalid={result['cookie_invalid']} message={result['message']}"
        )

        # 记录历史
        self._save_record({
            "date": date,
            "time": clock,
            "success": result["success"],
            "already": result["already"],
            "gain": result["gain"],
            "current": result["current"],
            "message": result["message"],
            "source": source,
        })

        # 通知
        self._notify_result(result, date, clock)
        return result

    def _parse_response(self, response) -> dict:
        """
        解析签到响应
        """
        result = {"success": False, "already": False, "cookie_invalid": False, "message": "", "gain": 0, "current": 0}
        status = getattr(response, "status_code", None)
        ct = (response.headers.get("Content-Type") or "").lower()

        # 非 JSON（Cloudflare 挑战页 / WAF 拦截）
        if "application/json" not in ct:
            text = (response.text or "")[:500]
            if "Just a moment" in text or "cf-challenge" in text:
                result["message"] = "Cloudflare 拦截（Just a moment），Cookie 可能已失效"
                result["cookie_invalid"] = True
            elif "high risk action" in text:
                result["message"] = "风控拦截（high risk action），Cookie 可能已失效"
                result["cookie_invalid"] = True
            elif status == 403:
                result["message"] = f"被服务器拦截（HTTP 403），可能 Cookie 过期或 IP 风控"
            else:
                result["message"] = f"非 JSON 响应（HTTP {status}）"
            return result

        try:
            data = response.json()
        except Exception:
            result["message"] = f"JSON 解析失败（HTTP {status}）"
            return result

        if not isinstance(data, dict):
            result["message"] = f"未知响应格式（HTTP {status}）"
            return result

        msg = str(data.get("message") or "")
        result["message"] = msg

        # 签到成功
        if data.get("success") is True:
            result["success"] = True
            result["gain"] = int(data.get("gain") or 0)
            result["current"] = int(data.get("current") or 0)
            if not result["gain"]:
                result["gain"] = self._extract_number(msg)
            return result

        # Cookie 失效优先于“鸡腿”文案判断
        if "USER NOT FOUND" in msg or data.get("status") == 404:
            result["cookie_invalid"] = True
            result["message"] = "USER NOT FOUND — Cookie 已失效，请更新"
            return result

        # 今日已签到
        if "已完成签到" in msg or "已签到" in msg or "鸡腿" in msg:
            result["already"] = True
            return result

        return result

    def _parse_accounts(self) -> list[dict]:
        """解析账号密码，每行支持 用户名----密码 / 用户名:密码。"""
        accounts = []
        for line in (self._accounts or "").splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in ("----", "：", ":", "|", "\t"):
                if sep in line:
                    user, password = (x.strip() for x in line.split(sep, 1))
                    if user and password:
                        accounts.append((user, password))
                    break
        return accounts

    def _solve_turnstile(self) -> str:
        """使用配置的 YesCaptcha/2Captcha 获取 Turnstile token。"""
        if not self._client_key or YesCaptchaSolver is None:
            raise RuntimeError("未配置验证码服务 API 密钥或 solver 依赖不可用")
        base = self._api_base_url
        if not base:
            base = "https://api.2captcha.com" if self._solver_type in ("2captcha", "twocaptcha") else "https://api.yescaptcha.com"
        solver = YesCaptchaSolver(api_base_url=base, client_key=self._client_key, proxies=self._get_proxies(), soft_id=None if self._solver_type in ("2captcha", "twocaptcha") else "62709")
        token = solver.solve(url="https://www.nodeseek.com/signIn.html", sitekey="0x4AAAAAAAaNy7leGjewpVyR", user_agent=self.DEFAULT_UA)
        if not token:
            raise RuntimeError("验证码服务未返回 token")
        return token

    def _browser_login(self, user: str, password: str) -> str:
        """在干净 CloakBrowser 上下文中登录，不注入旧 Cookie，返回新业务 Cookie。"""
        try:
            from cloakbrowser import launch_context
        except Exception as e:
            raise RuntimeError(f"CloakBrowser 不可用：{e}") from e
        token = self._solve_turnstile()
        context = page = None
        try:
            logger.info("NodeSeek：Cookie 失效，使用干净 CloakBrowser 上下文登录")
            context = launch_context(headless=True)
            page = context.new_page()
            page.goto("https://www.nodeseek.com/signIn.html", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass
            result = page.evaluate("""
            async ({token, username, password}) => {
              const out = {};
              let pre = null;
              try {
                const u = performance.getEntriesByType('resource').map(x => x.name).find(x => /\\/assets\\/preLogin-[^/]*\\.js/.test(x));
                if (u) pre = await import(u);
              } catch (e) { out.error = 'preLogin unavailable'; }
              let headers = {};
              try { if (pre && pre.g) headers = await pre.g(); } catch (e) { out.error = 'integrity header failed'; }
              const r = await fetch('/api/account/signIn', {method:'POST', credentials:'include', headers:Object.assign({'Content-Type':'application/json','x-captcha-token':token,'x-captcha-source':'turnstile'}, headers), body:JSON.stringify({username,password})});
              out.status = r.status; try { out.body = await r.json(); } catch (e) { out.body = {}; }
              return out;
            }
            """, {"token": token, "username": user, "password": password}) or {}
            body = result.get("body") or {}
            if not body.get("success"):
                raise RuntimeError(f"账密登录失败（HTTP {result.get('status', 0)}）")
            pairs = {}
            for item in (context.cookies() or []):
                name, value, domain = item.get("name"), item.get("value"), item.get("domain", "")
                if name and "nodeseek.com" in domain and name != "cf_clearance":
                    pairs[name] = value
            if not pairs:
                raise RuntimeError("登录成功但未取得 NodeSeek Cookie")
            cookie = "; ".join(f"{k}={v}" for k, v in pairs.items())
            logger.info("NodeSeek：干净浏览器登录成功，已取得新的 Cookie 字段")
            return cookie
        finally:
            try:
                if page:
                    page.close()
            finally:
                if context:
                    context.close()

    def _refresh_cookie_if_needed(self) -> bool:
        """Cookie 失效后用第一个账号刷新并写回配置。"""
        accounts = self._parse_accounts()
        if not accounts:
            logger.warning("NodeSeek：Cookie 已失效，但未配置账号密码")
            return False
        user, password = accounts[0]
        try:
            new_cookie = self._browser_login(user, password)
            self._cookie = new_cookie
            if self._auto_save_cookie:
                self.__update_config()
                logger.info("NodeSeek：新 Cookie 已写回插件配置")
            return True
        except Exception as e:
            logger.error("NodeSeek：自动登录刷新 Cookie 失败：%s", e)
            return False

    def _smart_post(self, url: str, headers: dict, data=None, proxies=None, timeout: int = 30):
        """
        智能 POST：
        1) curl_cffi（Chrome 指纹，过 Cloudflare）
        2) requests（兜底）
        """
        if HAS_CURL_CFFI and curl_requests:
            try:
                logger.info("NodeSeek 签到：使用 curl_cffi 发送请求（Chrome 指纹）")
                resp = curl_requests.post(
                    url, headers=headers, data=data,
                    impersonate="chrome", proxies=proxies, timeout=timeout,
                )
                ct = (resp.headers.get("Content-Type") or "").lower()
                if resp.status_code not in (400, 403) or "application/json" in ct:
                    return resp
                logger.warning(f"curl_cffi 响应异常：HTTP {resp.status_code} {ct}")
            except Exception as e:
                logger.warning(f"curl_cffi 请求失败，将回退 requests：{e}")

        # requests 兜底
        import requests
        logger.warning("NodeSeek 签到：使用 requests 兜底（无浏览器指纹，可能被 Cloudflare 拦截）")
        resp = requests.post(
            url, headers=headers, data=data, proxies=proxies, timeout=timeout
        )
        ct = (resp.headers.get("Content-Type") or "").lower()
        if resp.status_code in (400, 403) and "application/json" not in ct:
            raise Exception(f"requests 被拦截：HTTP {resp.status_code}（建议安装 curl_cffi）")
        return resp

    def _get_proxies(self):
        """获取并规范化系统代理，兼容字符串、字典和空值。"""
        if not self._use_proxy:
            return None
        try:
            configured = getattr(settings, "PROXY", None)
            if not configured:
                logger.warning("已启用代理，但系统未配置代理")
                return None

            # MoviePilot 不同版本可能返回代理 URL 或 {http, https} 字典。
            if isinstance(configured, str):
                proxy = configured.strip()
                if not proxy:
                    logger.warning("已启用代理，但系统代理地址为空")
                    return None
                return {"http": proxy, "https": proxy}

            if isinstance(configured, dict):
                proxies = {}
                for scheme in ("http", "https"):
                    value = configured.get(scheme)
                    if isinstance(value, str) and value.strip():
                        proxies[scheme] = value.strip()
                # 兼容仅配置 proxy 或 all 的格式。
                fallback = configured.get("proxy") or configured.get("all")
                if isinstance(fallback, str) and fallback.strip():
                    fallback = fallback.strip()
                    proxies.setdefault("http", fallback)
                    proxies.setdefault("https", fallback)
                if proxies:
                    return proxies

            logger.warning("已启用代理，但系统代理格式无法识别（仅支持字符串或 URL 字典）")
        except Exception as e:
            logger.error(f"获取系统代理设置出错：{type(e).__name__}: {e}")
        return None

    # ======================== 数据存储 ========================

    def _load_records(self) -> List[dict]:
        """加载签到历史"""
        try:
            data = self.get_data("nodeseek_data") or {}
            return data.get("records", []) or []
        except Exception:
            return []

    def _save_record(self, record: dict):
        """保存一条签到记录（最多保留 365 条）"""
        try:
            data = self.get_data("nodeseek_data") or {}
            records = data.get("records", []) or []
            records.append(record)
            records = records[-365:]
            data["records"] = records
            self.save_data("nodeseek_data", data)
        except Exception as e:
            logger.error(f"保存签到记录失败：{e}")

    # ======================== 通知 ========================

    def _notify_result(self, result: dict, date: str, clock: str):
        """按结果类型发送通知"""
        if not self._notify:
            return
        if result["success"]:
            gain = int(result.get("gain", 0) or 0)
            current = int(result.get("current", 0) or 0)
            lines = [
                f"✅ NodeSeek 签到成功",
                "",
                f"🍗 获得 +{gain:,} 鸡腿" if gain > 0 else "🍗 签到成功",
            ]
            if current:
                lines.append(f"💰 当前 {current:,} 鸡腿")
            self._send_notification(self._format_notification("NodeSeek 自动签到", "\n".join(lines)))
        elif result["already"]:
            self._send_notification(self._format_notification(
                "NodeSeek 自动签到", "ℹ️ 今日已签到过，明天再来\n\n💬 " + (result.get("message") or "已完成签到")
            ))
        else:
            if result["cookie_invalid"]:
                body = "⚠️ Cookie 已失效（USER NOT FOUND）\n\n请重新获取 Cookie 后更新插件配置"
            else:
                body = f"❌ 签到失败\n\n{result.get('message') or '未知错误'}"
            self._send_notification(self._format_notification("NodeSeek 自动签到", body))

    def _format_notification(self, title: str, body: str) -> str:
        """统一通知格式：时间戳 + 标题 + 正文"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (body or "").strip()
        if body:
            return f"🕒 {ts}\n{title}\n\n{body}"
        return f"🕒 {ts}\n{title}"

    def _send_notification(self, message: str):
        """通过 MoviePilot 通知系统发送消息"""
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="NodeSeek 自动签到",
                text=message,
            )
        except Exception as e:
            logger.error(f"发送通知失败：{e}")

    # ======================== 工具函数 ========================

    @staticmethod
    def _extract_number(text: str) -> int:
        """从文本中提取第一个数字"""
        if not text:
            return 0
        m = re.search(r"-?\d+(?:,\d+)*", text.replace(",", ""))
        return int(m.group()) if m else 0

    # ======================== API 端点 ========================

    def _api_sign(self, *args, **kwargs) -> dict:
        """API：手动触发签到"""
        if self._running:
            return {"code": 0, "message": "签到任务正在运行中", "data": {}}
        result = self._do_sign(source="api")
        return {
            "code": 1 if result["success"] or result["already"] else 0,
            "message": result.get("message") or "",
            "data": {
                "success": result["success"],
                "already": result["already"],
                "gain": result.get("gain", 0),
                "current": result.get("current", 0),
                "cookie_invalid": result.get("cookie_invalid", False),
            },
        }

    def _api_records(self, *args, **kwargs) -> dict:
        """API：获取最近签到记录"""
        records = self._load_records()
        return {
            "code": 1,
            "message": "success",
            "data": list(reversed(records[-30:])),
        }
