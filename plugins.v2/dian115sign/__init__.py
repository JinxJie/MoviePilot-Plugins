"""
癫影自动签到插件 - MoviePilot V2/V3 兼容

功能：
- 每日定时自动签到（普通签 / 运气签开关）
- curl_cffi Chrome 指纹过站点 WAF
- ECDSA P-256 动态密钥 + browser-session 请求签名（portal-browser-request/v1）
- 账号信息展示：积分、连续签到、最近签到
- 签到记录与结果通知
"""

import base64
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.context import Context
from app.log import logger
from app.schemas.types import EventType, NotificationType
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except Exception:
    curl_requests = None  # type: ignore
    HAS_CURL_CFFI = False

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes as _crypto_hashes
    HAS_CRYPTO = True
except Exception:
    ec = None  # type: ignore
    _crypto_hashes = None  # type: ignore
    HAS_CRYPTO = False


class Dian115Sign(_PluginBase):
    """
    癫影自动签到插件
    """

    # 插件元信息
    plugin_name = "癫影自动签到"
    plugin_desc = "癫影每日自动签到 · 普通签/运气签模式开关 · 连续签到与积分统计 · 签到记录 · 结果通知 · 失败重试"
    plugin_icon = "https://m.dian115.com/favicon.ico"
    plugin_version = "1.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "dian115sign_"
    plugin_order = 0
    auth_level = 2

    # ======================== 常量定义 ========================

    BASE_URL = "https://m.dian115.com"
    API_BASE = "/api/portal"

    SIGN_MODE_NORMAL = "normal"
    SIGN_MODE_LUCKY = "lucky"

    LUCKY_TIERS = {
        "jackpot": "大奖",
        "normal": "平手",
        "blank": "空签",
        "penalty": "倒霉",
    }

    # browser-session 有效期（秒），提前 5 分钟刷新
    SESSION_TTL_MARGIN = 300

    # curl_cffi 浏览器指纹回退链：
    # 从各版本都原生支持的老目标开始（0.5.x 只有 chrome99~110/edge/safari；
    # 新版虽有 chrome124+ 但老 libcurl 实现的 TLS 细节可能被 WAF 识别）
    IMPERSONATE_CHAIN = ["chrome110", "chrome107", "chrome104", "chrome101", "chrome99", "edge101", "safari15_5", "chrome124"]

    def __init__(self):
        super().__init__()
        self._session = None
        self._priv_key = None
        self._proof = None
        self._proof_expires_at = 0
        self._session_expires_at = 0
        self._clock_skew_ms = 0
        self._visitor_id = str(uuid.uuid4())

    # ======================== 配置项声明 ========================

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled") or False
            self._token = self._extract_token((config.get("token") or "").strip())
            self._lucky_mode = bool(config.get("lucky_mode"))
            self._cron = config.get("cron") or "30 9 * * *"
            self._notify = config.get("notify")
            self._notify = False if self._notify is None else self._notify
            self._retry = config.get("retry")
            self._retry = 3 if self._retry is None else int(self._retry)
            self._use_system_proxy = config.get("use_system_proxy")
            self._use_system_proxy = True if self._use_system_proxy is None else bool(self._use_system_proxy)
            self._proxy = (config.get("proxy") or "").strip()
            self._onlyonce = config.get("onlyonce") or False

            # 手动立即运行一次
            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()
                try:
                    self._sign_job(manual=True)
                except Exception as e:
                    logger.error(f"癫影手动签到失败：{e}")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "token": self._token,
            "lucky_mode": self._lucky_mode,
            "cron": self._cron,
            "notify": self._notify,
            "retry": self._retry,
            "proxy": getattr(self, "_proxy", ""),
            "use_system_proxy": getattr(self, "_use_system_proxy", True),
            "onlyonce": self._onlyonce,
        })

    @property
    def _mode(self) -> str:
        return self.SIGN_MODE_LUCKY if getattr(self, "_lucky_mode", False) else self.SIGN_MODE_NORMAL

    @staticmethod
    def _extract_token(raw: str) -> str:
        """兼容整段 Cookie 或纯 token 值，自动提取 __Host-portal_token"""
        if not raw:
            return ""
        if "eyJ" in raw and "__Host-portal_token=" in raw:
            for part in raw.split(";"):
                part = part.strip()
                if part.startswith("__Host-portal_token="):
                    return part.split("=", 1)[1].strip()
        return raw

    def get_state(self) -> bool:
        return bool(self._enabled and self._token)

    # ======================== 命令 / API / 服务 ========================

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/dian115_sign",
                "event": EventType.PluginAction,
                "desc": "癫影立即签到",
                "category": "",
                "data": {"action": "dian115_sign"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/signin",
                "endpoint": self._api_signin,
                "methods": ["GET"],
                "auth": "bear",
            },
            {
                "path": "/account",
                "endpoint": self._api_account,
                "methods": ["GET"],
                "auth": "bear",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时服务"""
        if self.get_state():
            return [
                {
                    "id": "Dian115Sign",
                    "name": "癫影自动签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._sign_job,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        pass

    def _api_signin(self, *args, **kwargs) -> Dict[str, Any]:
        """API：立即签到"""
        result = self._do_sign(manual=True)
        code = 0 if result.get("ok") else 1
        msg = result.get("msg") or ("成功" if result.get("ok") else "失败")
        return {"code": code, "msg": msg, "data": {k: v for k, v in result.items() if k not in ("ok", "msg")}}

    def _api_account(self, *args, **kwargs) -> Dict[str, Any]:
        """API：账号信息"""
        info = self._fetch_me()
        if info is None:
            return {"code": 1, "msg": "获取失败，请检查 Token"}
        return {"code": 0, "msg": "", "data": {
            "nickname": info.get("nickname"),
            "email": info.get("email"),
            "points": info.get("points"),
            "consecutive": info.get("consecutive_signin"),
            "last_signin_date": info.get("last_signin_date"),
        }}

    # ======================== HTTP 客户端 ========================

    def _http(self):
        """curl_cffi 会话（Chrome 指纹，按版本能力逐级回退；支持可选代理）"""
        if not HAS_CURL_CFFI:
            raise RuntimeError("curl_cffi 未安装，无法模拟浏览器指纹")
        if self._session is None:
            last_err = None
            for target in self.IMPERSONATE_CHAIN:
                try:
                    self._session = curl_requests.Session(impersonate=target)
                    logger.info(f"癫影签到使用浏览器指纹：{target}")
                    break
                except Exception as e:
                    last_err = e
                    continue
            if self._session is None:
                raise RuntimeError(f"curl_cffi 无可用浏览器指纹，请升级 curl_cffi：{last_err}")
            proxy = (getattr(self, "_proxy", "") or "").strip()
            if getattr(self, "_use_system_proxy", False):
                sys_proxy = self._system_proxy()
                if sys_proxy:
                    self._session.proxies = {"http": sys_proxy, "https": sys_proxy}
                    logger.info(f"癫影签到使用系统代理：{sys_proxy}")
                else:
                    logger.warning("癫影签到开启了系统代理，但 MoviePilot 未配置代理服务器，将直连")
            elif proxy:
                self._session.proxies = {"http": proxy, "https": proxy}
                logger.info(f"癫影签到走自定义代理：{proxy}")
        return self._session

    @staticmethod
    def _system_proxy() -> str:
        """读取 MoviePilot 系统设置中的代理（settings.PROXY.host）"""
        try:
            from app.core.config import settings
            proxy_host = getattr(getattr(settings, "PROXY", None), "host", None)
            if proxy_host:
                return str(proxy_host).strip()
        except Exception as e:
            logger.debug(f"癫影读取系统代理失败：{e}")
        # 兜底：容器环境变量
        import os
        return (os.environ.get("PROXY_HOST") or os.environ.get("https_proxy") or
                os.environ.get("HTTPS_PROXY") or "").split(",")[0].strip() or ""

    def _base_headers(self) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": f"{self.BASE_URL}/me/signin",
            "Cookie": f"__Host-portal_token={self._token}",
        }
        if not HAS_CRYPTO:
            return headers
        return headers

    def _b64u(self, raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _gen_keypair(self):
        """生成 ECDSA P-256 密钥对，返回 (私钥, jwk)"""
        priv = ec.generate_private_key(ec.SECP256R1())
        nums = priv.public_key().public_numbers()
        jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": self._b64u(nums.x.to_bytes(32, "big")),
            "y": self._b64u(nums.y.to_bytes(32, "big")),
        }
        return priv, jwk

    def _get_proof(self, force: bool = False) -> Optional[str]:
        """获取 browser-challenge proof 令牌（有效期约 10 分钟）"""
        now = time.time()
        if not force and self._proof and now < self._proof_expires_at:
            return self._proof
        r = self._http().get(
            f"{self.BASE_URL}{self.API_BASE}/auth/browser-challenge",
            headers=self._base_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"癫影 browser-challenge 失败：HTTP {r.status_code}")
            return None
        data = r.json() or {}
        if data.get("code") != "ok" or not data.get("proof"):
            logger.warning(f"癫影 browser-challenge 返回异常：{data}")
            return None
        self._proof = data["proof"]
        ttl = int(data.get("ttl") or 600)
        self._proof_expires_at = now + max(ttl - self.SESSION_TTL_MARGIN, 60)
        return self._proof

    def _ensure_session(self) -> bool:
        """注册 ECDSA 公钥建立 browser-session（有效期约 30 分钟）"""
        if not HAS_CRYPTO:
            logger.error("癫影签到需要 cryptography 库支持")
            return False
        now = time.time()
        if self._priv_key and self._session_expires_at and now < self._session_expires_at:
            return True
        proof = self._get_proof(force=True)
        if not proof:
            return False
        self._priv_key, jwk = self._gen_keypair()
        headers = self._base_headers()
        headers.update({
            "Content-Type": "application/json",
            "Origin": self.BASE_URL,
            "X-Portal-Browser-Proof": proof,
        })
        r = self._http().post(
            f"{self.BASE_URL}{self.API_BASE}/auth/browser-session",
            json={"public_jwk": jwk},
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"癫影 browser-session 失败：HTTP {r.status_code}")
            return False
        data = r.json() or {}
        if data.get("code") != "ok":
            logger.warning(f"癫影 browser-session 返回异常：{data}")
            return False
        server_ms = data.get("server_time_ms")
        if server_ms:
            self._clock_skew_ms = int(server_ms) - int(time.time() * 1000)
        ttl = int(data.get("ttl") or 1800)
        self._session_expires_at = time.time() + max(ttl - self.SESSION_TTL_MARGIN, 60)
        return True

    def _signed_headers(self, method: str, path: str) -> dict:
        """生成带签名请求头"""
        ts = str(int(time.time() * 1000) + self._clock_skew_ms)
        nonce = self._b64u(os.urandom(18))
        message = f"portal-browser-request/v1\n{method.upper()}\n{path}\n{ts}\n{nonce}"
        signature = self._priv_key.sign(message.encode(), ec.ECDSA(_crypto_hashes.SHA256()))
        headers = self._base_headers()
        headers.update({
            "Content-Type": "application/json",
            "Origin": self.BASE_URL,
            "X-Portal-Visitor-ID": self._visitor_id,
            "X-Portal-Current-Path": "/me/signin",
            "X-Portal-Browser-Proof": self._proof or "",
            "X-Portal-Browser-TS": ts,
            "X-Portal-Browser-Nonce": nonce,
            "X-Portal-Browser-Sig": self._b64u(signature),
        })
        return headers

    def _request(self, method: str, path: str) -> Optional[dict]:
        """带签名发起 API GET/POST"""
        if not self._ensure_session():
            return None
        headers = self._signed_headers(method, path)
        url = f"{self.BASE_URL}{path}"
        try:
            if method.upper() == "POST":
                r = self._http().post(url, json={}, headers=headers, timeout=15)
            else:
                r = self._http().get(url, headers=headers, timeout=15)
        except Exception as e:
            logger.error(f"癫影请求异常 {path}：{e}")
            return None
        if r.status_code == 403 and "blocked" in r.text.lower():
            logger.warning(f"癫影请求被 WAF 拦截：{path}")
            return None
        try:
            return {"status": r.status_code, **(r.json() or {})}
        except Exception:
            logger.warning(f"癫影响应非 JSON：{path} HTTP {r.status_code}")
            return None

    # ======================== 业务接口 ========================

    def _fetch_me(self) -> Optional[dict]:
        """获取账号信息"""
        resp = self._request("GET", f"{self.API_BASE}/me")
        if not resp or resp.get("code") != "ok":
            return None
        user = resp.get("user") or {}
        return {
            "nickname": user.get("nickname"),
            "email": user.get("email"),
            "points": user.get("points"),
            "consecutive_signin": user.get("consecutive_signin"),
            "last_signin_date": user.get("last_signin_date"),
        }

    def _fetch_calendar(self) -> list:
        """获取签到日历记录（接口按当前月返回，无需传参；带 query 会破坏签名路径）"""
        resp = self._request("GET", f"{self.API_BASE}/signin/calendar")
        if not resp or resp.get("code") != "ok":
            return []
        items = resp.get("items") or []
        month = datetime.now().strftime("%Y-%m")
        # 只保留当月记录
        return [it for it in items if str(it.get("signin_date") or "").startswith(month)]

    def _do_sign(self, manual: bool = False) -> dict:
        """执行一次签到"""
        today = datetime.now().strftime("%Y-%m-%d")

        me_before = self._fetch_me()
        if me_before is None:
            return {"ok": False, "msg": "Token 无效或网络被拦截", "date": today}

        if str(me_before.get("last_signin_date") or "") >= today:
            record = {
                "date": today,
                "time": datetime.now().strftime("%H:%M:%S"),
                "success": True,
                "already": True,
                "award": 0,
                "balance": me_before.get("points"),
                "streak": me_before.get("consecutive_signin"),
                "mode": self._mode,
                "tier": "",
            }
            self._append_record(record)
            return {"ok": True, "already": True, "msg": "今日已签到", **record}

        mode = self.SIGN_MODE_LUCKY if self._mode == self.SIGN_MODE_LUCKY else self.SIGN_MODE_NORMAL
        path = f"{self.API_BASE}/signin"
        headers = self._signed_headers("POST", path)
        body = {"mode": mode}
        try:
            r = self._http().post(
                f"{self.BASE_URL}{path}",
                json=body,
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            logger.error(f"癫影签到请求异常：{e}")
            return {"ok": False, "msg": f"请求异常：{e}", "date": today}

        if r.status_code == 403 and "blocked" in r.text.lower():
            return {"ok": False, "msg": "被 WAF 拦截", "date": today}

        try:
            data = r.json() or {}
        except Exception:
            return {"ok": False, "msg": f"响应解析失败 HTTP {r.status_code}", "date": today}

        if r.status_code == 409 or data.get("code") == "already_signed":
            record = {
                "date": today,
                "time": datetime.now().strftime("%H:%M:%S"),
                "success": True,
                "already": True,
                "award": 0,
                "balance": me_before.get("points"),
                "streak": me_before.get("consecutive_signin"),
                "mode": mode,
                "tier": "",
            }
            self._append_record(record)
            return {"ok": True, "already": True, "msg": "今日已签到", **record}

        if r.status_code != 200 or data.get("code") == "error":
            msg = data.get("msg") or f"HTTP {r.status_code}"
            return {"ok": False, "msg": msg, "date": today}

        award = int(data.get("award") or 0)
        tier = str(data.get("lucky_tier") or "")
        record = {
            "date": today,
            "time": datetime.now().strftime("%H:%M:%S"),
            "success": True,
            "already": False,
            "award": award,
            "balance": data.get("new_balance"),
            "streak": (me_before.get("consecutive_signin") or 0) + 1,
            "mode": data.get("mode") or mode,
            "tier": tier,
            "multiplier": data.get("multiplier"),
        }
        self._append_record(record)

        result = {"ok": True, "already": False, "msg": "签到成功", **record}
        # 补充最新余额
        me_after = self._fetch_me()
        if me_after:
            result["balance"] = me_after.get("points")
            result["streak"] = me_after.get("consecutive_signin")
            record["balance"] = me_after.get("points")
            record["streak"] = me_after.get("consecutive_signin")
            self._save_records(self._records)
        return result

    # ======================== 定时任务 ========================

    def _sign_job(self, manual: bool = False):
        """定时签到任务入口"""
        if not self._token:
            logger.warning("癫影签到：未配置 Token，无法执行签到")
            return
        # 手动测试仅需 Token；定时计划才要求「启用插件」开关
        if not manual and not self._enabled:
            return
        logger.info(f"癫影自动签到开始（{'手动' if manual else '定时'}），模式：{'运气签' if self._mode == self.SIGN_MODE_LUCKY else '普通签'}")

        max_retry = max(int(self._retry or 0), 0)
        result = {}
        for attempt in range(max_retry + 1):
            result = self._do_sign(manual=manual)
            if result.get("ok"):
                break
            if attempt < max_retry:
                logger.warning(f"癫影第 {attempt + 1} 次签到失败：{result.get('msg')}，稍后重试")
                time.sleep(60)
        else:
            pass

        if result.get("ok"):
            logger.info(f"癫影签到完成：{result.get('msg')}")
        else:
            logger.error(f"癫影签到最终失败：{result.get('msg')}")

        if self._notify:
            self._send_notify(result)

    def _send_notify(self, result: dict):
        """发送签到结果通知"""
        if not result:
            return
        today = result.get("date") or datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if result.get("ok") and not result.get("already"):
            mode_label = "运气签" if result.get("mode") == self.SIGN_MODE_LUCKY else "普通签"
            tier_label = self.LUCKY_TIERS.get(result.get("tier") or "", "")
            award = int(result.get("award") or 0)
            if result.get("mode") == self.SIGN_MODE_LUCKY and tier_label:
                title_emoji = "🎉" if tier_label == "大奖" else ("😰" if award < 0 else "🎲")
            else:
                title_emoji = "✅"
            lines = [
                f"📅 癫影签到成功",
                "",
                f"{title_emoji} 签到模式：{mode_label}" + (f" · {tier_label}" if tier_label and result.get("mode") == self.SIGN_MODE_LUCKY else ""),
                f"🧧 获得积分：{'+' if award >= 0 else ''}{award}",
                f"💰 当前余额：{result.get('balance', '—')}",
                f"🔥 连续签到：{result.get('streak', '—')} 天",
                "",
                f"🕒 {now}",
            ]
            title = f"癫影签到成功（{today}）"
            text = "\n".join(lines)
        elif result.get("ok") and result.get("already"):
            title = f"癫影已签到（{today}）"
            text = "\n".join([
                f"📅 今日已完成签到，无需重复操作",
                "",
                f"💰 当前余额：{result.get('balance', '—')}",
                f"🔥 连续签到：{result.get('streak', '—')} 天",
                "",
                f"🕒 {now}",
            ])
        else:
            title = f"癫影签到失败（{today}）"
            text = "\n".join([
                f"❌ 失败原因：{result.get('msg') or '未知'}",
                f"🔁 已重试 {max(int(self._retry or 0), 0)} 次",
                "",
                f"⚠️ 请检查 Token 是否有效或网络是否可达",
                "",
                f"🕒 {now}",
            ])

        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as e:
            logger.error(f"癫影通知发送失败：{e}")

    # ======================== 数据存储 ========================

    def _load_records(self) -> list:
        try:
            data = self.get_data("dian115_records") or []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @property
    def _records(self) -> list:
        if not hasattr(self, "__cached_records"):
            self.__cached_records = self._load_records()
        return self.__cached_records

    def _save_records(self, records: list):
        records = records[-500:]
        try:
            self.save_data("dian115_records", records)
            self.__cached_records = records
        except Exception as e:
            logger.error(f"癫影保存签到记录失败：{e}")

    def _append_record(self, record: dict):
        records = [r for r in self._records if str(r.get("date")) != str(record.get("date"))]
        records.append(record)
        self._save_records(records)

    # ======================== 页面渲染 ========================

    def get_page(self) -> List[dict]:
        """统计页面：账号信息 KPI + 本月签到记录"""
        records = sorted(self._records, key=lambda r: str(r.get("date")), reverse=True)
        me = None
        if self._token:
            try:
                me = self._fetch_me()
            except Exception:
                me = None

        points = (me or {}).get("points")
        streak = (me or {}).get("consecutive_signin")
        nickname = (me or {}).get("nickname")
        last_date = (me or {}).get("last_signin_date")

        total_award = sum(int(r.get("award") or 0) for r in records)
        success_days = len({r.get("date") for r in records if r.get("success")})

        def kpi(icon: str, label: str, value: str, color: str = "", note: str = "") -> dict:
            value_cls = f"text-h6 font-weight-bold text-{color}" if color else "text-h6 font-weight-bold"
            value_props = {
                "class": value_cls,
                "style": "font-size: clamp(0.8rem, 3.8vw, 1.25rem); white-space: normal; overflow-wrap: anywhere; word-break: break-all; line-height: 1.2;",
            }
            right = [
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                {"component": "div", "props": {"class": "d-flex align-center flex-wrap"}, "content": [
                    {"component": "span", "props": value_props, "text": value},
                ]},
            ]
            if note:
                right.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": note})
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [
                    {"component": "VCardText", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VAvatar", "props": {"rounded": True, "variant": "tonal", "color": "primary", "size": "x-large", "class": "me-3 flex-shrink-0"}, "content": [
                            {"component": "span", "props": {"style": "font-size: 2.25rem; line-height: 1;"}, "text": icon},
                        ]},
                        {"component": "div", "props": {"class": "flex-grow-1", "style": "min-width: 0;"}, "content": right},
                    ]},
                ],
            }

        kpis = [
            ("🧑‍🚀", "账号昵称", str(nickname or "未获取"), "primary", f"邮箱：{(me or {}).get('email') or '—'}"),
            ("💰", "当前积分", f"{int(points):,}" if isinstance(points, (int, float)) else "—", "info", "站点实时数据"),
            ("🔥", "连续签到", f"{int(streak):,} 天" if isinstance(streak, (int, float)) else "—", "success", f"最近签到：{last_date or '—'}"),
            ("🧧", "累计获得", f"{total_award:,}", "warning", f"本地共记录 {success_days} 天"),
        ]

        page: List[dict] = [
            {
                "component": "VRow",
                "props": {"dense": True},
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [kpi(*kpis[0])]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [kpi(*kpis[1])]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [kpi(*kpis[2])]},
                    {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [kpi(*kpis[3])]},
                ],
            },
        ]

        # ---- 签到记录 ----
        rows = []
        for idx, r in enumerate(records[:30], start=1):
            ok = bool(r.get("success"))
            already = bool(r.get("already"))
            status = "已签到" if already else ("成功" if ok else "失败")
            status_color = "success" if (ok and not already) else ("info" if already else "error")
            mode = "运气签" if r.get("mode") == self.SIGN_MODE_LUCKY else "普通签"
            tier = self.LUCKY_TIERS.get(r.get("tier") or "", "")
            award = int(r.get("award") or 0)
            award_text = f"{'+' if award > 0 else ''}{award}"
            rows.append([
                (str(idx), "text-body-2 text-medium-emphasis"),
                (str(r.get("date") or "—"), "text-body-2"),
                (str(r.get("time") or ""), "text-body-2 text-center"),
                (mode + (f" · {tier}" if tier else ""), "text-body-2 text-center"),
                (award_text, f"text-body-2 font-weight-bold text-center text-{'success' if award > 0 else ('error' if award < 0 else 'medium-emphasis')}"),
                (str(r.get("balance") if r.get("balance") is not None else "—"), "text-body-2 text-center"),
                (status, f"text-body-2 text-center text-{status_color}"),
            ])

        header_cells = [
            ("#", "text-caption text-medium-emphasis"),
            ("日期", "text-caption text-medium-emphasis"),
            ("时间", "text-caption text-center text-medium-emphasis"),
            ("模式", "text-caption text-center text-medium-emphasis"),
            ("积分", "text-caption text-center text-medium-emphasis"),
            ("余额", "text-caption text-center text-medium-emphasis"),
            ("状态", "text-caption text-center text-medium-emphasis"),
        ]

        table_content = [
            {
                "component": "VTable",
                "props": {"density": "comfortable"},
                "content": [
                    {"component": "thead", "content": [
                        {"component": "tr", "content": [
                            {"component": "th", "props": {"class": c}, "text": t} for t, c in header_cells
                        ]},
                    ]},
                    {"component": "tbody", "content": [
                        {"component": "tr", "content": [
                            {"component": "td", "props": {"class": c}, "text": t} for t, c in row
                        ]} for row in rows
                    ]},
                ],
            },
        ] if rows else [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无签到记录，等待首次签到后生成"}},
        ]

        page.append({
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                        {"component": "VCardTitle", "text": "📋 签到记录（最近 30 条）"},
                        {"component": "VCardText", "props": {"class": "pa-2"}, "content": table_content},
                    ]},
                ]},
            ],
        })

        return page

    # ======================== 表单 ========================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        from .config_form import build_form
        return build_form()
