"""栖影 HDHaven MoviePilot V3 自动签到。使用用户在浏览器取得的站点会话，不处理人机验证。"""
import json
import os
import threading
import uuid
from datetime import datetime
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas.types import MessageType
from apscheduler.triggers.cron import CronTrigger

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


class _RunConfig:
    """一次签到运行所需的配置快照；整体替换、只读使用，避免重载期间字段错配。"""

    __slots__ = ("cookie", "use_system_proxy", "proxy", "use_browserless", "browserless_url")

    def __init__(self, cookie="", use_system_proxy=True, proxy="",
                 use_browserless=False, browserless_url=""):
        self.cookie = cookie
        self.use_system_proxy = use_system_proxy
        self.proxy = proxy
        self.use_browserless = use_browserless
        self.browserless_url = browserless_url


class _RunContext:
    """同代运行上下文：配置快照、取消标志与代际号绑定为一个不可变对象，整体替换。"""

    __slots__ = ("generation", "config", "cancel")

    def __init__(self, generation, config, cancel):
        self.generation = generation
        self.config = config
        self.cancel = cancel


class _DateRollover(Exception):
    """运行期间宿主日期已跨日；中止本次提交并按新日期重启当天流程。"""


# 请求体里的 context 是普通 JSON，没有 cookies() 方法；Cookie 导出必须走远端运行时 API：
# Puppeteer 为 page.cookies(...)，Playwright 为 page.context().cookies(...)，双路径都尝试。
_BROWSERLESS_SCRIPT = """export default async ({ page, context }) => {
  if (typeof page.setCookie === 'function') {
    await page.setCookie(...context.cookies);
  } else {
    await page.context().addCookies(context.cookies);
  }
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
  let cookies = [];
  try {
    if (typeof page.cookies === 'function') {
      cookies = await page.cookies('https://hdhaven.com');
    } else {
      cookies = await page.context().cookies('https://hdhaven.com');
    }
  } catch (e) {}
  return {data: result, type: 'application/json', cookies: cookies};
}"""


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
    """Browserless REST 每次调用都是独立浏览器；这里通过回传 Cookie 保持会话连续。"""

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
        self._client = curl_requests.Session(impersonate="chrome110", trust_env=False)
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
        script = _BROWSERLESS_SCRIPT
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
            outer = response.json()
        except (ValueError, TypeError):
            raise RuntimeError("Browserless 响应无法解析，站点请求结果未确认") from None
        if not isinstance(outer, dict):
            raise RuntimeError("Browserless 响应无法解析，站点请求结果未确认")
        payload = outer.get("data") if "data" in outer else outer
        remote_cookies = outer.get("cookies")
        if isinstance(remote_cookies, list):
            for item in remote_cookies:
                if (isinstance(item, dict) and isinstance(item.get("name"), str)
                        and isinstance(item.get("value"), str) and item["name"]):
                    self._cookies[item["name"]] = item["value"]
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
    plugin_version = "3.0.5"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "hdhavensign_"
    plugin_order = 0
    auth_level = 2
    BASE = "https://hdhaven.com"
    SITE_HOST = "hdhaven.com"
    DEFAULT_CRON = "30 9 * * *"
    # stop_service 等待在途任务收敛的上限；超时按宿主约定明确报告未收敛。
    STOP_WAIT_SECONDS = 10.0
    _SAFE_ERRORS = frozenset({
        "缺少 curl_cffi 依赖", "Browserless 地址格式错误",
        "Browserless 地址须为带 token 的 HTTPS /function 接口",
        "Browserless 请求目标不受支持", "Browserless 执行失败，站点请求结果未确认",
        "Browserless 响应无法解析，站点请求结果未确认",
        "Browserless 响应结构异常，未确认站点结果",
        "Browserless 未返回站点 HTTP 状态，未确认站点结果",
        "Browserless 响应体缺失", "请填写站点登录会话 Cookie",
        "Cookie 格式错误", "缺少栖影登录会话 Cookie",
        "已启用 Browserless，请填写 /function 接口地址",
        "站点返回非 JSON，可能遭遇人机验证；请人工核对",
        "站点 JSON 响应无法解析；请人工核对",
        "站点响应结构异常；请人工核对",
        "签到成功响应结构异常；请人工核对",
        "签到结果数字字段缺失；请人工核对",
        "签到响应未确认，请在站点核对；再次运行会先查询站点今日签到状态",
        "未配置栖影登录会话 Cookie", "会话未通过登录预检，请重新登录并更新 Cookie",
        "站点检测到网络环境变化：请在与插件相同的代理节点下重新登录并完成人机验证，再更新 Cookie",
        "站点今日签到状态无法确认，未发送签到请求",
        "站点确认今日已签到，但本地记录保存失败",
        "待核对标记保存失败，未发送签到请求",
        "宿主日期连续跨日，本次未提交签到",
        "配置已变更或插件已停止，签到未提交",
    })

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._notify = True
        self._cron = self.DEFAULT_CRON
        self._generation = 0
        self._context = _RunContext(self._generation, _RunConfig(), threading.Event())
        self._commit_lock = threading.Lock()
        self._lock = threading.Lock()
        self._timers = []

    @staticmethod
    def _timezone_name():
        """统一时区来源：MoviePilot 配置的 TZ；无效时回退系统本地时区。"""
        name = str(getattr(settings, "TZ", "") or "Asia/Shanghai").strip()
        try:
            ZoneInfo(name)
        except Exception:
            return None
        return name

    def _now(self):
        name = self._timezone_name()
        if name:
            try:
                return datetime.now(ZoneInfo(name))
            except Exception:
                pass
        return datetime.now().astimezone()

    def _cron_valid(self, expr):
        try:
            CronTrigger.from_crontab(expr, timezone=self._timezone_name())
            return True
        except (ValueError, TypeError):
            return False

    def _revoke_pending_runs(self):
        """置旧代取消标志并取消待触发 Timer；与提交边界互斥，避免中止判定与 POST 交错。"""
        acquired = self._commit_lock.acquire(timeout=self.STOP_WAIT_SECONDS)
        if not acquired:
            logger.warning("栖影签到等待提交边界超时，在途任务可能仍在提交；将继续换代并保留其结果")
        try:
            event = self._context.cancel
            if event is not None:
                event.set()
            timers, self._timers = self._timers, []
            for timer in timers:
                timer.cancel()
        finally:
            if acquired:
                self._commit_lock.release()

    def init_plugin(self, config=None):
        config = config or {}
        self._revoke_pending_runs()
        snapshot = _RunConfig(
            cookie=str(config.get("cookie") or "").strip(),
            use_system_proxy=bool(config.get("use_system_proxy", True)),
            proxy=str(config.get("proxy") or "").strip(),
            use_browserless=bool(config.get("use_browserless", False)),
            browserless_url=str(config.get("browserless_url") or "").strip(),
        )
        self._generation += 1
        # 单属性整体替换：配置快照、取消标志与代际号始终同代。
        self._context = _RunContext(self._generation, snapshot, threading.Event())
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", True))
        raw_cron = str(config.get("cron") or self.DEFAULT_CRON).strip()
        if self._cron_valid(raw_cron):
            self._cron = raw_cron
        else:
            logger.warning("栖影签到 Cron 非法，已回退默认值；请检查格式（分 时 日 月 周）")
            self._cron = self.DEFAULT_CRON
        if config.get("onlyonce"):
            # 保存配置时清除一次性开关，避免下次宿主重启再触发。
            updated = dict(config)
            updated["onlyonce"] = False
            try:
                self.update_config(updated)
            except Exception:
                logger.warning("栖影一次性开关持久化失败，未执行签到")
                return
            timer = threading.Timer(3, self._run, kwargs={"manual": True})
            timer.daemon = True
            self._timers.append(timer)
            timer.start()

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
        logger.info("栖影签到会话 Cookie 项：%s", ",".join(sorted(jar.keys())))
        return "; ".join(f"{name}={item.value}" for name, item in jar.items())

    @staticmethod
    def _cookie_items(cookie):
        jar = SimpleCookie()
        jar.load(cookie)
        return {name: item.value for name, item in jar.items()}

    def _session(self, cfg=None):
        """按快照构建会话；显式配置代理并禁用环境代理，保证“直连”语义真实。"""
        cfg = cfg or self._context.config
        proxy = self._proxy_from_settings() if cfg.use_system_proxy else cfg.proxy
        if cfg.use_browserless:
            if not cfg.browserless_url:
                raise ValueError("已启用 Browserless，请填写 /function 接口地址")
            cookie = self._parse_cookie(cfg.cookie)
            logger.info("栖影签到网络路径：Browserless")
            return _BrowserlessSession(cfg.browserless_url, cookie, proxy)
        if curl_requests is None:
            raise RuntimeError("缺少 curl_cffi 依赖")
        cookie = self._parse_cookie(cfg.cookie)
        # 禁用自动重定向：站点接口出现跳转本身即异常信号，按未确认结果处理，
        # 不携带会话凭据跟随到任何目标。
        session = curl_requests.Session(impersonate="chrome110", trust_env=False,
                                        allow_redirects=False)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
            logger.info("栖影签到网络路径：%s代理", "系统" if cfg.use_system_proxy else "自定义")
        elif cfg.use_system_proxy:
            logger.warning("栖影签到已开启系统代理，但未读取到代理配置，将直连")
        else:
            logger.info("栖影签到网络路径：直连（环境代理已禁用）")
        headers = {
            "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.BASE + "/me", "Origin": self.BASE,
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        }
        jar = getattr(session, "cookies", None)
        if jar is not None and hasattr(jar, "set"):
            # Cookie 进入会话 jar：限定站点域名 + secure，同域 HTTP 重定向不会带走会话。
            for name, value in self._cookie_items(cookie).items():
                jar.set(name, value, domain=self.SITE_HOST, path="/", secure=True)
            session.headers.update(headers)
        else:
            # 无 jar 的实现退回固定 Cookie 头（不享受轮换更新）。
            session.headers.update({"Cookie": cookie, **headers})
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
            # type(...) is int：拒绝 bool 等非数字，避免把异常响应记成成功。
            if any(type(result.get(k)) is not int for k in ("earned", "points", "checkin_streak")):
                raise RuntimeError("签到结果数字字段缺失；请人工核对")
            return {"state": "success", "earned": result["earned"],
                    "points": result["points"], "streak": result["checkin_streak"]}
        # 只有明确确认成功的响应才记为成功；下次运行需先读取站点状态再决定是否提交。
        raise RuntimeError("签到响应未确认，请在站点核对；再次运行会先查询站点今日签到状态")

    def _notice(self, title, text):
        if self._notify:
            try:
                self.post_message(mtype=MessageType.Plugin, title=title, text=text)
            except Exception:
                logger.warning("栖影签到通知发送失败")

    def _send_notify(self, status, date, result=None, reason="", warning=""):
        """Use site-confirmed numbers only; an uncertain POST is not a failed sign-in."""
        result = result or {}
        now = self._now().strftime("%Y-%m-%d %H:%M:%S")
        def value(key):
            item = result.get(key)
            return str(item) if type(item) is int else "未提供"

        if status == "success":
            title = f"栖影签到成功（{date}）"
            award = result.get("earned")
            earned = f"{award:+d}" if type(award) is int else "未提供"
            lines = ["✅ 签到模式：普通签", f"🧧 获得积分：{earned}",
                     f"💰 当前积分：{value('points')}", f"🔥 连续签到：{value('streak')} 天"]
        elif status == "already":
            title = f"栖影已签到（{date}）"
            lines = ["📅 今日已完成签到，无需重复操作", "",
                     f"💰 当前积分：{value('points')}", f"🔥 连续签到：{value('streak')} 天"]
        else:
            uncertain = status == "pending"
            title = f"栖影签到{'待核对' if uncertain else '未完成'}（{date}）"
            lines = [f"⚠️ {'结果待核对' if uncertain else '未完成原因'}：{reason or '未知'}", "",
                     "🔎 请先在站点核对签到状态，勿直接重复提交" if uncertain else
                     "🔎 请检查 Cookie、网络及站点状态"]
        if warning:
            lines = [*lines, "", f"⚠️ {warning}"]
        self._notice(title, "\n".join([*lines, "", f"🕒 {now}"]))

    @staticmethod
    def _history_rows(records):
        """Keep the most complete confirmed row per (account, date), newest thirty first.

        去重键含账号：不同账号同日记录互不覆盖；旧版未绑定账号的记录归入
        account_id=None 槽位，同日旧记录之间仍互斥，但与已绑定记录不互斥。
        """
        by_key = {}
        for row in records:
            if not isinstance(row, dict) or row.get("state") != "success":
                continue
            date = row.get("date")
            if not isinstance(date, str) or not date:
                continue
            key = (date, str(row.get("account_id")))
            previous = by_key.get(key)
            count = lambda item: sum(type(item.get(field)) is int
                                     for field in ("earned", "points", "streak"))
            if previous is None or count(row) >= count(previous):
                by_key[key] = row
        return [by_key[key] for key in sorted(by_key, reverse=True)[:30]]

    def _remember_result(self, result):
        """Best-effort display history; never change a confirmed sign-in outcome."""
        try:
            records = self.get_data("sign_records") or []
            if not isinstance(records, list):
                records = []
            self.save_data("sign_records", list(reversed(self._history_rows([*records, result]))))
        except Exception:
            logger.warning("栖影签到记录保存失败，不影响已确认的签到结果")

    def _remember_pending(self, pending):
        """Best-effort 未决尝试记录（按日期+账号一份），跨日后仍可追溯。"""
        try:
            rows = self.get_data("pending_attempts") or []
            if not isinstance(rows, list):
                rows = []
            rows = [row for row in rows if isinstance(row, dict)]
            key = (pending.get("date"), pending.get("account_id"))
            rows = [row for row in rows if (row.get("date"), row.get("account_id")) != key]
            rows.append(pending)
            rows.sort(key=lambda row: str(row.get("date") or ""))
            self.save_data("pending_attempts", rows[-30:])
        except Exception:
            logger.warning("栖影待核对尝试记录保存失败，不影响签到流程")

    def _resolve_pending(self, date, account_id):
        """站点给出明确结果后移除对应未决尝试；best-effort。"""
        try:
            rows = self.get_data("pending_attempts") or []
            if not isinstance(rows, list):
                return
            rows = [row for row in rows if isinstance(row, dict)
                    and not (row.get("date") == date and row.get("account_id") == account_id)]
            self.save_data("pending_attempts", rows)
        except Exception:
            logger.warning("栖影待核对尝试清理失败，不影响已确认的签到结果")

    def _store_confirmed(self, confirmed, today):
        """写入并读回已确认结果；存储故障自动重试一次。返回是否确认落盘。

        读回校验含账号：防止写入静默失败时，存储中恰好留有另一账号同日的
        success 记录被误判为本次已保存，进而错误清除本账号的待核对标记。
        """
        account_id = confirmed.get("account_id")
        for _ in range(2):
            try:
                self.save_data("last_result", confirmed)
                saved = self.get_data("last_result") or {}
                if (saved.get("date") == today and saved.get("state") == "success"
                        and saved.get("account_id") == account_id):
                    return True
            except Exception:
                pass
        return False

    def _remember_error(self, date, started_at, message):
        """Best-effort 记录最近一次失败原因，供首页展示；内容已脱敏。"""
        try:
            self.save_data("last_error", {"date": date, "started_at": started_at,
                                          "time": self._now().strftime("%H:%M:%S"),
                                          "message": message})
        except Exception:
            pass

    def _clear_error(self):
        try:
            self.save_data("last_error", {})
        except Exception:
            pass

    def _service_run(self, *args, **kwargs):
        """按宿主调度约定返回 (成功, 消息) 元组，避免失败字典被记为成功。"""
        result = self._run(*args, **kwargs)
        if isinstance(result, dict):
            return bool(result.get("success")), str(result.get("message") or "")
        return result

    def _ensure_current(self, ctx):
        """代际校验：上下文已被换代或取消标志置位时，中止尚未提交的签到。"""
        if self._context is not ctx or ctx.cancel.is_set():
            raise RuntimeError("配置已变更或插件已停止，签到未提交")

    def _run(self, manual=False):
        # 单次读取同代上下文：配置快照与取消标志不可能来自不同代。
        ctx = self._context
        if not manual and not self._enabled:
            return {"success": False, "message": "定时签到未启用"}
        if not self._lock.acquire(blocking=False):
            return {"success": False, "message": "签到正在运行"}
        try:
            return self._run_locked(ctx)
        finally:
            self._lock.release()

    def _run_locked(self, ctx):
        """会话生命周期与跨日重启循环；日期在每次尝试开始时重新计算。"""
        cfg = ctx.config
        session = None
        clock = self._now()
        today = clock.date().isoformat()
        started_at = clock.strftime("%Y-%m-%d %H:%M:%S")
        stage = "准备会话"
        try:
            if not cfg.cookie:
                raise RuntimeError("未配置栖影登录会话 Cookie")
            session = self._session(cfg)
            for attempt in range(2):
                clock = self._now()
                today = clock.date().isoformat()
                started_at = clock.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    return self._run_day(ctx, session, today, started_at)
                except _DateRollover:
                    if attempt:
                        raise RuntimeError("宿主日期连续跨日，本次未提交签到") from None
                    logger.info("栖影签到：运行跨过午夜，已按新日期重新执行当天流程")
            raise RuntimeError("宿主日期连续跨日，本次未提交签到") from None
        except Exception as exc:
            logger.warning("栖影签到未完成：阶段=%s，异常类型=%s", stage, type(exc).__name__)
            detail = str(exc) if type(exc) in (RuntimeError, ValueError) else ""
            message = (detail if detail in self._SAFE_ERRORS
                       else "请求或存储异常；请检查 Cookie、网络及站点状态")
            try:
                current = self.get_data("last_result") or {}
            except Exception:
                current = {}
            uncertain = (isinstance(current, dict) and current.get("date") == today
                         and current.get("state") == "pending")
            self._remember_error(today, started_at, message)
            self._send_notify("pending" if uncertain else "incomplete", today, reason=message)
            return {"success": False, "message": message}
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _run_day(self, ctx, session, today, started_at):
        stage = "登录预检"
        account_id = None
        try:
            # 先查询站点签到状态；本地待核对记录不能代替站点结果。
            logger.info("栖影签到登录预检：开始只读请求")
            me = session.get(self.BASE + "/api/auth/me", timeout=20)
            content_type = (me.headers.get("content-type") or "").lower()
            response_type = "JSON" if "json" in content_type else "非JSON"
            logger.info("栖影签到登录预检：HTTP %s，响应类型=%s，发生跳转=%s",
                        me.status_code, response_type, bool(getattr(me, "history", None)))
            try:
                account = self._decode(me)
            except RuntimeError:
                if me.status_code != 200:
                    logger.warning("栖影签到登录预检：站点拒绝——server=%s，cf-mitigated=%s",
                                   me.headers.get("server") or "-", me.headers.get("cf-mitigated") or "-")
                raise
            has_account = isinstance(account.get("data"), dict) and bool(account["data"].get("id"))
            logger.info("栖影签到登录预检：success=%s，账户标识存在=%s",
                        account.get("success") is True, has_account)
            if me.status_code != 200 or account.get("success") is not True or not has_account:
                # 仅记录站点自身返回的拒绝原因与边缘头，便于区分会话失效、IP/指纹风控。
                logger.warning("栖影签到登录预检：站点拒绝——code=%s，message=%s，server=%s，cf-mitigated=%s",
                               account.get("code", "-"), str(account.get("message") or "-")[:100],
                               me.headers.get("server") or "-", me.headers.get("cf-mitigated") or "-")
                if account.get("code") == "SESSION_VERIFY_REQUIRED":
                    raise RuntimeError("站点检测到网络环境变化：请在与插件相同的代理节点下重新登录并完成人机验证，再更新 Cookie")
                raise RuntimeError("会话未通过登录预检，请重新登录并更新 Cookie")
            logger.info("栖影签到登录预检：通过")
            account_id = account["data"].get("id")
            status = account["data"].get("checkin_today")
            last_checkin = account["data"].get("last_checkin")
            if type(status) is not bool or (last_checkin is not None and
                                            (not isinstance(last_checkin, str) or
                                             (status and last_checkin != today) or
                                             (not status and last_checkin == today))):
                raise RuntimeError("站点今日签到状态无法确认，未发送签到请求")
            if status:
                # 站点确认已签到：本地数字仅在归属同一账号时复用。
                stage = "已签到结果保存"
                previous = self.get_data("last_result") or {}
                confirmed = {"date": today, "state": "success", "account_id": account_id}
                if (isinstance(previous, dict) and previous.get("date") == today
                        and previous.get("state") == "success"
                        and previous.get("account_id") == account_id):
                    confirmed.update({key: previous[key] for key in ("earned", "points", "streak")
                                      if type(previous.get(key)) is int})
                account_data = account["data"]
                live_numbers = {"points": account_data.get("points"),
                                "streak": account_data.get("checkin_streak")}
                saved_ok = self._store_confirmed(confirmed, today)
                self._remember_result({**confirmed, "time": self._now().strftime("%H:%M:%S")})
                if saved_ok:
                    self._resolve_pending(today, account_id)
                    self._clear_error()
                    self._send_notify("already", today, live_numbers)
                    logger.info("栖影签到：站点确认今日已签到，跳过提交")
                    return {"success": True, "message": "站点确认今日已签到，不重复提交"}
                # 保存失败：保留 pending last_result 与未决尝试的一致状态，不自相矛盾；
                # 下次运行 already 分支会覆盖为已确认并清理。
                warning = "本地记录保存失败，请检查 MoviePilot 存储；站点已确认今日已签到"
                self._send_notify("already", today, live_numbers, warning=warning)
                return {"success": True,
                        "message": "站点确认今日已签到，但本地记录保存失败，请检查本地存储"}
            logger.info("栖影签到：站点确认今日未签到，准备提交")
            self._ensure_current(ctx)
            # 提交临界区：代际校验 → 待核对标记 → POST，与配置重载/停止互斥；
            # 重载必须等本临界区让出才能换代，检查与 POST 之间不再有中止窗口。
            with self._commit_lock:
                self._ensure_current(ctx)
                if self._now().date().isoformat() != today:
                    # 跨日：站点会把提交计入新的一天，按旧日期落库会错位；中止并按新日期重启。
                    raise _DateRollover
                # 持久化并读回 pending，防止 POST 已送达而本地无法记录时再次扣账/重复签到。
                attempt_id = uuid.uuid4().hex[:12]
                stage = "保存待核对标记"
                self.save_data("last_result", {"date": today, "state": "pending",
                                               "account_id": account_id, "attempt_id": attempt_id,
                                               "started_at": started_at})
                marker = self.get_data("last_result") or {}
                if (marker.get("date") != today or marker.get("state") != "pending"
                        or marker.get("attempt_id") != attempt_id):
                    raise RuntimeError("待核对标记保存失败，未发送签到请求")
                self._remember_pending({"attempt_id": attempt_id, "date": today,
                                        "account_id": account_id, "started_at": started_at})
                # 浏览器 Network：POST /api/account/checkin，Content-Length: 0；不要传 json={}。
                stage = "签到请求"
                logger.info("栖影签到：预检通过且待核对标记已保存，准备提交签到")
                response = session.post(self.BASE + "/api/account/checkin", timeout=20)
            if self._context is not ctx:
                logger.info("栖影签到：提交期间配置已变更，结果按提交时的配置记录")
            logger.info("栖影签到响应：HTTP %s，响应类型=%s", response.status_code,
                        "JSON" if "json" in (response.headers.get("content-type") or "").lower() else "非JSON")
            result = self._interpret(response)
            if self._now().date().isoformat() != today:
                # 请求本身跨过午夜：站点实际记账日期未知，不把结果静默归入旧日期；
                # pending 标记与未决尝试原样保留（状态自洽），数字经通知承载，
                # 下次运行按站点 checkin_today 自愈。
                self._send_notify("success", today, result,
                                  warning="签到请求跨过午夜：记账日期以站点记录为准；本地保留待核对状态")
                return {"success": True,
                        "message": "签到成功（跨午夜提交；本地记录保留待核对状态）",
                        "data": result}
            stage = "结果保存"
            confirmed = {"date": today, "state": "success", "account_id": account_id,
                         "time": self._now().strftime("%H:%M:%S"), **result}
            saved_ok = self._store_confirmed(confirmed, today)
            if saved_ok:
                self._resolve_pending(today, account_id)
                self._remember_result(dict(confirmed))
                self._clear_error()
                self._send_notify("success", today, result)
                return {"success": True, "message": "签到成功", "data": result}
            # 站点已明确确认成功，不得降级为“待核对”：数字保留在通知中；
            # pending last_result 与未决尝试一并保留（状态自洽），页面按站点确认展示，
            # 下次运行的 already 分支会把本地标记覆盖为已确认，完成自愈。
            self._send_notify("success", today, result,
                              warning="站点已确认签到成功，但本地记录保存失败；待核对标记将在下次签到后更新")
            return {"success": True,
                    "message": "站点已确认签到成功，但本地记录保存失败，请检查本地存储",
                    "data": result}
        except _DateRollover:
            raise
        except Exception as exc:
            # 不输出网络异常原文：可能含 Cookie/代理地址等敏感字段。
            logger.warning("栖影签到未完成：阶段=%s，异常类型=%s", stage, type(exc).__name__)
            detail = str(exc) if type(exc) in (RuntimeError, ValueError) else ""
            message = detail if detail in self._SAFE_ERRORS else (
                "请求或存储异常，结果可能已提交；请人工核对，勿重试"
                if stage in ("签到请求", "结果保存") else "请求或存储异常；请检查 Cookie、网络及站点状态")
            try:
                current = self.get_data("last_result") or {}
            except Exception:
                current = {}
            uncertain = stage in ("签到请求", "结果保存") or (
                isinstance(current, dict) and current.get("date") == today
                and current.get("state") == "pending")
            self._remember_error(today, started_at, message)
            self._send_notify("pending" if uncertain else "incomplete", today, reason=message)
            return {"success": False, "message": message}

    def get_state(self):
        return self._enabled and bool(self._context.config.cookie)

    def _cron_trigger(self):
        try:
            return CronTrigger.from_crontab(self._cron, timezone=self._timezone_name())
        except (ValueError, TypeError):
            logger.warning("栖影签到 Cron 无效，未注册定时任务；请检查格式（分 时 日 月 周）")
            return None

    def get_service(self):
        if not self.get_state():
            return []
        trigger = self._cron_trigger()
        if trigger is None:
            return []
        return [{"id": "HDHavenSign", "name": "栖影自动签到",
                 "trigger": trigger, "func": self._service_run, "kwargs": {}}]

    def get_command(self):
        return []

    def get_api(self):
        return []

    def get_form(self):
        def field(component, model, label, **props):
            return {"component": component, "props": {"model": model, "label": label, **props}}

        def row(*cols):
            return {"component": "VRow", "content": list(cols)}

        def col(*content, **props):
            props.setdefault("cols", 12)
            return {"component": "VCol", "props": props, "content": list(content)}

        return ([{"component": "VForm", "content": [
            row(
                col(field("VSwitch", "enabled", "启用定时签到"), md=4),
                col(field("VSwitch", "onlyonce", "保存后执行一次签到"), md=4),
                col(field("VSwitch", "notify", "发送结果通知"), md=4),
            ),
            row(
                col(field("VTextField", "cookie", "栖影登录会话 Cookie", type="password",
                          hint="填写浏览器登录后的完整 Cookie（须包含非空 hdh_session 项），失效后需手动更新",
                          **{"persistent-hint": True}), md=8),
                col(field("VTextField", "cron", "Cron（默认每天 09:30）",
                          placeholder="分 时 日 月 周",
                          hint="五字段 Cron，如 30 9 * * *；非法值自动回退默认",
                          **{"persistent-hint": True}), md=4),
            ),
            row(
                col(field("VSwitch", "use_system_proxy", "使用系统代理（默认开启）"), md=4),
                col(field("VTextField", "proxy", "自定义代理（关闭系统代理时使用）"), md=8),
            ),
            row(
                col({"component": "VCard", "props": {"class": "mt-4 mb-3", "variant": "outlined"},
                     "content": [
                         {"component": "VCardTitle", "text": "Browserless 备用模式（可选）"},
                         {"component": "VCardText", "props": {"class": "pt-2"}, "content": [
                             field("VSwitch", "use_browserless", "启用 Browserless（默认关闭）",
                                   hint="开启后本次请求经 Browserless 转发，失败不自动重发签到",
                                   **{"persistent-hint": True}),
                             field("VTextField", "browserless_url", "Browserless HTTPS /function 地址",
                                   type="password",
                                   hint="填写完整地址（含 ?token=...）；站点 Cookie 将发送给该服务，请仅使用可信服务",
                                   **{"persistent-hint": True}),
                         ]},
                     ]}),
            ),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                      "text": "登录需浏览器完成人机验证；每次先查询站点签到状态，仅明确未签到才提交请求。"}},
        ]}], {"enabled": False, "onlyonce": False, "notify": True, "cookie": "",
              "cron": "30 9 * * *", "use_system_proxy": True, "proxy": "",
              "use_browserless": False, "browserless_url": ""})

    def get_page(self):
        cfg = self._context.config
        today = self._now().date().isoformat()
        record = self.get_data("last_result") or {}
        if not isinstance(record, dict):
            record = {}
        records = self.get_data("sign_records") or []
        if not isinstance(records, list):
            records = []
        pending_rows = [row for row in (self.get_data("pending_attempts") or [])
                        if isinstance(row, dict) and row.get("date")]
        error = self.get_data("last_error") or {}
        if not isinstance(error, dict):
            error = {}
        if record.get("state") == "success" and record.get("date"):
            records.append(record)
        all_rows = self._history_rows(records)

        account = None
        if cfg.cookie:
            session = None
            try:
                session = self._session(cfg)
                response = session.get(self.BASE + "/api/auth/me", timeout=20)
                payload = self._decode(response)
                if response.status_code == 200 and payload.get("success") is True and isinstance(payload.get("data"), dict):
                    account = payload["data"]
            except Exception:
                logger.warning("栖影首页只读账户查询失败")
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
        account_id = account.get("id") if account else record.get("account_id")
        # 记录按账号隔离：已知当前账号时仅展示本账号与未绑定账号的旧记录。
        visible = [row for row in all_rows
                   if account_id is None or row.get("account_id") in (None, account_id)]
        counted = ([row for row in visible if row.get("account_id") == account_id]
                   if account_id is not None else visible)

        def number(value, suffix=""):
            return f"{value:,}{suffix}" if type(value) is int else "未提供"

        def kpi(icon, label, value, color, note):
            return {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                {"component": "VCardText", "props": {"class": "text-center pa-3"}, "content": [
                    {"component": "div", "props": {"class": "text-h5 mb-1"}, "text": icon},
                    {"component": "div", "props": {
                        "class": f"text-h6 font-weight-bold text-{color}",
                        "style": "white-space:normal;overflow-wrap:anywhere;line-height:1.2",
                    }, "text": value},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": note},
                ]},
            ]}

        latest = visible[0] if visible else {}
        nickname = (account or {}).get("nickname") or (account or {}).get("username")
        points = (account or {}).get("points")
        streak = (account or {}).get("checkin_streak")
        signed = (account or {}).get("checkin_today")
        # 今日状态按账号隔离：仅本账号（或旧版未绑定的兼容记录）可代表当前账号。
        # 今日状态按账号隔离：仅本账号（或旧版未绑定的兼容记录）可代表当前账号；
        # last_result 被其他账号覆盖时，从账号级未决尝试恢复本账号的今日待核对状态。
        record_mine = account_id is None or record.get("account_id") in (None, account_id)
        own_pending_attempt = account_id is not None and any(
            row.get("date") == today and row.get("account_id") == account_id
            for row in pending_rows)
        local_confirmed = (record_mine and record.get("date") == today
                           and record.get("state") == "success")
        if ((record_mine and record.get("date") == today
             and record.get("state") == "pending") or own_pending_attempt):
            # 站点确认已签到即视为已解决：本地标记待下次运行覆盖，如实展示恢复中状态。
            today_state = ("已签到（站点确认；本地待核对标记待更新）" if signed is True
                           else "结果待核对")
        elif signed is True:
            today_state = "已签到"
        elif local_confirmed and signed is None:
            today_state = "已签到（本地已确认，实时查询失败）"
        elif signed is False:
            today_state = "未签到"
        else:
            today_state = "无法确认"
        earned_total = sum(row["earned"] for row in counted if type(row.get("earned")) is int)
        earned_days = sum(type(row.get("earned")) is int for row in counted)
        tiles = [
            ("👤", "账号", str(nickname or "未提供"), "primary", "站点只读账户信息"),
            ("💰", "当前积分", number(points) if account else "获取失败", "info", "站点实时数据"),
            ("🔥", "连续签到", number(streak, " 天") if type(streak) is int else number(latest.get("streak"), " 天"),
             "success", "站点实时数据" if type(streak) is int else "最近一次确认的本地结果"),
            ("🧧", "最近 30 条获得积分", number(earned_total)
             if any(type(row.get("earned")) is int for row in counted) else "未提供",
             "warning", f"本账号有积分记录 {earned_days} 天（仅统计最近 30 条）"),
        ]
        page = [{"component": "VRow", "props": {"dense": True}, "content": [
            {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6}, "content": [kpi(*tile)]}
            for tile in tiles
        ]}]
        today_lines = [f"{today_state} · 最近记录：{record.get('date') if record_mine else '—'}"]
        stale_pending = [str(row.get("date")) for row in pending_rows
                         if row.get("date") != today
                         or (account_id is not None
                             and row.get("account_id") not in (None, account_id))]
        if stale_pending:
            today_lines.append(f"历史待核对：{'、'.join(stale_pending[:10])}（站点状态未确认，请人工核对）")
        if error and not local_confirmed:
            error_line = f"最近一次失败：{error.get('date') or '—'} {error.get('time') or ''} {error.get('message') or ''}".rstrip()
            today_lines.append(error_line)
        page.append({"component": "VCard", "props": {"variant": "tonal", "class": "ma-2"}, "content": [
            {"component": "VCardTitle", "text": "📅 今日签到"},
            {"component": "VCardText", "text": " ｜ ".join(today_lines)},
        ]})
        legacy_visible = account_id is not None and any(row.get("account_id") is None for row in visible)
        table_note = "旧版记录未绑定账号，不计入本账号累计；窄屏可左右滑动查看完整表格" if legacy_visible \
            else "窄屏可左右滑动查看完整表格"
        headers = ("日期", "时间", "获得积分", "签到后积分", "连续签到", "状态")
        rows = [{"component": "tr", "content": [
            {"component": "td", "text": text} for text in (
                str(row.get("date") or "—"), str(row.get("time") or "—"),
                number(row.get("earned")), number(row.get("points")),
                number(row.get("streak"), " 天"),
                "已签到" if row.get("account_id") is not None else "已签到（未绑定账号）",
            )
        ]} for row in visible[:30]]
        table = [{"component": "div", "props": {"style": "overflow-x:auto;max-width:100%"}, "content": [{
            "component": "VTable", "props": {"density": "compact", "style": "min-width:680px"}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "props": {"class": "text-no-wrap"}, "text": title} for title in headers
                ]}]},
                {"component": "tbody", "content": rows},
            ],
        }]}] if rows else [{"component": "VAlert", "props": {
            "type": "info", "variant": "tonal", "text": "暂无本地签到记录"}}]
        page.append({"component": "VCard", "props": {"variant": "tonal", "class": "ma-2"}, "content": [
            {"component": "VCardTitle", "text": "📋 签到记录（最近 30 条）"},
            {"component": "VCardText", "props": {"class": "pa-2"}, "content": [
                *table,
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                 "text": table_note},
            ]},
        ]})
        return page

    def stop_service(self):
        """按宿主合同返回收敛结果：True 已收敛；False 未收敛（宿主只认字面量 False）。"""
        self._revoke_pending_runs()
        if self._lock.acquire(timeout=self.STOP_WAIT_SECONDS):
            self._lock.release()
            return True
        logger.warning("栖影签到仍有任务未收敛（等待 %s 秒超时），结果可能稍后落盘并产生通知",
                       self.STOP_WAIT_SECONDS)
        return False
