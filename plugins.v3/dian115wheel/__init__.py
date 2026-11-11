"""癫影娱乐 — MoviePilot V3 原生插件。仅在用户明确开启后游玩。"""
import base64
import os
import random
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


class PaidRateLimited(RuntimeError):
    """Complete business rejection, not a lost or ambiguous response."""


class Dian115Wheel(_PluginBase):
    plugin_name = "癫影娱乐"
    plugin_desc = "癫影幸运转盘与幸运大富翁：独立开关，依次运行，支持次数、余额限制、记录及通知；游玩消耗积分。"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/dian115entertainment.png"
    plugin_version = "3.1.1"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "dian115wheel_"
    plugin_order = 0
    auth_level = 2
    BASE = "https://m.dian115.com"
    API = "/api/portal"
    WHEEL = API + "/lottery/wheel"
    MONOPOLY = API + "/games/monopoly/play"
    STATUS = API + "/games/status"
    ME = API + "/me"

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._token = ""
        self._cron = "0 10 * * *"
        self._spin_mode = "custom"
        self._wheel_enabled = True
        self._monopoly_enabled = False
        self._maximum_spins = False
        self._max_spins = 1
        self._min_balance = 5
        self._monopoly_max_spins = 1
        self._monopoly_min_balance = 5
        self._monopoly_maximum_spins = False
        self._monopoly_spin_mode = "custom"
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
        # 兼容旧版 spin_mode；新配置以最大次数开关为准。
        self._maximum_spins = bool(config.get("maximum_spins", config.get("spin_mode") == "maximum"))
        self._spin_mode = "maximum" if self._maximum_spins else "custom"
        # 旧 game=monopoly 配置继续仅运行大富翁；缺少新字段的原用户保持转盘开启。
        old_game = config.get("game", "wheel")
        self._wheel_enabled = bool(config.get("wheel_enabled", old_game != "monopoly"))
        self._monopoly_enabled = bool(config.get("monopoly_enabled", old_game == "monopoly"))
        self._max_spins = max(1, min(100, int(config.get("max_spins") if config.get("max_spins") is not None else 1)))
        self._min_balance = max(0, int(config.get("min_balance") if config.get("min_balance") is not None else 5))
        # 旧版共用玩法设置仅在没有大富翁专属字段时沿用，避免改变原有用户行为。
        self._monopoly_maximum_spins = bool(config.get("monopoly_maximum_spins", self._maximum_spins))
        self._monopoly_spin_mode = "maximum" if self._monopoly_maximum_spins else "custom"
        monopoly_spins = config.get("monopoly_max_spins", self._max_spins)
        monopoly_balance = config.get("monopoly_min_balance", self._min_balance)
        self._monopoly_max_spins = max(1, min(100, int(monopoly_spins if monopoly_spins is not None else 1)))
        self._monopoly_min_balance = max(0, int(monopoly_balance if monopoly_balance is not None else 5))
        self._notify = bool(config.get("notify", True))
        self._use_system_proxy = bool(config.get("use_system_proxy", True))
        self._proxy = str(config.get("proxy") or "").strip()
        if config.get("onlyonce"):
            self.update_config({"enabled": self._enabled, "token": self._token, "cron": self._cron,
                                "wheel_enabled": self._wheel_enabled, "monopoly_enabled": self._monopoly_enabled,
                                "spin_mode": self._spin_mode, "maximum_spins": self._maximum_spins,
                                "max_spins": self._max_spins, "min_balance": self._min_balance,
                                "monopoly_maximum_spins": self._monopoly_maximum_spins,
                                "monopoly_max_spins": self._monopoly_max_spins,
                                "monopoly_min_balance": self._monopoly_min_balance,
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
        raw = getattr(settings, "PROXY", None)
        if isinstance(raw, dict):
            proxy = raw.get("https") or raw.get("http")
        else:
            proxy = getattr(raw, "host", None)
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

    @staticmethod
    def _check_bootstrap_response(response, stage):
        # 仅使用响应状态码；异常字符串和响应内容可能包含凭据。
        try:
            response.raise_for_status()
        except Exception:
            status = getattr(response, "status_code", None)
            status = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else "未知"
            raise RuntimeError(f"{stage} 失败：HTTP {status}") from None

    def _ensure_browser_session(self, force=False):
        now = time.time()
        if not force and now < self._session_expiry:
            return
        http = self._http()
        try:
            proof_response = http.get(self.BASE + self.API + "/auth/browser-challenge",
                                      headers=self._headers(), timeout=15)
        except Exception as exc:
            raise RuntimeError(f"browser-challenge 请求失败：{type(exc).__name__}") from None
        self._check_bootstrap_response(proof_response, "browser-challenge")
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
        try:
            response = http.post(self.BASE + self.API + "/auth/browser-session",
                                 json={"public_jwk": jwk, "signature_version": 2}, headers=headers, timeout=15)
        except Exception as exc:
            raise RuntimeError(f"browser-session 请求失败：{type(exc).__name__}") from None
        self._check_bootstrap_response(response, "browser-session")
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
            message = f"站点接口失败：HTTP {response.status_code}" + (f"，代码 {code}" if code and code.isidentifier() else "")
            if method == "POST" and code == "rate_limited":
                raise PaidRateLimited(message)
            raise RuntimeError(message)
        return data

    def _records(self):
        data = self.get_data("wheel_records") or []
        return data if isinstance(data, list) else []

    def _current_points(self):
        """只读站点实时积分；失败时不使用上次抽奖前的余额冒充当前值。"""
        data = self._request("GET", self.ME)
        value = (data.get("user") or {}).get("points")
        if isinstance(value, bool) or value is None:
            raise ValueError("站点未返回有效积分")
        points = int(value)
        if points < 0:
            raise ValueError("站点未返回有效积分")
        return points

    @staticmethod
    def _notification_text(results, balance, error=""):
        prizes = [str(row["label"]) for row in results]
        prize_lines = ["  ｜  ".join(prizes[i:i + 2]) for i in range(0, len(prizes), 2)]
        lines = [f"🎲 本轮抽奖：{len(results)} 次  ｜  消耗积分：{sum(int(r['cost']) for r in results)}",
                 f"💰 当前积分：{balance if balance is not None else '获取失败'}", "", "🎁 奖品结果", *prize_lines]
        if error:
            lines.extend(["", f"⚠️ 提前结束：{error}"])
        return "\n".join(lines)

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
        if not self._wheel_enabled and not self._monopoly_enabled:
            return {"code": 1, "msg": "请先开启幸运转盘或幸运大富翁"}
        if self._wheel_enabled:
            wheel = self._run_wheel()
            if not self._monopoly_enabled or wheel.get("unsafe_stop"):
                return wheel
        else:
            wheel = None
        monopoly = self._run_monopoly()
        if wheel is None:
            return monopoly
        return {"code": 0 if wheel["data"] or monopoly["data"] else 1,
                "msg": f"转盘：{wheel['msg']}；大富翁：{monopoly['msg']}",
                "data": wheel["data"] + monopoly["data"]}

    def _pace_after_paid(self):
        """从持久化的最近消费尝试起节流；只保证单实例顺序执行。"""
        stamp = self.get_data("last_paid_attempt_at")
        if stamp is None:
            return
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
            raise RuntimeError("消费冷却记录无效，已停止以保护积分")
        elapsed = time.time() - stamp
        if elapsed < 0:
            raise RuntimeError("系统时间早于上次消费记录，已停止以保护积分")
        wait = random.uniform(15.0, 25.0) - elapsed
        if wait > 0:
            time.sleep(wait)

    def _mark_paid_attempt(self):
        """POST 前保存时间戳；保存失败时绝不发送消费请求。"""
        stamp = time.time()
        self.save_data("last_paid_attempt_at", stamp)
        if self.get_data("last_paid_attempt_at") != stamp:
            raise RuntimeError("无法保存消费冷却记录，未发送游玩请求")

    def _run_wheel(self):
        results = []
        error = ""
        unsafe_stop = False
        initial_remaining = None
        try:
            if self.get_data("wheel_pending") == datetime.now().date().isoformat():
                raise RuntimeError("先前转盘结果待核对；请在站点核对今日次数和积分，今日不再自动抽奖")
            today = datetime.now().date().isoformat()
            progress = self.get_data("wheel_progress")
            if not isinstance(progress, dict) or progress.get("date") != today:
                progress = None
            # 每次读取站点限额，并用本地已完成次数兜底防止滞后计数。
            for _ in range(self._max_spins if self._spin_mode == "custom" else 100):
                self._pace_after_paid()
                status = self._request("GET", self.STATUS)
                wheel = (status.get("items") or {}).get("daily_wheel") or {}
                if not isinstance(wheel, dict) or any(key not in wheel for key in ("used_today", "max_plays")):
                    raise RuntimeError("站点未返回完整转盘额度，已停止以保护积分")
                used = self._nonnegative_int(wheel["used_today"], "转盘已用次数")
                maximum = self._nonnegative_int(wheel["max_plays"], "转盘每日额度")
                if progress is not None:
                    baseline = self._nonnegative_int(progress.get("baseline"), "转盘本地基准次数")
                    count = self._nonnegative_int(progress.get("count"), "转盘本地已抽次数")
                    used = max(used, baseline + count)
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
                # 请求前写入并读回待核对标记；结果未知时禁止后续轮次再次消费。
                today = datetime.now().date().isoformat()
                self.save_data("wheel_pending", today)
                if self.get_data("wheel_pending") != today:
                    raise RuntimeError("无法保存转盘待核对标记，未发送抽奖请求")
                self._mark_paid_attempt()
                result = self._request("POST", self.WHEEL, retry=False)
                prize = result.get("prize")
                if not isinstance(prize, dict) or not any(key in prize for key in ("label", "points", "vip_days")):
                    raise RuntimeError("抽奖请求已发送但站点未返回可识别奖品；请先在站点核实积分，勿重试")
                row = {"date": datetime.now().isoformat(timespec="seconds"), "cost": int(result.get("cost") if result.get("cost") is not None else cost),
                       "label": str(prize.get("label") or "未知"), "points": int(prize.get("points") or 0),
                       "vip_days": int(prize.get("vip_days") or 0)}
                self.save_data("wheel_records", (self._records() + [row])[-500:])
                if row not in self._records():
                    raise RuntimeError("转盘结果未能持久保存；请在站点核对，勿重试")
                if progress is None:
                    progress = {"date": today, "baseline": used, "count": 0}
                progress = {**progress, "count": self._nonnegative_int(progress["count"], "转盘本地已抽次数") + 1}
                self.save_data("wheel_progress", progress)
                if self.get_data("wheel_progress") != progress:
                    raise RuntimeError("转盘额度记录未能持久保存；请在站点核对，勿重试")
                results.append(row)
                self.save_data("wheel_pending", None)
                if self.get_data("wheel_pending") is not None:
                    raise RuntimeError("转盘待核对标记未清除；请核对站点后再运行")
        except Exception as exc:
            # 不记录请求头/Token/返回体，避免凭据泄露。
            unsafe_stop = True
            error = str(exc)[:200] if isinstance(exc, RuntimeError) else type(exc).__name__
            logger.warning(f"癫影转盘执行中断：{error}")
        if results and self._notify:
            try:
                if "rate_limited" in error:
                    raise RuntimeError("站点限流，本轮不再查询积分")
                self._pace_after_paid()
                balance = self._current_points()
            except Exception:
                balance = None
                logger.warning("癫影转盘通知读取当前积分失败")
            try:
                self.post_message(mtype=NotificationType.Plugin, title="癫影娱乐 · 转盘完成",
                                  text=self._notification_text(results, balance, error))
            except Exception:
                logger.warning("癫影转盘通知发送失败")
        return {"code": 0 if results else 1, "msg": f"完成 {len(results)} 次" + (f"；{error}" if error else ""), "data": results,
                "unsafe_stop": unsafe_stop}

    @staticmethod
    def _nonnegative_int(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise RuntimeError(f"大富翁{name}无效，已停止以保护积分")
        try:
            number = int(value)
        except ValueError:
            raise RuntimeError(f"大富翁{name}无效，已停止以保护积分") from None
        if number < 0:
            raise RuntimeError(f"大富翁{name}无效，已停止以保护积分")
        return number

    @staticmethod
    def _award_int(value):
        # 大富翁格子可能是扣分（站点账本用有符号奖励）；仅额度/成本必须非负。
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise RuntimeError("大富翁奖励积分无效；请在站点核对，勿重试")
        try:
            return int(value)
        except ValueError:
            raise RuntimeError("大富翁奖励积分无效；请在站点核对，勿重试") from None

    def _monopoly_pending(self):
        pending = self.get_data("monopoly_pending")
        today = datetime.now().date().isoformat()
        return pending == today or (isinstance(pending, dict) and pending.get("date") == today)

    def _auto_reconcile_monopoly_pending(self):
        """旧标记先读站点额度和积分；以站点次数接管后续预检，不重发原请求。"""
        today = datetime.now().date().isoformat()
        pending = self.get_data("monopoly_pending")
        if pending is None:
            return True
        if pending != today and (not isinstance(pending, dict) or pending.get("date") != today):
            return False
        if isinstance(pending, dict) and pending.get("persistence_failed"):
            return False
        progress = self.get_data("monopoly_progress")
        try:
            expected = None
            if isinstance(pending, dict) and pending.get("expected_used") is not None:
                expected = self._nonnegative_int(pending["expected_used"], "请求前已用次数")
            if progress is not None:
                if not isinstance(progress, dict) or progress.get("date") != today:
                    return False
                baseline = self._nonnegative_int(progress.get("baseline"), "本地基准次数")
                count = self._nonnegative_int(progress.get("count"), "本地已玩次数")
                local_used = baseline + count
                if expected is not None and expected != local_used:
                    return False
                expected = local_used
            self._pace_after_paid()
            status = self._request("GET", self.STATUS)
            items = status.get("items")
            game = items.get("monopoly") if isinstance(items, dict) else None
            if not isinstance(game, dict):
                return False
            used = self._nonnegative_int(game.get("used_today"), "已用次数")
            self._nonnegative_int(game.get("max_plays"), "每日额度")
            self._nonnegative_int(game.get("cost"), "单次消耗")
            if expected is not None and used < expected:
                return False
            # 余额只读核对；本轮新的消费仍由下面的逐次预检决定。
            self._current_points()
            if progress is not None and expected is not None and used > expected:
                updated = {**progress, "baseline": used - count}
                self.save_data("monopoly_progress", updated)
                if self.get_data("monopoly_progress") != updated:
                    return False
            # 达到额度也可清理旧标记，下面的站点额度预检会正常结束本轮。
            self.save_data("monopoly_pending", None)
            return self.get_data("monopoly_pending") is None
        except Exception:
            return False

    def _run_monopoly(self):
        """独立玩法；只信任站点状态的额度和成本，不重发不确定的消费请求。"""
        if not self._token:
            return {"code": 1, "msg": "请先填写登录 Token"}
        results, error, initial_remaining = [], "", None
        try:
            if self._monopoly_pending():
                if not self._auto_reconcile_monopoly_pending():
                    raise RuntimeError("站点额度或积分读取失败、次数倒退或记录无法保存；本轮不能安全预检")
            today = datetime.now().date().isoformat()
            progress = self.get_data("monopoly_progress")
            if not isinstance(progress, dict) or progress.get("date") != today:
                progress = None
            for _ in range(self._monopoly_max_spins if self._monopoly_spin_mode == "custom" else 100):
                if self._monopoly_pending():
                    raise RuntimeError("大富翁结果仍待核对；今日不再自动游玩")
                self._pace_after_paid()
                status = self._request("GET", self.STATUS)
                items = status.get("items")
                game = items.get("monopoly") if isinstance(items, dict) else None
                if not isinstance(game, dict):
                    raise RuntimeError("站点未返回大富翁状态，已停止以保护积分")
                used = self._nonnegative_int(game.get("used_today"), "已用次数")
                maximum = self._nonnegative_int(game.get("max_plays"), "每日额度")
                cost = self._nonnegative_int(game.get("cost"), "单次消耗")
                if progress is not None:
                    baseline = self._nonnegative_int(progress.get("baseline"), "本地基准次数")
                    count = self._nonnegative_int(progress.get("count"), "本地已玩次数")
                    used = max(used, baseline + count)
                if initial_remaining is None:
                    initial_remaining = max(0, maximum - used)
                if len(results) >= initial_remaining:
                    break
                if used >= maximum:
                    error = "今日大富翁次数已用完或站点未开放"
                    break
                points = self._current_points()
                if points - cost < self._monopoly_min_balance:
                    error = f"积分不足：余额 {points}，单次消耗 {cost}，需保留 {self._monopoly_min_balance}"
                    break
                # POST 前落盘；若请求结果未知，重启后也不可再次消费。
                today = datetime.now().date().isoformat()
                pending = {"date": today, "expected_used": used}
                self.save_data("monopoly_pending", pending)
                if self.get_data("monopoly_pending") != pending:
                    raise RuntimeError("无法保存大富翁待核对标记，未发送游玩请求")
                self._mark_paid_attempt()
                try:
                    result = self._request("POST", self.MONOPOLY, retry=False)
                except PaidRateLimited:
                    # Only a parsed, complete rate_limited response is a definite rejection.
                    rejected = {**pending, "rejected": "rate_limited"}
                    self.save_data("monopoly_pending", rejected)
                    # If persistence fails, leave the original ambiguous marker in place.
                    raise
                award = self._award_int(result.get("award_points"))
                steps = result.get("steps")
                if not isinstance(steps, list) or not steps:
                    raise RuntimeError("大富翁请求已发送但结果不明确；请在站点核对，勿重试")
                # 站点前端用 award_points 和 steps 展示结果；不把前端默认格子当成服务端结果。
                row = {"date": datetime.now().isoformat(timespec="seconds"), "game": "monopoly",
                       "cost": cost, "label": f"大富翁奖励 {award} 积分", "points": award, "vip_days": 0}
                self.save_data("wheel_records", (self._records() + [row])[-500:])
                if row not in self._records():
                    self.save_data("monopoly_pending", {**pending, "persistence_failed": True})
                    raise RuntimeError("大富翁结果未能持久保存；请在站点核对，勿重试")
                if progress is None:
                    progress = {"date": today, "baseline": used, "count": 0}
                progress = {**progress, "count": self._nonnegative_int(progress["count"], "本地已玩次数") + 1}
                self.save_data("monopoly_progress", progress)
                if self.get_data("monopoly_progress") != progress:
                    self.save_data("monopoly_pending", {**pending, "persistence_failed": True})
                    raise RuntimeError("大富翁额度记录未能持久保存；请在站点核对，勿重试")
                results.append(row)
                self.save_data("monopoly_pending", None)
                if self.get_data("monopoly_pending") is not None:
                    raise RuntimeError("大富翁记录已保存但待核对标记未清除；请核对站点后再运行")
        except Exception as exc:
            error = str(exc)[:200] if isinstance(exc, RuntimeError) else type(exc).__name__
            logger.warning(f"癫影大富翁执行中断：{error}")
        if results and self._notify:
            try:
                if "rate_limited" in error:
                    raise RuntimeError("站点限流，本轮不再查询积分")
                self._pace_after_paid()
                balance = self._current_points()
            except Exception:
                balance = None
            try:
                self.post_message(mtype=NotificationType.Plugin, title="癫影娱乐 · 大富翁完成",
                                  text=self._notification_text(results, balance, error).replace("本轮抽奖", "本轮游玩"))
            except Exception:
                logger.warning("癫影大富翁通知发送失败")
        return {"code": 0 if results else 1, "msg": f"完成 {len(results)} 次" + (f"；{error}" if error else ""), "data": results}

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._token:
            return []
        return [{"id": "Dian115Wheel", "name": "癫影娱乐", "trigger": CronTrigger.from_crontab(self._cron),
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
            field("VSwitch", "wheel_enabled", "运行幸运大转盘", hint="默认开启；与大富翁同时开启时先执行转盘", persistent_hint=True),
            field("VTextField", "max_spins", "转盘 · 自定义次数", type="number", min="1", max="100", hint="转盘最大次数关闭时，每轮按此次数抽奖（默认 1 次）", persistent_hint=True),
            field("VSwitch", "maximum_spins", "转盘 · 最大次数", hint="按转盘今日剩余额度抽奖，忽略转盘自定义次数；仍受转盘积分保留限制", persistent_hint=True),
            field("VTextField", "min_balance", "转盘 · 至少保留积分", type="number", min="0"),
            field("VSwitch", "monopoly_enabled", "运行幸运大富翁", hint="默认关闭；与转盘同时开启时排在转盘之后", persistent_hint=True),
            field("VTextField", "monopoly_max_spins", "大富翁 · 自定义次数", type="number", min="1", max="100", hint="大富翁最大次数关闭时，每轮按此次数游玩（默认 1 次）", persistent_hint=True),
            field("VSwitch", "monopoly_maximum_spins", "大富翁 · 最大次数", hint="按大富翁今日剩余额度游玩，忽略大富翁自定义次数；仍受大富翁积分保留限制", persistent_hint=True),
            field("VTextField", "monopoly_min_balance", "大富翁 · 至少保留积分", type="number", min="0"),
            field("VSwitch", "notify", "中奖结果通知"),
            field("VSwitch", "use_system_proxy", "使用系统代理（默认开启）"),
            field("VTextField", "proxy", "自定义代理（关闭系统代理时生效）"),
            field("VSwitch", "onlyonce", "保存后立即运行一次（消耗积分）"),
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "两种玩法均会消耗积分；分别开启时按转盘→大富翁执行，转盘结果不明则停止，避免继续扣分；最大次数按各玩法当日剩余额度执行，逐次检查积分与限额。"}},
        ]}]
        return form, {"enabled": False, "token": "", "cron": "0 10 * * *", "wheel_enabled": True, "monopoly_enabled": False, "maximum_spins": False, "max_spins": 1,
                      "min_balance": 5, "monopoly_maximum_spins": False, "monopoly_max_spins": 1, "monopoly_min_balance": 5,
                      "notify": True, "use_system_proxy": True, "proxy": "", "onlyonce": False}

    def get_page(self):
        all_records = self._records()
        records = list(reversed(all_records[-20:]))
        try:
            current_points = self._current_points() if self._token else None
        except Exception:
            current_points = None
            logger.warning("癫影转盘首页读取当前积分失败")
        def item(label, value, balance=False):
            return {"component": "VCol", "props": {"cols": 12 if balance else 6, "md": 12 if balance else 3}, "content": [
                {"component": "VCard", "props": {"variant": "tonal", **({"color": "info"} if balance else {})}, "content": [
                    {"component": "VCardText", "props": {"class": "text-center"}, "content": [
                        {"component": "div", "props": {"style": "font-size:clamp(1rem,4vw,1.5rem);overflow-wrap:anywhere;"}, "text": str(value)},
                        {"component": "div", "props": {"class": "text-caption"}, "text": label}]}]}]}
        total_cost = sum(int(r.get("cost") or 0) for r in all_records)
        total_points = sum(int(r.get("points") or 0) for r in all_records)
        cards = [item("已记录抽奖", len(all_records)), item("消耗积分", total_cost),
                 item("奖励积分", total_points), item("净积分", total_points - total_cost)]
        balance_card = item("当前积分（实时）", current_points if current_points is not None else "获取失败", balance=True)
        rows = [{"component": "tr", "content": [{"component": "td", "text": str(r.get(k) if r.get(k) is not None else "—")}
                for k in ("date", "label", "cost", "points", "vip_days")]} for r in records]
        return [{"component": "VRow", "content": [balance_card]},
                {"component": "VRow", "content": cards}, {"component": "VCard", "props": {"class": "mt-3"}, "content": [
            {"component": "VCardTitle", "text": "最近游玩记录（以本地记录为准）"},
            {"component": "VCardText", "props": {"style": "overflow-x:auto"}, "content": [
                {"component": "VTable", "props": {"style": "min-width:540px"}, "content": [
                    {"component": "thead", "content": [{"component": "tr", "content":
                        [{"component": "th", "text": t} for t in ("时间", "奖品", "消耗", "积分奖励", "VIP 天数")]}]},
                    {"component": "tbody", "content": rows}]}]}]}]
