"""癫影幸运转盘 — MoviePilot V3 原生插件。仅在用户明确开启后抽奖。"""
import base64
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List

from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.schemas.types import NotificationType

try:
    from curl_cffi import requests as curl_requests
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
except ImportError:
    curl_requests = ec = hashes = None


class Dian115Wheel(_PluginBase):
    plugin_name = "癫影幸运转盘"
    plugin_desc = "癫影每日幸运转盘：可选定时抽奖、次数与余额限制、中奖记录及通知。每次抽奖会消耗站点积分。"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/dian115wheel.png"
    plugin_version = "3.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "dian115wheel_"
    plugin_order = 0
    auth_level = 2
    BASE = "https://m.dian115.com"
    API = "/api/portal"
    WHEEL = API + "/lottery/wheel"
    STATUS = API + "/games/status"
    ME = API + "/me"

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._token = ""
        self._cron = "0 10 * * *"
        self._spin_mode = "custom"
        self._max_spins = 1
        self._min_balance = 5
        self._notify = True
        self._use_system_proxy = True
        self._proxy = ""
        self._session = None
        self._key = None
        self._proof = ""
        self._session_expiry = 0
        self._clock_skew = 0
        self._session_id = ""
        self._signature_version = 1
        self._visitor_id = str(uuid.uuid4())
        self._lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._token = self._extract_token(config.get("token") or "")
        self._cron = config.get("cron") or "0 10 * * *"
        self._spin_mode = "maximum" if config.get("spin_mode") == "maximum" else "custom"
        self._max_spins = max(1, min(100, int(config.get("max_spins") if config.get("max_spins") is not None else 1)))
        self._min_balance = max(0, int(config.get("min_balance") if config.get("min_balance") is not None else 5))
        self._notify = bool(config.get("notify", True))
        self._use_system_proxy = bool(config.get("use_system_proxy", True))
        self._proxy = str(config.get("proxy") or "").strip()
        if config.get("onlyonce"):
            self.update_config({"enabled": self._enabled, "token": self._token, "cron": self._cron,
                                "spin_mode": self._spin_mode,
                                "max_spins": self._max_spins, "min_balance": self._min_balance,
                                "notify": self._notify, "use_system_proxy": self._use_system_proxy,
                                "proxy": self._proxy, "onlyonce": False})
            # 不阻塞配置保存；手动运行也要遵守积分和次数护栏
            threading.Thread(target=self._run, daemon=True, name="dian115-wheel-once").start()

    @staticmethod
    def _extract_token(raw):
        raw = str(raw).strip()
        for item in raw.split(";"):
            item = item.strip()
            if item.startswith(("__Host-portal_token=", "portal_token=")):
                return item.split("=", 1)[1].strip()
        # 粘贴整段 Cookie 时不得将其他 Cookie 当作 Token 发出。
        return "" if ";" in raw or "=" in raw else raw

    @staticmethod
    def _b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _system_proxy():
        proxy = getattr(getattr(settings, "PROXY", None), "host", None)
        return str(proxy or os.environ.get("PROXY_HOST") or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "").split(",")[0].strip()

    def _http(self):
        if curl_requests is None or ec is None:
            raise RuntimeError("缺少 curl_cffi 或 cryptography 依赖")
        if self._session is None:
            self._session = curl_requests.Session(impersonate="chrome110")
            proxy = self._system_proxy() if self._use_system_proxy else self._proxy
            if proxy:
                self._session.proxies = {"http": proxy, "https": proxy}
            # 浏览器访问首页获取 WAF 会话；不在日志中输出 Cookie
            self._session.get(self.BASE + "/", timeout=15)
        return self._session

    def _headers(self, referer="/lottery"):
        result = {"Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9",
                  "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
                  "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
                  "Referer": self.BASE + referer, "Origin": self.BASE,
                  "X-Portal-Current-Path": referer, "X-Portal-Visitor-ID": self._visitor_id}
        if self._token:
            result["Cookie"] = "__Host-portal_token=" + self._token
        return result

    def _ensure_browser_session(self, force=False):
        now = time.time()
        if not force and now < self._session_expiry:
            return
        http = self._http()
        proof_response = http.get(self.BASE + self.API + "/auth/browser-challenge",
                                  headers=self._headers(), timeout=15)
        proof_response.raise_for_status()
        challenge = proof_response.json()
        if challenge.get("enabled") is False:
            self._key = None
            self._proof = ""
            self._session_expiry = now + 300
            return
        self._proof = challenge.get("proof") or ""
        if not self._proof:
            raise RuntimeError("browser-challenge 未返回 proof")
        proof_ttl = max(60, int(challenge.get("ttl") or 600))
        self._key = ec.generate_private_key(ec.SECP256R1())
        numbers = self._key.public_key().public_numbers()
        jwk = {"kty": "EC", "crv": "P-256", "x": self._b64(numbers.x.to_bytes(32, "big")),
               "y": self._b64(numbers.y.to_bytes(32, "big"))}
        headers = self._headers()
        headers["X-Portal-Browser-Proof"] = self._proof
        response = http.post(self.BASE + self.API + "/auth/browser-session",
                             json={"public_jwk": jwk, "signature_version": 2}, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("code") not in (None, "ok") or data.get("enabled") is False:
            raise RuntimeError("browser-session 注册失败")
        self._session_id = str(data.get("session_id") or "")
        self._signature_version = 2 if int(data.get("signature_version") or 1) == 2 and self._session_id else 1
        self._clock_skew = int(data.get("server_time_ms") or int(time.time() * 1000)) - int(time.time() * 1000)
        self._session_expiry = time.time() + max(30, min(proof_ttl - 30, int(data.get("ttl") or 1800) - 300))

    def _signed_headers(self, method, path):
        headers = self._headers()
        if self._proof:
            headers["X-Portal-Browser-Proof"] = self._proof
        if not self._key:
            return headers
        ts = str(int(time.time() * 1000) + self._clock_skew)
        nonce = self._b64(os.urandom(24))
        if self._signature_version == 2:
            message = f"portal-browser-request/v2\n{self._session_id}\n{method}\n{path}\n{ts}\n{nonce}"
            headers["X-Portal-Browser-Session"] = self._session_id
            headers["X-Portal-Browser-Version"] = "2"
        else:
            message = f"portal-browser-request/v1\n{method}\n{path.split('?', 1)[0]}\n{ts}\n{nonce}"
        der = self._key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
        # WebCrypto ECDSA signature is raw r||s, not DER.
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        r, s = decode_dss_signature(der)
        headers.update({"X-Portal-Browser-TS": ts, "X-Portal-Browser-Nonce": nonce,
                        "X-Portal-Browser-Sig": self._b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))})
        return headers

    def _request(self, method, path, retry=True):
        self._ensure_browser_session()
        response = self._http().request(method, self.BASE + path,
                                        headers=self._signed_headers(method, path), timeout=15)
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise RuntimeError(f"站点返回非 JSON（HTTP {response.status_code}）")
        if not isinstance(data, dict):
            raise RuntimeError("站点响应格式无效")
        if retry and method != "POST" and data.get("code") in ("browser_proof_invalid", "browser_proof_required"):
            self._ensure_browser_session(force=True)
            return self._request(method, path, retry=False)
        if response.status_code >= 400 or data.get("code") not in (None, "ok"):
            code = str(data.get("code") or "")
            # 不把站点返回的任意 msg 写入通知/日志，避免反射敏感信息。
            raise RuntimeError(f"站点接口失败：HTTP {response.status_code}" + (f"，代码 {code}" if code and code.isidentifier() else ""))
        return data

    def _records(self):
        data = self.get_data("wheel_records") or []
        return data if isinstance(data, list) else []

    def _run(self):
        if not self._lock.acquire(blocking=False):
            return {"code": 1, "msg": "抽奖任务正在运行"}
        try:
            return self._run_locked()
        finally:
            self._lock.release()

    def _run_locked(self):
        if not self._token:
            return {"code": 1, "msg": "请先填写登录 Token"}
        results = []
        error = ""
        initial_remaining = None
        try:
            # 最大次数模式逐次读取站点限额；首次额度也作为本轮上限，防止站点计数暂未更新时超抽。
            for _ in range(self._max_spins if self._spin_mode == "custom" else 100):
                status = self._request("GET", self.STATUS)
                wheel = (status.get("items") or {}).get("daily_wheel") or {}
                used, maximum = int(wheel.get("used_today") or 0), int(wheel.get("max_plays") or 0)
                if self._spin_mode == "maximum":
                    if initial_remaining is None:
                        initial_remaining = max(0, maximum - used)
                    if len(results) >= initial_remaining:
                        break
                raw_cost = wheel.get("cost")
                if isinstance(raw_cost, bool) or raw_cost is None or int(raw_cost) < 0:
                    raise RuntimeError("站点未提供有效的单次抽奖消耗，已停止以保护积分")
                cost = int(raw_cost)
                if maximum <= used:
                    error = "今日转盘次数已用完或站点未开放"
                    break
                account = self._request("GET", self.ME)
                points = int((account.get("user") or {}).get("points") or 0)
                if points - cost < self._min_balance:
                    error = f"积分不足：余额 {points}，单次消耗 {cost}，需保留 {self._min_balance}"
                    break
                # 仅此接口会消费积分；不得在 HTTP 失败后自动重试，避免重复抽奖。
                result = self._request("POST", self.WHEEL, retry=False)
                prize = result.get("prize")
                if not isinstance(prize, dict) or not any(key in prize for key in ("label", "points", "vip_days")):
                    raise RuntimeError("抽奖请求已发送但站点未返回可识别奖品；请先在站点核实积分，勿重试")
                row = {"date": datetime.now().isoformat(timespec="seconds"), "cost": int(result.get("cost") if result.get("cost") is not None else cost),
                       "label": str(prize.get("label") or "未知"), "points": int(prize.get("points") or 0),
                       "vip_days": int(prize.get("vip_days") or 0)}
                results.append(row)
                self.save_data("wheel_records", (self._records() + [row])[-500:])
        except Exception as exc:
            # 不记录请求头/Token/返回体，避免凭据泄露。
            error = str(exc)[:200] if isinstance(exc, RuntimeError) else type(exc).__name__
            logger.warning(f"癫影转盘执行中断：{error}")
        if results and self._notify:
            labels = "、".join(r["label"] for r in results)
            try:
                self.post_message(mtype=NotificationType.Plugin, title="癫影幸运转盘", text=f"抽奖 {len(results)} 次：{labels}" + (f"；{error}" if error else ""))
            except Exception:
                logger.warning("癫影转盘通知发送失败")
        return {"code": 0 if results else 1, "msg": f"完成 {len(results)} 次" + (f"；{error}" if error else ""), "data": results}

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._token:
            return []
        return [{"id": "Dian115Wheel", "name": "癫影幸运转盘", "trigger": CronTrigger.from_crontab(self._cron),
                 "func": self._run, "kwargs": {}}]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{"path": "/spin", "endpoint": self._run, "methods": ["POST"], "auth": "bear"}]

    def get_state(self):
        return self._enabled and bool(self._token)

    def stop_service(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def get_form(self):
        def field(component, model, label, **props):
            return {"component": component, "props": {"model": model, "label": label,
                                                    **{k.replace("_", "-"): v for k, v in props.items()}}}
        form = [{"component": "VForm", "content": [
            field("VSwitch", "enabled", "启用定时抽奖"),
            field("VTextField", "token", "癫影 __Host-portal_token", type="password", hint="登录 m.dian115.com 后从浏览器 Cookie 复制；不与签到插件共享登录状态", persistent_hint=True),
            field("VTextField", "cron", "Cron（默认每天 10:00）"),
            field("VSelect", "spin_mode", "抽奖次数模式", items=[
                {"title": "自定义次数", "value": "custom"},
                {"title": "最大次数（抽完今日剩余额度）", "value": "maximum"}],
                  hint="最大次数按站点今日剩余额度执行；两种模式均逐次检查积分保留限制", persistent_hint=True),
            field("VTextField", "max_spins", "自定义次数（仅自定义模式生效）", type="number", min="1", max="100"),
            field("VTextField", "min_balance", "抽奖后至少保留积分", type="number", min="0"),
            field("VSwitch", "notify", "中奖结果通知"),
            field("VSwitch", "use_system_proxy", "使用系统代理（默认开启）"),
            field("VTextField", "proxy", "自定义代理（关闭系统代理时生效）"),
            field("VSwitch", "onlyonce", "保存后立即运行一次（消耗积分）"),
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "幸运转盘会消耗积分；最大次数按站点今日剩余额度执行，逐次检查积分与限额；不自动重试抽奖 POST。"}},
        ]}]
        return form, {"enabled": False, "token": "", "cron": "0 10 * * *", "spin_mode": "custom", "max_spins": 1,
                      "min_balance": 5, "notify": True, "use_system_proxy": True, "proxy": "", "onlyonce": False}

    def get_page(self):
        all_records = self._records()
        records = list(reversed(all_records[-20:]))
        def item(label, value):
            return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardText", "props": {"class": "text-center"}, "content": [
                        {"component": "div", "props": {"style": "font-size:clamp(1rem,4vw,1.5rem);overflow-wrap:anywhere;"}, "text": str(value)},
                        {"component": "div", "props": {"class": "text-caption"}, "text": label}]}]}]}
        total_cost = sum(int(r.get("cost") or 0) for r in all_records)
        total_points = sum(int(r.get("points") or 0) for r in all_records)
        cards = [item("已记录抽奖", len(all_records)), item("消耗积分", total_cost), item("奖励积分", total_points),
                 item("净积分", total_points - total_cost)]
        rows = [{"component": "tr", "content": [{"component": "td", "text": str(r.get(k) if r.get(k) is not None else "—")}
                for k in ("date", "label", "cost", "points", "vip_days")]} for r in records]
        return [{"component": "VRow", "content": cards}, {"component": "VCard", "props": {"class": "mt-3"}, "content": [
            {"component": "VCardTitle", "text": "最近抽奖记录（站点结果，以本地记录为准）"},
            {"component": "VCardText", "props": {"style": "overflow-x:auto"}, "content": [
                {"component": "VTable", "props": {"style": "min-width:540px"}, "content": [
                    {"component": "thead", "content": [{"component": "tr", "content":
                        [{"component": "th", "text": t} for t in ("时间", "奖品", "消耗", "积分奖励", "VIP 天数")]}]},
                    {"component": "tbody", "content": rows}]}]}]}]
