"""三个论坛的签到协议实现：药丸（invites.fun）、蜂巢（fengchao.chat）、蓝影（hdblue.cc）。

接口行为逐条对照各自上游插件核实：
- 药丸对照 jxxghp/MoviePilot-Plugins plugins.v2/invitessignin v3.0.1（Flarum remember 会话 + CSRF）。
- 蜂巢对照 madrays/MoviePilot-Plugins plugins.v3/fengchaosignin v3.1.10（Bearer Key 集成 API）。
- 蓝影对照 jxxghp/MoviePilot-Plugins plugins.v3/hdbluesignin v2.0.2 core.py（专用密钥集成 API）。

本模块不导入宿主 ``app.*``：全部网络行为可离线伪造，供插件与单测复用。
错误一律抛出本地定义的 :class:`ForumError`；请求异常原文（可能含 Cookie/代理地址）不进入
日志或通知，只暴露本地可信文案与站点白名单字段。
"""
from __future__ import annotations

import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

import requests

try:  # 浏览器仿真为可选项；宿主未安装时自动降级为纯 requests 并提示
    from cloakbrowser import launch_context
except ImportError:
    launch_context = None

try:
    from zoneinfo import ZoneInfo
    TZ_BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 等缺时区库环境下退化为固定东八区
    TZ_BEIJING = timezone(timedelta(hours=8))


class ForumError(RuntimeError):
    """论坛签到失败；``uncertain`` 表示 POST 可能已生效但结果未确认。"""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: int = 0,
                 uncertain: bool = False, auth: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.uncertain = uncertain
        self.auth = auth


def session_factory() -> requests.Session:
    """独立会话工厂：trust_env=False，代理只走插件显式配置，不读环境变量。"""
    session = requests.Session()
    session.trust_env = False
    return session


def _proxy_dict(proxy: str) -> Optional[dict]:
    return {"http": proxy, "https": proxy} if proxy else None


def _beijing_today() -> str:
    """蓝影协议以北京时间为准；上游 core.py 明确使用 Asia/Shanghai 判定签到日。"""
    return datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")


