"""栖影 HDHaven MoviePilot V3 自动签到。使用用户在浏览器取得的站点会话，不处理人机验证。"""
import os
import threading
from datetime import datetime
from http.cookies import SimpleCookie

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.schemas.types import NotificationType
from apscheduler.triggers.cron import CronTrigger

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


class HDHavenSign(_PluginBase):
    plugin_name = "栖影自动签到"
    plugin_desc = "HDHaven 普通签到、定时执行及结果通知"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/hdhavensign.png"
    plugin_version = "3.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "hdhavensign_"
    plugin_order = 0
    auth_level = 2
    BASE = "https://hdhaven.com"

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._cookie = ""
        self._cron = "30 9 * * *"
        self._notify = True
        self._use_system_proxy = True
        self._proxy = ""
        self._lock = threading.Lock()
        self._timer = None

    def init_plugin(self, config=None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._cookie = str(config.get("cookie") or "").strip()
        self._cron = str(config.get("cron") or "30 9 * * *").strip()
        self._notify = bool(config.get("notify", True))
        self._use_system_proxy = bool(config.get("use_system_proxy", True))
        self._proxy = str(config.get("proxy") or "").strip()
        if config.get("onlyonce"):
            # 保存配置时清除一次性开关，避免下次宿主重启再触发。
            updated = dict(config)
            updated["onlyonce"] = False
            try:
                self.update_config(updated)
            except Exception:
                logger.warning("栖影一次性开关持久化失败，未执行签到")
                return
            self._timer = threading.Timer(3, self._run, kwargs={"manual": True})
            self._timer.daemon = True
            self._timer.start()

    @staticmethod
    def _proxy_from_settings():
        raw = getattr(settings, "PROXY", None)
        if isinstance(raw, dict):
            value = raw.get("https") or raw.get("http")
        else:
            value = getattr(raw, "host", None)
        return str(value or os.environ.get("PROXY_HOST") or os.environ.get("https_proxy") or
                   os.environ.get("HTTPS_PROXY") or "").strip()

    @staticmethod
    def _parse_cookie(value):
        raw = value.strip()
        if raw.lower().startswith("cookie:"):
            raw = raw[7:].strip()
        if not raw or any(c in raw for c in "\r\n"):
            raise ValueError("请填写站点登录会话 Cookie")
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            raise ValueError("Cookie 格式错误") from None
        if not jar or any(not part.strip() or "=" not in part for part in raw.split(";")):
            raise ValueError("Cookie 格式错误")
        if "hdh_session" not in jar or not jar["hdh_session"].value:
            raise ValueError("缺少栖影登录会话 Cookie")
        return "; ".join(f"{name}={item.value}" for name, item in jar.items())

    def _session(self):
        if curl_requests is None:
            raise RuntimeError("缺少 curl_cffi 依赖")
        cookie = self._parse_cookie(self._cookie)
        session = curl_requests.Session(impersonate="chrome110")
        proxy = self._proxy_from_settings() if self._use_system_proxy else self._proxy
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        session.headers.update({
            "Cookie": cookie, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.BASE + "/me", "Origin": self.BASE,
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        })
        return session

    @staticmethod
    def _decode(response):
        if "json" not in (response.headers.get("content-type") or "").lower():
            raise RuntimeError("站点返回非 JSON，可能遭遇人机验证；请人工核对")
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise RuntimeError("站点 JSON 响应无法解析；请人工核对") from None
        if not isinstance(data, dict):
            raise RuntimeError("站点响应结构异常；请人工核对")
        return data

    @classmethod
    def _interpret(cls, response):
        data = cls._decode(response)
        if response.status_code == 201 and data.get("success") is True:
            result = data.get("data")
            if not isinstance(result, dict) or result.get("mode") != "normal":
                raise RuntimeError("签到成功响应结构异常；请人工核对")
            if any(not isinstance(result.get(k), int) for k in ("earned", "points", "checkin_streak")):
                raise RuntimeError("签到结果数字字段缺失；请人工核对")
            return {"state": "success", "earned": result["earned"],
                    "points": result["points"], "streak": result["checkin_streak"]}
        # 尚未获得站点“已签到”响应证据，所有非成功响应均视为结果不确定，不自动重试。
        raise RuntimeError("签到响应未确认，请在站点核对；勿自动重试")

    def _notice(self, title, text):
        if self._notify:
            try:
                self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
            except Exception:
                logger.warning("栖影签到通知发送失败")

    def _run(self, manual=False):
        if not manual and not self._enabled:
            return {"success": False, "message": "定时签到未启用"}
        if not self._lock.acquire(blocking=False):
            return {"success": False, "message": "签到正在运行"}
        session = None
        try:
            if not self._cookie:
                raise RuntimeError("未配置栖影登录会话 Cookie")
            today = datetime.now().date().isoformat()
            previous = self.get_data("last_result") or {}
            if previous.get("date") == today and previous.get("state") in ("success", "pending"):
                return {"success": previous["state"] == "success", "message": "今日已执行或结果待核对，不重复提交"}
            session = self._session()
            # 仅用已知的只读账户接口预检登录状态。
            me = session.get(self.BASE + "/api/auth/me", timeout=20)
            account = self._decode(me)
            if me.status_code != 200 or account.get("success") is not True or not isinstance(account.get("data"), dict) or not account["data"].get("id"):
                raise RuntimeError("会话未通过登录预检，请重新登录并更新 Cookie")
            # 持久化并读回 pending，防止 POST 已送达而本地无法记录时再次扣账/重复签到。
            self.save_data("last_result", {"date": today, "state": "pending"})
            marker = self.get_data("last_result") or {}
            if marker.get("date") != today or marker.get("state") != "pending":
                raise RuntimeError("待核对标记保存失败，未发送签到请求")
            # 浏览器 Network：POST /api/account/checkin，Content-Length: 0；不要传 json={}。
            response = session.post(self.BASE + "/api/account/checkin", timeout=20)
            result = self._interpret(response)
            self.save_data("last_result", {"date": today, **result})
            saved = self.get_data("last_result") or {}
            if saved.get("date") != today or saved.get("state") != "success":
                raise RuntimeError("签到已返回成功，但记录保存失败，请人工核对")
            self._notice("栖影签到成功", f"获得积分：{result['earned']}；当前积分：{result['points']}；连续签到：{result['streak']}")
            return {"success": True, "message": "签到成功", "data": result}
        except Exception as exc:
            # 不输出网络异常原文：可能含 Cookie/代理地址等敏感字段。
            logger.warning("栖影签到未完成：%s", type(exc).__name__)
            message = str(exc) if type(exc) in (RuntimeError, ValueError) else "请求或存储异常，结果可能已提交；请人工核对，勿重试"
            self._notice("栖影签到待核对", message)
            return {"success": False, "message": message}
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            self._lock.release()

    def get_state(self):
        return self._enabled and bool(self._cookie)

    def get_service(self):
        if not self.get_state():
            return []
        return [{"id": "HDHavenSign", "name": "栖影自动签到",
                 "trigger": CronTrigger.from_crontab(self._cron), "func": self._run, "kwargs": {}}]

    def get_command(self):
        return []

    def get_api(self):
        return []

    def get_form(self):
        def field(component, model, label, **props):
            return {"component": component, "props": {"model": model, "label": label, **props}}
        return ([{"component": "VForm", "content": [
            field("VSwitch", "enabled", "启用定时签到"),
            field("VSwitch", "onlyonce", "保存后执行一次签到"),
            field("VSwitch", "notify", "发送结果通知"),
            field("VTextField", "cookie", "栖影登录会话 Cookie", type="password",
                  hint="仅在 MoviePilot 配置中填写浏览器登录后的 Cookie；失效后需手动更新", **{"persistent-hint": True}),
            field("VTextField", "cron", "Cron（默认每天 09:30）"),
            field("VSwitch", "use_system_proxy", "使用系统代理（默认开启）"),
            field("VTextField", "proxy", "自定义代理（关闭系统代理时使用）"),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                      "text": "登录需浏览器完成人机验证；遇到未知签到结果不会自动重试，请登录站点核对。"}},
        ]}], {"enabled": False, "onlyonce": False, "notify": True, "cookie": "",
              "cron": "30 9 * * *", "use_system_proxy": True, "proxy": ""})

    def get_page(self):
        record = self.get_data("last_result") or {}
        state = {"success": "签到成功", "pending": "结果待核对"}.get(str(record.get("state") or ""), "尚无记录")
        return [{"component": "VCard", "props": {"class": "ma-2"}, "content": [
            {"component": "VCardTitle", "text": "栖影自动签到"},
            {"component": "VCardText", "text": f"最近状态：{state}；日期：{record.get('date', '-')}"},
        ]}]

    def stop_service(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
