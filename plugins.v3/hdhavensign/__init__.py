"""栖影 HDHaven MoviePilot V3 自动签到。使用用户在浏览器取得的站点会话，不处理人机验证。"""
import os
import threading
import json
from datetime import datetime
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.schemas.types import NotificationType
from apscheduler.triggers.cron import CronTrigger

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


class _BrowserlessResponse:
    def __init__(self, payload):
        if not isinstance(payload, dict):
            raise RuntimeError("Browserless 响应结构异常，未确认站点结果")
        self.status_code = payload.get("status")
        if type(self.status_code) is not int:
            raise RuntimeError("Browserless 未返回站点 HTTP 状态，未确认站点结果")
        self.headers = {"content-type": payload.get("contentType") or ""}
        self.history = []
        self._body = payload.get("body")

    def json(self):
        if not isinstance(self._body, str):
            raise ValueError("Browserless 响应体缺失")
        return json.loads(self._body)


class _BrowserlessSession:
    def __init__(self, endpoint, cookie, proxy=""):
        if curl_requests is None:
            raise RuntimeError("缺少 curl_cffi 依赖")
        try:
            url = urlsplit(endpoint)
            valid_token = any(parse_qs(url.query).get("token", []))
            hostname = url.hostname
        except ValueError:
            raise ValueError("Browserless 地址格式错误") from None
        if (url.scheme != "https" or not hostname or url.username or url.password or
                url.fragment or url.path.rstrip("/") != "/function" or not valid_token):
            raise ValueError("Browserless 地址须为带 token 的 HTTPS /function 接口")
        query = [(key, value) for key, value in parse_qsl(url.query, keep_blank_values=True)
                 if key != "timeout"]
        # The server's TIMEOUT upper bound may still be lower (notably on self-hosted instances).
        query.append(("timeout", "45000"))
        self._endpoint = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
        self._client = curl_requests.Session(impersonate="chrome110")
        if proxy:
            self._client.proxies = {"http": proxy, "https": proxy}
        self._cookies = {name: item.value for name, item in self._cookie_jar(cookie).items()}

    @staticmethod
    def _cookie_jar(cookie):
        jar = SimpleCookie()
        jar.load(cookie)
        return jar

    def _request(self, method, url, timeout):
        # Only allow the two site endpoints used by the existing sign-in flow.
        if (method, url) not in (("GET", HDHavenSign.BASE + "/api/auth/me"),
                                 ("POST", HDHavenSign.BASE + "/api/account/checkin")):
            raise ValueError("Browserless 请求目标不受支持")
        script = """export default async ({ page, context }) => {
  await page.setCookie(...context.cookies);
  await page.goto('https://hdhaven.com/me', {waitUntil: 'domcontentloaded', timeout: 20000});
  const result = await page.evaluate(async ({method, url}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {method, credentials: 'include', signal: controller.signal});
      return {status: response.status, contentType: response.headers.get('content-type') || '',
              body: await response.text()};
    } finally {
      clearTimeout(timer);
    }
  }, context.request);
  return {data: result, type: 'application/json'};
}"""
        cookies = [{"name": name, "value": value, "domain": "hdhaven.com", "path": "/",
                    "secure": True, "httpOnly": name == "hdh_session"}
                   for name, value in self._cookies.items()]
        response = self._client.post(self._endpoint,
                                     json={"code": script, "context": {"cookies": cookies,
                                                                         "request": {"method": method, "url": url}}},
                                     timeout=55)
        if response.status_code != 200:
            raise RuntimeError("Browserless 执行失败，站点请求结果未确认")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise RuntimeError("Browserless 响应无法解析，站点请求结果未确认") from None
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        return _BrowserlessResponse(payload)

    def get(self, url, timeout=20):
        return self._request("GET", url, timeout)

    def post(self, url, timeout=20):
        return self._request("POST", url, timeout)

    def close(self):
        self._client.close()