def _retry_after_seconds(value: Any) -> int:
    """解析 429 的 Retry-After（秒数或 HTTP 日期），失败返回 0。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            remaining = parsedate_to_datetime(str(value)) - datetime.now(TZ_BEIJING)
            return max(0, int(remaining.total_seconds()) + 1)
        except (TypeError, ValueError, OverflowError):
            return 0


def _site_error_detail(response) -> str:
    """从站点错误响应中提取白名单字段（detail/title/code），截断防泄漏。"""
    try:
        payload = response.json()
    except Exception:
        text = str(getattr(response, "text", "") or "").strip()
        return text[:200] if text else ""
    if not isinstance(payload, dict):
        return ""
    messages = []
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            detail = error.get("detail") or error.get("title") or error.get("code")
            if detail:
                messages.append(str(detail))
    if not messages and payload.get("message"):
        messages.append(str(payload["message"]))
    return "；".join(messages)[:200]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10 ** 18


class InvitesClient:
    """药丸论坛（invites.fun，Flarum 架构）签到。

    优先用配置 Cookie 中的 ``flarum_remember`` 换取新 ``flarum_session``；失败且配置了
    账号密码时走 ``POST /login`` 兜底。签到判定以站点实时状态为准：
    ``canCheckin=false`` 且 ``lastCheckinTime`` 落在今天才算成功。
    """

    BASE = "https://invites.fun"
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0")
    TIMEOUT = 20

    def __init__(self, cookie: str = "", username: str = "", password: str = "",
                 use_browser: bool = False, proxy: str = "",
                 on_cookie_update: Optional[Callable[[str, str], None]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.cookie = str(cookie or "").strip()
        self.username = str(username or "").strip()
        self.password = password or ""
        self.use_browser = bool(use_browser)
        self.proxy = proxy or ""
        # 参数为 (flarum_remember, flarum_session)，由插件决定是否回写配置。
        self.on_cookie_update = on_cookie_update
        # 降级等提示经插件注入宿主日志；本模块不导入 app.*。
        self.log = log or (lambda message: None)

    # ---- 内部工具 ----

    def _html_headers(self, cookie_header: str = "") -> dict:
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _api_headers(self, csrf_token: str) -> dict:
        """对齐药丸前端签到请求的浏览器头。"""
        return {
            "accept": "*/*",
            "accept-language": "zh-CN,zh-Hans;q=0.9",
            "origin": self.BASE,
            "referer": self.BASE + "/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-csrf-token": csrf_token,
            "user-agent": self.USER_AGENT,
        }

    def _refresh_session(self, remember: str) -> str:
        """用 flarum_remember 换取新 flarum_session（禁重定向以捕获 Set-Cookie）。"""
        self.log("刷新会话：GET https://invites.fun（禁重定向）")
        try:
            with session_factory() as session:
                response = session.get(self.BASE, allow_redirects=False,
                                       cookies={"flarum_remember": remember},
                                       headers=self._html_headers(),
                                       proxies=_proxy_dict(self.proxy), timeout=self.TIMEOUT)
        except requests.RequestException as exc:
            self.log(f"刷新会话网络异常：{type(exc).__name__}")
            raise ForumError("药丸站点连接失败（网络异常）", retryable=True) from None
        if response.status_code >= 400:
            self.log(f"刷新会话失败：HTTP {response.status_code}")
            raise ForumError(f"药丸站点拒绝请求（HTTP {response.status_code}），请确认站点可访问")
        session_value = response.cookies.get("flarum_session")
        if not session_value:
            match = re.search(r"flarum_session=([^;]+)", response.headers.get("Set-Cookie", ""))
            session_value = match.group(1) if match else None
        if not session_value:
            self.log("刷新会话失败：HTTP 200 但未下发新 flarum_session（remember 可能已失效）")
            raise ForumError("药丸 Cookie 已失效：站点未返回新会话，请重新登录勾选记住我后更新 Cookie")
        self.log("刷新会话成功：新 flarum_session 已下发")
        return session_value

    def _page_via_browser(self, cookie_header: str) -> Optional[str]:
        """CloakBrowser 渲染页面，用于绕过 Cloudflare 防护；失败返回 None。"""
        context = page = None
        try:
            context = launch_context(headless=True, proxy=self.proxy or None)
            page = context.new_page()
            if cookie_header:
                page.set_extra_http_headers({"cookie": cookie_header})
            page.goto(self.BASE)
            page.wait_for_load_state("networkidle", timeout=60 * 1000)
            return page.content()
        except Exception:
            return None
        finally:
            if page:
                page.close()
            if context:
                context.close()

    def _fetch_page(self, cookie_header: str) -> str:
        if self.use_browser:
            if launch_context is None:
                self.log("已启用浏览器仿真，但宿主缺少 cloakbrowser 依赖，降级为普通请求")
            else:
                self.log("获取页面：CloakBrowser 渲染中")
                html = self._page_via_browser(cookie_header)
                if html:
                    self.log(f"获取页面成功：CloakBrowser 返回 {len(html)} 字符")
                    return html
                self.log("浏览器仿真获取药丸页面失败，降级为普通请求")
        try:
            with session_factory() as session:
                response = session.get(self.BASE, headers=self._html_headers(cookie_header),
                                       proxies=_proxy_dict(self.proxy), timeout=self.TIMEOUT)
        except requests.RequestException as exc:
            self.log(f"获取页面网络异常：{type(exc).__name__}")
            raise ForumError("药丸页面请求失败（网络异常）", retryable=True) from None
        if response.status_code != 200:
            self.log(f"获取页面失败：HTTP {response.status_code}")
            raise ForumError(f"请求药丸页面失败（HTTP {response.status_code}）")
        self.log(f"获取页面成功：HTTP 200，{len(response.text)} 字符")
        return response.text

    def _extract_identity(self, html: str) -> tuple[str, str]:
        """从页面负载中提取 csrfToken 与 userId。"""
        csrf_match = re.search(r'"csrfToken":"(.*?)"', html)
        user_match = re.search(r'"userId":(\d+)', html)
        if not csrf_match or not user_match:
            self.log("提取身份失败：页面不含 Flarum csrfToken/userId 负载"
                     "（可能触发人机验证或 Cookie 失效）")
            raise ForumError("无法从药丸页面提取 csrfToken/userId：Cookie 可能失效或触发人机验证，可开启浏览器仿真重试")
        self.log(f"提取身份成功：userId={user_match.group(1)}")
        return csrf_match.group(1), user_match.group(1)

    @staticmethod
    def _is_today(state: dict) -> bool:
        last_time = str((state or {}).get("lastCheckinTime") or "")
        return bool(last_time and last_time.startswith(datetime.now().strftime("%Y-%m-%d")))

    def _fetch_state(self, user_id: str, cookies: dict, csrf_token: str) -> dict:
        """查询站点实时签到状态（JSON:API 用户属性），失败返回空字典。"""
        try:
            with session_factory() as session:
                response = session.get(f"{self.BASE}/api/users/{user_id}", cookies=cookies,
                                       headers=self._api_headers(csrf_token),
                                       proxies=_proxy_dict(self.proxy), timeout=self.TIMEOUT)
        except requests.RequestException as exc:
            self.log(f"查询签到状态网络异常：{type(exc).__name__}")
            return {}
        if response.status_code != 200:
            self.log(f"查询签到状态失败：HTTP {response.status_code}")
            return {}
        try:
            payload = response.json()
        except ValueError:
            self.log("查询签到状态失败：响应非 JSON")
            return {}
        data = payload.get("data") if isinstance(payload, dict) else None
        attrs = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attrs, dict):
            self.log("查询签到状态失败：响应缺少用户属性")
            return {}
        self.log(f"查询签到状态：canCheckin={attrs.get('canCheckin')}，"
                 f"lastCheckinTime={attrs.get('lastCheckinTime') or '—'}，"
                 f"余额={attrs.get('money')}，累计={attrs.get('totalContinuousCheckIn')} 天")
        return {
            "user_id": str(data.get("id") or ""),
            "canCheckin": attrs.get("canCheckin"),
            "lastCheckinTime": attrs.get("lastCheckinTime") or "",
            "totalContinuousCheckIn": attrs.get("totalContinuousCheckIn"),
            "money": attrs.get("money"),
        }

    @staticmethod
    def _result(status: str, message: str, state: dict) -> dict:
        return {"status": status, "message": message,
                "reward": None, "points": state.get("money"),
                "streak": state.get("totalContinuousCheckIn"), "account": None,
                "checked_today": True, "recent": [], "level_name": None, "level_icon": None}

    def _verify_checkin(self, user_id: str, cookies: dict, csrf_token: str) -> Optional[dict]:
        """POST 结果不确定时立即复核站点状态；确认已签则返回已签到结果，否则 None。"""
        try:
            state = self._fetch_state(user_id, cookies, csrf_token)
        except Exception:
            return None
        if state.get("canCheckin") is False and self._is_today(state):
            self.log(f"复核确认：站点显示今日已签到（lastCheckinTime={state.get('lastCheckinTime')}）")
            return self._result("already", "复核确认今日已签到", state)
        return None

    def _do_checkin(self, user_id: str, cookies: dict, csrf_token: str) -> dict:
        """签到前先查站点实时状态，签到后以响应中的用户属性复核。"""
        before = self._fetch_state(user_id, cookies, csrf_token)
        if before.get("canCheckin") is False and self._is_today(before):
            self.log("签到前检查：站点显示今日已签到，跳过提交")
            return self._result("already", "今日已签到", before)
        self.log(f"签到前检查：今日未签到，提交 POST {self.BASE}/api/checkin（userId={user_id}）")
        try:
            with session_factory() as session:
                response = session.post(f"{self.BASE}/api/checkin", cookies=cookies,
                                        headers=self._api_headers(csrf_token),
                                        proxies=_proxy_dict(self.proxy), timeout=self.TIMEOUT)
        except requests.RequestException as exc:
            self.log(f"签到请求网络异常：{type(exc).__name__}（结果不确定，立即复核）")
            verified = self._verify_checkin(user_id, cookies, csrf_token)
            if verified:
                return verified
            raise ForumError("药丸签到请求网络异常，结果未确认", retryable=True,
                             uncertain=True) from None
        if response.status_code != 200:
            # 站点可能已先落账（如重复点击），复核后按已签到处理。
            detail = _site_error_detail(response)
            self.log(f"签到请求失败：HTTP {response.status_code}"
                     + (f"，站点原因：{detail}" if detail else "")
                     + f"，server={response.headers.get('server') or '-'}"
                     + f"，cf-mitigated={response.headers.get('cf-mitigated') or '-'}；复核站点状态")
            after = self._fetch_state(user_id, cookies, csrf_token)
            if after.get("canCheckin") is False and self._is_today(after):
                self.log("复核确认：站点显示今日已签到，按已签到处理")
                return self._result("already", "站点显示今日已签到", after)
            raise ForumError(f"药丸签到请求失败（HTTP {response.status_code}）"
                             + (f":{detail}" if detail else ""))
        try:
            payload = response.json()
        except ValueError:
            self.log("签到响应无法解析：非 JSON，立即复核站点状态")
            verified = self._verify_checkin(user_id, cookies, csrf_token)
            if verified:
                return verified
            raise ForumError("药丸签到响应无法解析，请到站点核对签到状态") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        attrs = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attrs, dict):
            self.log("签到响应异常：缺少用户属性，立即复核站点状态")
            verified = self._verify_checkin(user_id, cookies, csrf_token)
            if verified:
                return verified
            raise ForumError("药丸签到响应缺少用户状态，请到站点核对")
        state = {"canCheckin": attrs.get("canCheckin"),
                 "lastCheckinTime": attrs.get("lastCheckinTime") or "",
                 "totalContinuousCheckIn": attrs.get("totalContinuousCheckIn"),
                 "money": attrs.get("money")}
        if state.get("canCheckin") is not False or not self._is_today(state):
            self.log(f"签到响应未确认：canCheckin={state.get('canCheckin')}，"
                     f"lastCheckinTime={state.get('lastCheckinTime') or '—'}，立即复核站点状态")
            verified = self._verify_checkin(user_id, cookies, csrf_token)
            if verified:
                return verified
            raise ForumError("药丸签到响应未确认今日已签到，请到站点核对")
        self.log(f"签到响应确认：lastCheckinTime={state.get('lastCheckinTime')}，"
                 f"余额={state.get('money')}，累计={state.get('totalContinuousCheckIn')} 天")
        return self._result("success", "签到成功", state)

    def _remember_cookie(self, remember: str, session_value: str) -> None:
        if self.on_cookie_update:
            try:
                self.on_cookie_update(remember, session_value)
            except Exception:
                pass

    # ---- 会话准备与签到路径 ----

    def _prepare(self) -> dict:
        """获取有效会话：Cookie 优先，账号密码登录兜底；全部失败时汇总原因。"""
        errors = []
        if self.cookie and "flarum_remember=" in self.cookie:
            try:
                remember_match = re.search(r"flarum_remember=([^;\s]+)", self.cookie)
                if not remember_match:
                    raise ForumError("配置的药丸 Cookie 中缺少 flarum_remember，请登录时勾选记住我后重新复制")
                remember = remember_match.group(1)
                new_session = self._refresh_session(remember)
                cookie_header = f"flarum_remember={remember}; flarum_session={new_session}"
                html = self._fetch_page(cookie_header)
                csrf_token, user_id = self._extract_identity(html)
                return {"remember": remember, "session": new_session,
                        "cookies": {"flarum_remember": remember, "flarum_session": new_session},
                        "csrf": csrf_token, "user_id": user_id}
            except ForumError as error:
                errors.append(f"Cookie 方式：{error}")
        if self.username and self.password:
            try:
                return self._login()
            except ForumError as error:
                errors.append(f"登录方式：{error}")
        elif not (self.cookie and "flarum_remember=" in self.cookie):
            errors.append("未配置有效 Cookie 或账号密码")
        self.log("会话获取失败：" + ("；".join(errors) if errors else "未知原因"))
        raise ForumError("；".join(errors) or "药丸会话获取失败")

    def _login(self) -> dict:
        """账号密码登录兜底：先取初始会话与 csrf，再 POST /login。"""
        self.log("登录兜底：获取初始会话 GET https://invites.fun")
        try:
            with session_factory() as session:
                first = session.get(self.BASE, headers=self._html_headers(),
                                    proxies=_proxy_dict(self.proxy), timeout=self.TIMEOUT)
                if first.status_code != 200:
                    self.log(f"登录兜底失败：初始会话 HTTP {first.status_code}")
                    raise ForumError(f"获取药丸初始会话失败（HTTP {first.status_code}）")
                initial_session = first.cookies.get("flarum_session")
                csrf_token = first.headers.get("x-csrf-token")
                if not initial_session or not csrf_token:
                    self.log("登录兜底失败：站点未返回初始会话或 csrf token")
                    raise ForumError("药丸未返回初始会话或 csrf token，站点可能启用人机验证")
                self.log("登录兜底：POST /login")
                login = session.post(
                    f"{self.BASE}/login",
                    json={"identification": self.username, "password": self.password, "remember": True},
                    headers={"accept": "*/*", "content-type": "application/json; charset=UTF-8",
                             "origin": self.BASE, "referer": self.BASE + "/",
                             "x-csrf-token": csrf_token, "user-agent": self.USER_AGENT},
                    proxies=_proxy_dict(self.proxy), timeout=self.TIMEOUT)
        except requests.RequestException as exc:
            self.log(f"登录兜底网络异常：{type(exc).__name__}")
            raise ForumError("药丸站点连接失败（网络异常）", retryable=True) from None
        if login.status_code != 200:
            detail = _site_error_detail(login)
            self.log(f"登录失败：HTTP {login.status_code}"
                     + (f"，站点原因：{detail}" if detail else ""))
            raise ForumError(f"药丸账号密码登录失败（HTTP {login.status_code}）"
                             + (f":{detail}" if detail else "，请检查账号密码"))
        remember = login.cookies.get("flarum_remember")
        new_session = login.cookies.get("flarum_session")
        if not remember or not new_session:
            raise ForumError("药丸登录后未返回有效 Cookie")
        try:
            user_id = str((login.json() or {}).get("userId") or "")
        except ValueError:
            user_id = ""
        if not user_id:
            self.log("登录失败：响应缺少用户 ID")
            raise ForumError("药丸登录响应缺少用户 ID")
        self.log(f"登录成功：userId={user_id}，remember Cookie 已下发")
        return {"remember": remember, "session": new_session,
                "cookies": {"flarum_remember": remember, "flarum_session": new_session},
                "csrf": login.headers.get("X-CSRF-Token") or csrf_token,
                "user_id": user_id}

    def signin(self) -> dict:
        """执行签到：会话准备 → 签到 → 回写变化的 remember Cookie。"""
        started = time.monotonic()
        prepared = self._prepare()
        result = self._do_checkin(prepared["user_id"], prepared["cookies"], prepared["csrf"])
        self._remember_cookie(prepared["remember"], prepared["session"])
        self.log(f"签到完成：{result['status']}，耗时 {time.monotonic() - started:.1f} 秒")
        return result

    def status(self) -> dict:
        """只读查询账号签到状态（不签到），供插件首页展示；站点无逐日奖励历史。"""
        started = time.monotonic()
        prepared = self._prepare()
        state = self._fetch_state(prepared["user_id"], prepared["cookies"], prepared["csrf"])
        if not state:
            raise ForumError("药丸签到状态查询失败，请稍后重试")
        self._remember_cookie(prepared["remember"], prepared["session"])
        checked_today = state.get("canCheckin") is False and self._is_today(state)
        self.log(f"状态查询完成，耗时 {time.monotonic() - started:.1f} 秒")
        return {"checked_today": checked_today,
                "points": state.get("money"),
                "streak": state.get("totalContinuousCheckIn"),
                "reward": None, "account": None, "recent": []}


class FengchaoClient:
    """蜂巢论坛（fengchao.chat）MoviePilot 集成 API 签到。

    Bearer Key 鉴权；响应封装 ``{code, message, data}``。GET/PUT 与 POST /check-in
    在可重试状态码（408/425/429/5xx）下做有限次传输重试，POST /check-in 服务端按
    用户与日期幂等。
    """

    BASE = "https://fengchao.chat"
    INTEGRATION = "/api/integrations/moviepilot/v1"
    RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
    TRANSPORT_DELAYS = (1.0, 3.0)
    # 内部重试可接受的站点最长等待；超过则不再内部重试，交给定时重试按站点要求等待。
    TRANSPORT_MAX_WAIT_SECONDS = 600
    CONNECT_TIMEOUT = 8
    READ_TIMEOUT = 20

    def __init__(self, api_key: str, instance_id: str, plugin_version: str,
                 moviepilot_version: str = "", proxy: str = "", log=None):
        self.api_key = str(api_key or "").strip()
        self.instance_id = str(instance_id or "")
        self.plugin_version = str(plugin_version or "")
        self.moviepilot_version = str(moviepilot_version or "")
        self.proxy = proxy or ""
        self.log = log or (lambda message: None)
        if not self.api_key:
            raise ForumError("未配置蜂巢 MP 专用 API Key，请在论坛“隐秘的角落”生成并填写")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"MoviePilot-ForumSignin/{self.plugin_version}",
            "X-MoviePilot-Instance-Id": self.instance_id,
            "X-MoviePilot-Plugin-Version": self.plugin_version,
            "X-MoviePilot-Version": self.moviepilot_version,
            "X-MoviePilot-Request-Id": f"{self.instance_id}-{uuid.uuid4().hex}",
        }

    @staticmethod
    def _local_backoff(attempt: int) -> float:
        """无 Retry-After 时的短退避。"""
        index = min(max(0, attempt), len(FengchaoClient.TRANSPORT_DELAYS) - 1)
        return FengchaoClient.TRANSPORT_DELAYS[index]

    def request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        retryable = method in ("GET", "PUT") or path.endswith("/check-in")
        max_attempts = 1 + len(self.TRANSPORT_DELAYS) if retryable else 1
        self.log(f"{method} {path}（可重试={retryable}，最多 {max_attempts} 次）")
        for attempt in range(max_attempts):
            started = time.monotonic()
            try:
                with session_factory() as session:
                    response = session.request(method, self.BASE + path, headers=self._headers(),
                                               json=payload, proxies=_proxy_dict(self.proxy),
                                               timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
                                               allow_redirects=False)
            except requests.RequestException as exc:
                error = ForumError("连接蜂巢论坛失败，请检查 DNS、网络和代理设置", retryable=retryable)
                if attempt + 1 >= max_attempts:
                    self.log(f"传输失败（第 {attempt + 1}/{max_attempts} 次）：{type(exc).__name__}，不再重试")
                    raise error from None
                delay = self._local_backoff(attempt)
                self.log(f"传输失败（第 {attempt + 1}/{max_attempts} 次）：{type(exc).__name__}，{delay:.1f} 秒后重试")
                time.sleep(delay + random.uniform(0, 0.5))
                continue
            elapsed = time.monotonic() - started
            if retryable and response.status_code in self.RETRYABLE_STATUS and attempt + 1 < max_attempts:
                header = str(response.headers.get("retry-after") or "").strip()
                wait = _retry_after_seconds(header) if header else 0
                if wait > self.TRANSPORT_MAX_WAIT_SECONDS:
                    # 站点要求等待超过内部上限：不提前重试，交给定时重试按站点要求等待。
                    self.log(f"HTTP {response.status_code} 要求等待 {wait} 秒，"
                             f"超过内部重试上限 {self.TRANSPORT_MAX_WAIT_SECONDS} 秒，转由定时重试执行")
                    raise ForumError(f"蜂巢 API 限流，站点要求等待 {wait} 秒",
                                     retryable=True, retry_after=wait)
                delay = float(wait) if wait else self._local_backoff(attempt)
                self.log(f"HTTP {response.status_code}（第 {attempt + 1}/{max_attempts} 次，"
                         f"Retry-After={header or '未提供'}），{delay:.1f} 秒后重试")
                time.sleep(delay + random.uniform(0, 0.5))
                continue
            try:
                result = response.json() or {}
            except ValueError:
                if retryable and attempt + 1 < max_attempts:
                    delay = self._local_backoff(attempt) + random.uniform(0, 0.5)
                    self.log(f"HTTP {response.status_code} 非 JSON 网关响应（第 {attempt + 1}/{max_attempts} 次），{delay:.1f} 秒后重试")
                    time.sleep(delay)
                    continue
                request_id = str(response.headers.get("x-request-id")
                                 or response.headers.get("x-trace-id")
                                 or response.headers.get("cf-ray") or "无")
                self.log(f"HTTP {response.status_code} 非 JSON 响应，请求标识={request_id}")
                raise ForumError(f"蜂巢论坛网关返回非 JSON 响应（HTTP {response.status_code}）") from None
            if response.status_code >= 400 or result.get("code") not in (None, 0):
                message = str(result.get("message") or f"蜂巢 API 请求失败（HTTP {response.status_code}）")
                self.log(f"请求失败：HTTP {response.status_code}，code={result.get('code')}，"
                         f"message={message[:120]}，耗时 {elapsed:.2f} 秒")
                # 限流时把站点要求的精确等待时间带给调度层重试（不封顶）。
                if response.status_code == 429:
                    raise ForumError(message[:200], retryable=True,
                                     retry_after=_retry_after_seconds(
                                         response.headers.get("Retry-After")))
                raise ForumError(message[:200])
            self.log(f"HTTP {response.status_code}，code={result.get('code')}，耗时 {elapsed:.2f} 秒")
            return result.get("data") or {}
        raise ForumError("蜂巢 API 请求未完成", retryable=retryable)

    def me(self) -> dict:
        return self.request("GET", self.INTEGRATION + "/me")

    @staticmethod
    def _parse_identity(identity: dict) -> dict:
        """解析 /me 身份卡：账号、积分、等级与近 30 天逐日签到奖励。"""
        status_data = identity.get("status") if isinstance(identity.get("status"), dict) else identity
        account = status_data.get("account") if isinstance(status_data.get("account"), dict) else {}
        summary = status_data.get("checkIn") if isinstance(status_data.get("checkIn"), dict) else {}
        recent = []
        for item in (account.get("recentCheckIns") or [])[:30]:
            if isinstance(item, dict) and item.get("date"):
                recent.append({"date": str(item.get("date"))[:10], "reward": item.get("reward")})
        return {
            "account": str(account.get("displayName") or account.get("username") or "") or None,
            "points": account.get("points"),
            "level_name": str(account.get("levelName") or "") or None,
            "level_icon": str(account.get("levelIcon") or "") or None,
            "streak": summary.get("currentStreak"),
            "today_reward": summary.get("todayReward"),
            "checked_today": bool(summary.get("checkedInToday")) if summary else None,
            "recent": recent,
        }

    def signin(self) -> dict:
        """执行签到；响应已含奖励与连续天数，再尽量补一次账号卡与论坛侧签到历史。"""
        data = self.request("POST", self.INTEGRATION + "/check-in", {})
        if not isinstance(data, dict) or not isinstance(data.get("alreadyCheckedIn"), bool):
            self.log(f"签到响应缺少 alreadyCheckedIn 字段："
                     f"{list(data.keys())[:8] if isinstance(data, dict) else type(data).__name__}")
            raise ForumError("蜂巢签到响应缺少签到结果字段，请到论坛核对签到状态")
        already = bool(data.get("alreadyCheckedIn"))
        summary = data.get("checkIn") if isinstance(data.get("checkIn"), dict) else {}
        result = {
            "status": "already" if already else "success",
            "message": "今日已签到" if already else "签到成功",
            "reward": summary.get("todayReward", data.get("reward")),
            "streak": data.get("currentStreak", summary.get("currentStreak")),
            "points": None, "account": None, "checked_today": True,
            "level_name": None, "level_icon": None, "recent": [], "today_reward": None,
        }
        try:
            profile = self._parse_identity(self.me())
            for key in ("account", "points", "level_name", "level_icon", "recent"):
                result[key] = profile[key]
            self.log(f"账号卡获取成功：{profile['account'] or '未知'}，"
                     f"积分={profile['points']}，近期签到 {len(profile['recent'])} 天")
        except ForumError as error:
            self.log(f"账号卡查询失败（不影响签到结果）：{error}")
        return result

    def status(self) -> dict:
        """只读获取账号卡与签到摘要（不签到），供插件首页展示。"""
        profile = self._parse_identity(self.me())
        if not profile["account"] and profile["points"] is None:
            raise ForumError("蜂巢未返回账号信息，请检查 API Key")
        return profile


def validate_hdblue_payload(payload: Any) -> dict:
    """白名单校验蓝影服务器载荷（对照上游 core.py，防畸形数据落库展示）。"""
    if not isinstance(payload, dict) or payload.get("protocolVersion") != 1:
        raise ForumError("蓝影接口版本不兼容，请更新插件")
    account = payload.get("account")
    date = payload.get("date")
    if not isinstance(account, dict) or not account.get("id") or not isinstance(date, str):
        raise ForumError("蓝影返回的数据不完整")
    try:
        if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
            raise ValueError()
    except ValueError:
        raise ForumError("蓝影返回的签到日期无效") from None
    if payload.get("timezone") != "Asia/Shanghai" or not isinstance(payload.get("alreadyCheckedIn"), bool):
        raise ForumError("蓝影返回的签到状态无效")
    for key in ("points", "reward", "currentStreak", "maxStreak"):
        if not _number(payload.get(key)):
            raise ForumError("蓝影返回的签到数值无效")
    raw_history = payload.get("history", [])
    if not isinstance(raw_history, list):
        raise ForumError("蓝影返回的签到记录无效")
    history = []
    for item in raw_history[:30]:
        if isinstance(item, dict) and isinstance(item.get("date"), str) and _number(item.get("reward")):
            history.append({"date": item["date"][:10], "reward": item["reward"]})
    return {
        "protocolVersion": 1, "timezone": "Asia/Shanghai", "date": date,
        "account": {key: str(account.get(key) or "")[:200]
                    for key in ("id", "username", "nickname", "avatarUrl")},
        "pointName": str(payload.get("pointName") or "积分")[:30],
        "points": payload["points"], "reward": payload["reward"],
        "alreadyCheckedIn": payload["alreadyCheckedIn"],
        "performedCheckIn": payload.get("performedCheckIn") is True,
        "currentStreak": payload["currentStreak"], "maxStreak": payload["maxStreak"],
        "history": history,
    }


class HDBlueClient:
    """蓝影论坛（hdblue.cc）MoviePilot 集成 API 签到。

    专用密钥 ``hdbmp_…`` 鉴权；GET /check-in 查询状态、POST /check-in 签到、GET /me
    取账号。POST 结果不确定（超时/畸形响应）时用 GET 复核，服务端按用户与日期幂等。
    """

    API_ROOT = "https://hdblue.cc/api/integrations/moviepilot/v1"
    KEY_PATTERN = re.compile(r"hdbmp_[A-Za-z0-9_-]{20,200}")
    CONNECT_TIMEOUT = 5
    READ_TIMEOUT = 15
    MAX_BODY_BYTES = 262144

    def __init__(self, api_key: str, proxy: str = "", log=None):
        self.api_key = str(api_key or "").strip()
        self.proxy = proxy or ""
        self.log = log or (lambda message: None)
        if not self.KEY_PATTERN.fullmatch(self.api_key):
            raise ForumError("蓝影签到密钥格式错误，应以 hdbmp_ 开头并在论坛个人设置生成")

    def _request(self, method: str, endpoint: str) -> dict:
        if method not in ("GET", "POST") or endpoint not in ("/me", "/check-in"):
            raise ForumError("不支持的蓝影请求")
        started = time.monotonic()
        self.log(f"{method} {endpoint}（代理={'开' if self.proxy else '关'}）")
        try:
            with session_factory() as session:
                response = session.request(
                    method, self.API_ROOT + endpoint,
                    headers={"Authorization": "Bearer " + self.api_key,
                             "Accept": "application/json",
                             "User-Agent": "MoviePilot-HDBlueSignin/1"},
                    json={} if method == "POST" else None,
                    timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
                    allow_redirects=False, proxies=_proxy_dict(self.proxy))
        except requests.RequestException as exc:
            self.log(f"{method} {endpoint} 传输异常：{type(exc).__name__}"
                     + ("（POST 结果不确定，需复核）" if method == "POST" else ""))
            raise ForumError("连接蓝影论坛超时或网络暂时不可用",
                             retryable=True, uncertain=method == "POST") from None
        status = response.status_code
        if not 200 <= status < 300:
            if status == 401:
                self.log("HTTP 401：签到密钥无效")
                raise ForumError("蓝影签到密钥已失效，请在论坛个人设置重新生成", auth=True)
            if status == 403:
                self.log("HTTP 403：站点拒绝签到（检查签到开放状态或账号权限）")
                raise ForumError("蓝影论坛当前不允许签到，请检查签到开放状态或账号权限")
            if status == 429:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                self.log(f"HTTP 429 限流：Retry-After={response.headers.get('Retry-After') or '未提供'}"
                         + (f"（解析为 {retry_after} 秒）" if retry_after else ""))
                raise ForumError("蓝影论坛请求过于频繁，稍后自动重试", retryable=True,
                                 retry_after=retry_after)
            if status >= 500:
                self.log(f"HTTP {status} 服务端错误")
                raise ForumError("蓝影论坛服务暂时不可用", retryable=True, uncertain=method == "POST")
            if 300 <= status < 400:
                self.log(f"HTTP {status} 发生跳转（不跟随）")
                raise ForumError("蓝影论坛地址发生跳转，已停止请求，请更新插件")
            self.log(f"HTTP {status} 拒绝请求")
            raise ForumError("蓝影论坛拒绝请求，请检查账号状态或更新插件")
        if len(response.content) > self.MAX_BODY_BYTES:
            self.log(f"HTTP {status} 响应过大：{len(response.content)} 字节")
            raise ForumError("蓝影响应超出大小限制", uncertain=method == "POST")
        try:
            envelope = response.json()
        except (ValueError, TypeError):
            self.log(f"HTTP {status} 响应非 JSON，{len(response.content)} 字节")
            raise ForumError("蓝影未返回有效的 JSON 数据", uncertain=method == "POST") from None
        if not isinstance(envelope, dict) or type(envelope.get("code")) is not int or envelope["code"] != 0:
            self.log(f"HTTP {status} 响应异常：code={envelope.get('code') if isinstance(envelope, dict) else '非对象'}"
                     f"，耗时 {time.monotonic() - started:.2f} 秒")
            raise ForumError("蓝影接口返回异常结果，请在论坛网页确认签到状态",
                             uncertain=method == "POST")
        self.log(f"HTTP {status}，code={envelope.get('code')}，{len(response.content)} 字节，"
                 f"耗时 {time.monotonic() - started:.2f} 秒")
        try:
            return validate_hdblue_payload(envelope.get("data"))
        except ForumError as error:
            # 2xx POST 的响应体异常时，请求可能已在服务端生效。
            error.uncertain = error.uncertain or method == "POST"
            raise

    def me(self) -> dict:
        return self._request("GET", "/me")

    def status(self) -> dict:
        """只读获取账号、余额与近 30 天签到奖励（不签到），供插件首页展示。"""
        payload = self._request("GET", "/me")
        checked_today = payload.get("date") == _beijing_today() and bool(payload.get("alreadyCheckedIn"))
        return self._profile(checked_today, payload)

    @staticmethod
    def _profile(checked_today: bool, payload: dict) -> dict:
        account = payload.get("account") or {}
        return {"checked_today": checked_today,
                "reward": payload.get("reward"), "points": payload.get("points"),
                "streak": payload.get("currentStreak"), "point_name": payload.get("pointName"),
                "max_streak": payload.get("maxStreak"),
                "account": str(account.get("nickname") or account.get("username") or "") or None,
                "recent": [{"date": item.get("date"), "reward": item.get("reward")}
                           for item in (payload.get("history") or [])[:30]
                           if isinstance(item, dict) and item.get("date")]}

    def signin(self) -> dict:
        today = _beijing_today()
        payload = self._request("GET", "/check-in")
        if payload.get("date") == today and payload.get("alreadyCheckedIn"):
            self.log(f"签到前查询：站点日期={payload.get('date')}，已签到，跳过提交")
            return {**self._profile(True, payload), "status": "already", "message": "今日已签到"}
        self.log(f"签到前查询：站点日期={payload.get('date')}，未签到，提交 POST /check-in")
        try:
            payload = self._request("POST", "/check-in")
        except ForumError as error:
            if not error.uncertain:
                raise
            self.log("POST 结果不确定，使用 GET /check-in 复核")
            try:
                verified = self._request("GET", "/check-in")
            except ForumError as verify_error:
                raise (verify_error if verify_error.auth else error) from None
            if verified.get("date") != today or not verified.get("alreadyCheckedIn"):
                self.log("复核未确认签到生效，按原错误处理")
                raise error from None
            self.log("复核确认：签到已在服务端生效")
            payload = verified
        if payload.get("date") != today or not payload.get("alreadyCheckedIn"):
            self.log(f"签到结果未确认：date={payload.get('date')}，alreadyCheckedIn={payload.get('alreadyCheckedIn')}")
            raise ForumError("蓝影签到结果待确认，请在论坛网页检查", uncertain=True)
        message = "已确认签到成功" if payload.get("performedCheckIn") else "今日已签到"
        self.log(f"签到结论：{message}，奖励={payload.get('reward')}，"
                 f"余额={payload.get('points')}，连续={payload.get('currentStreak')} 天")
        return {**self._profile(True, payload), "status": "success", "message": message}