class HDHavenSign(_PluginBase):
    plugin_name = "栖影自动签到"
    plugin_desc = "HDHaven 普通签到、定时执行及结果通知"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/hdhavensign.png"
    plugin_version = "3.0.2"
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
        self._use_browserless = False
        self._browserless_url = ""
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
        self._use_browserless = bool(config.get("use_browserless", False))
        self._browserless_url = str(config.get("browserless_url") or "").strip()
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
        if self._use_browserless:
            if not self._browserless_url:
                raise ValueError("已启用 Browserless，请填写 /function 接口地址")
            cookie = self._parse_cookie(self._cookie)
            proxy = self._proxy_from_settings() if self._use_system_proxy else self._proxy
            logger.info("栖影签到网络路径：Browserless")
            return _BrowserlessSession(self._browserless_url, cookie, proxy)
        if curl_requests is None:
            raise RuntimeError("缺少 curl_cffi 依赖")
        cookie = self._parse_cookie(self._cookie)
        session = curl_requests.Session(impersonate="chrome110")
        proxy = self._proxy_from_settings() if self._use_system_proxy else self._proxy
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
            logger.info("栖影签到网络路径：%s代理", "系统" if self._use_system_proxy else "自定义")
        elif self._use_system_proxy:
            logger.warning("栖影签到已开启系统代理，但未读取到代理配置，将直连")
        else:
            logger.info("栖影签到网络路径：直连（系统代理已关闭）")
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
        # 只有明确确认成功的响应才记为成功；下次运行需先读取站点状态再决定是否提交。
        raise RuntimeError("签到响应未确认，请在站点核对；再次运行会先查询站点今日签到状态")

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
        stage = "准备会话"
        try:
            if not self._cookie:
                raise RuntimeError("未配置栖影登录会话 Cookie")
            today = datetime.now().date().isoformat()
            session = self._session()
            # 先查询站点签到状态；本地待核对记录不能代替站点结果。
            stage = "登录预检"
            logger.info("栖影签到登录预检：开始只读请求")
            me = session.get(self.BASE + "/api/auth/me", timeout=20)
            content_type = (me.headers.get("content-type") or "").lower()
            response_type = "JSON" if "json" in content_type else "非JSON"
            logger.info("栖影签到登录预检：HTTP %s，响应类型=%s，发生跳转=%s",
                        me.status_code, response_type, bool(getattr(me, "history", None)))
            account = self._decode(me)
            has_account = isinstance(account.get("data"), dict) and bool(account["data"].get("id"))
            logger.info("栖影签到登录预检：success=%s，账户标识存在=%s",
                        account.get("success") is True, has_account)
            if me.status_code != 200 or account.get("success") is not True or not has_account:
                raise RuntimeError("会话未通过登录预检，请重新登录并更新 Cookie")
            logger.info("栖影签到登录预检：通过")
            status = account["data"].get("checkin_today")
            last_checkin = account["data"].get("last_checkin")
            if type(status) is not bool or (last_checkin is not None and
                                            (not isinstance(last_checkin, str) or
                                             (status and last_checkin != today) or
                                             (not status and last_checkin == today))):
                raise RuntimeError("站点今日签到状态无法确认，未发送签到请求")
            if status:
                self.save_data("last_result", {"date": today, "state": "success"})
                saved = self.get_data("last_result") or {}
                if saved.get("date") != today or saved.get("state") != "success":
                    raise RuntimeError("站点确认今日已签到，但本地记录保存失败")
                logger.info("栖影签到：站点确认今日已签到，跳过提交")
                return {"success": True, "message": "站点确认今日已签到，不重复提交"}
            logger.info("栖影签到：站点确认今日未签到，准备提交")
            # 持久化并读回 pending，防止 POST 已送达而本地无法记录时再次扣账/重复签到。
            self.save_data("last_result", {"date": today, "state": "pending"})
            marker = self.get_data("last_result") or {}
            if marker.get("date") != today or marker.get("state") != "pending":
                raise RuntimeError("待核对标记保存失败，未发送签到请求")
            # 浏览器 Network：POST /api/account/checkin，Content-Length: 0；不要传 json={}。
            stage = "签到请求"
            logger.info("栖影签到：预检通过且待核对标记已保存，准备提交签到")
            response = session.post(self.BASE + "/api/account/checkin", timeout=20)
            logger.info("栖影签到响应：HTTP %s，响应类型=%s", response.status_code,
                        "JSON" if "json" in (response.headers.get("content-type") or "").lower() else "非JSON")
            result = self._interpret(response)
            stage = "结果保存"
            self.save_data("last_result", {"date": today, **result})
            saved = self.get_data("last_result") or {}
            if saved.get("date") != today or saved.get("state") != "success":
                raise RuntimeError("签到已返回成功，但记录保存失败，请人工核对")
            self._notice("栖影签到成功", f"获得积分：{result['earned']}；当前积分：{result['points']}；连续签到：{result['streak']}")
            return {"success": True, "message": "签到成功", "data": result}
        except Exception as exc:
            # 不输出网络异常原文：可能含 Cookie/代理地址等敏感字段。
            logger.warning("栖影签到未完成：阶段=%s，异常类型=%s", stage, type(exc).__name__)
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
            {"component": "VCard", "props": {"class": "mt-4 mb-3", "variant": "outlined"}, "content": [
                {"component": "VCardTitle", "text": "Browserless 备用模式（可选）"},
                {"component": "VCardText", "props": {"class": "pt-2"}, "content": [
                    field("VSwitch", "use_browserless", "启用 Browserless（默认关闭）",
                          hint="关闭时沿用现有签到路径；开启后本次请求使用 Browserless，不会在失败后自动切换或重发签到。",
                          **{"persistent-hint": True}),
                    field("VTextField", "browserless_url", "Browserless HTTPS /function 地址", type="password",
                          hint="填写完整地址（含 ?token=...）；仅开启时使用，站点 Cookie 将发送给该服务，请仅使用可信服务。",
                          **{"persistent-hint": True}),
                ]},
            ]},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                      "text": "登录需浏览器完成人机验证；每次先查询站点签到状态，仅明确未签到才提交请求。"}},
        ]}], {"enabled": False, "onlyonce": False, "notify": True, "cookie": "",
              "cron": "30 9 * * *", "use_system_proxy": True, "proxy": "",
              "use_browserless": False, "browserless_url": ""})

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
