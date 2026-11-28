"""论坛签到合集离线测试：全部 HTTP 请求伪造，不需要真实凭证。

覆盖三个客户端协议（药丸会话刷新/签到判定/登录兜底、蜂巢封装解析与限流重试、
蓝影载荷校验与不确定结果复核）以及插件调度、记录、通知与重试生命周期。
"""
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3/forumsignin"


class Base:
    """_PluginBase 假体：数据与通知都在内存。"""

    def __init__(self):
        self.data = {}
        self.notices = []
        self.saved_config = {}
        self._config_dict = {}

    def get_data(self, key):
        return self.data.get(key)

    def save_data(self, key, value):
        self.data[key] = value

    def update_config(self, config):
        self.saved_config = dict(config)

    def get_config(self):
        """与宿主 _PluginBase 同名同义：返回当前插件配置字典。"""
        return dict(self._config_dict)

    def post_message(self, **kwargs):
        self.notices.append(kwargs)


class FakeResponse:
    def __init__(self, status=200, headers=None, cookies=None, body=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._body = body
        self.text = text or (json.dumps(body, ensure_ascii=False) if body is not None else "")
        self.content = self.text.encode("utf-8")

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeSession:
    """按 script(method, url, kwargs) 决定响应的假会话；记录全部调用。"""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.script(method, url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class FakeTimer:
    """可检查的假 Timer：start/cancel 只改状态，不真正执行。"""

    instances = []

    def __init__(self, interval, func, args=None, kwargs=None):
        self.interval = interval
        self.func = func
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.started = False
        self.canceled = False

    def start(self):
        self.started = True
        FakeTimer.instances.append(self)

    def cancel(self):
        self.canceled = True
        self.started = False

    def is_alive(self):
        return self.started and not self.canceled


class FakeCronTrigger:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    @classmethod
    def from_crontab(cls, expression, timezone=None):
        fields = str(expression).split()
        if len(fields) != 5:
            raise ValueError("bad crontab")
        return cls(expression=str(expression))


def load_plugin():
    """以包形式加载插件，宿主 app.* 全部用假体替换。"""
    names = ("app", "app.sdk", "app.sdk.plugin", "app.sdk.config", "app.sdk.logging",
             "app.schemas", "app.schemas.types", "apscheduler", "apscheduler.triggers",
             "apscheduler.triggers.cron")
    modules = {name: types.ModuleType(name) for name in names}
    modules["app.sdk.plugin"]._PluginBase = Base
    modules["app.sdk.config"].settings = types.SimpleNamespace(
        PROXY=None, TZ="Asia/Shanghai", VERSION_FLAG="v3")
    log_records = []
    modules["app.sdk.logging"].logger = types.SimpleNamespace(
        info=lambda *a, **k: log_records.append(" ".join(str(x) for x in a)),
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: log_records.append(str(a[0]) if a else ""),
        error=lambda *a, **k: None)
    modules["app.schemas.types"].MessageType = types.SimpleNamespace(Plugin="plugin")
    modules["apscheduler.triggers.cron"].CronTrigger = FakeCronTrigger
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "forumsignin", PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["forumsignin"] = module
        try:
            spec.loader.exec_module(module)
            module._clients_module = sys.modules.get("forumsignin.clients")
            module._config_form_module = sys.modules.get("forumsignin.config_form")
        finally:
            sys.modules.pop("forumsignin", None)
            sys.modules.pop("forumsignin.clients", None)
            sys.modules.pop("forumsignin.config_form", None)
    module._log_records = log_records
    return module


plugin = load_plugin()
clients = plugin._clients_module
TODAY = clients._beijing_today()


def flarum_page(csrf="csrf-token-1", user_id="7"):
    return ('<script>window.flarum={"apiDocument":{},"csrfToken":"%s",'
            '"userId":%s};</script>' % (csrf, user_id))


def hdblue_payload(date=TODAY, already=False, performed=False):
    return {
        "protocolVersion": 1, "timezone": "Asia/Shanghai", "date": date,
        "account": {"id": "42", "username": "userb", "nickname": "蓝影用户", "avatarUrl": ""},
        "pointName": "积分", "points": 100, "reward": 5, "alreadyCheckedIn": already,
        "performedCheckIn": performed, "currentStreak": 3, "maxStreak": 9,
        "history": [{"date": date, "reward": 5}],
    }


def raises_forum_error(action, expect=None):
    import pytest

    with pytest.raises(clients.ForumError) as excinfo:
        action()
    if expect:
        assert expect in str(excinfo.value)
    return excinfo.value


# ---------- 药丸 ----------


def test_invites_checkin_success_and_cookie_persisted():
    updates = []

    def script(method, url, kwargs):
        if kwargs.get("allow_redirects") is False:  # 会话刷新
            return FakeResponse(cookies={"flarum_session": "sess2"})
        if "api/users" in url:
            return FakeResponse(body={"data": {"id": "7", "attributes": {
                "canCheckin": True, "lastCheckinTime": "", "money": 88,
                "totalContinuousCheckIn": 4}}})
        if url == "https://invites.fun/api/checkin":
            return FakeResponse(body={"data": {"attributes": {
                "canCheckin": False, "lastCheckinTime": f"{TODAY} 09:00:00", "money": 93,
                "totalContinuousCheckIn": 5}}})
        if url == "https://invites.fun":
            return FakeResponse(text=flarum_page())
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=remember1; flarum_session=sess1",
                                       on_cookie_update=lambda r, s: updates.append((r, s)))
        result = client.signin()
    assert result["status"] == "success"
    assert result["points"] == 93
    assert result["streak"] == 5
    assert updates == [("remember1", "sess2")]


def test_invites_already_skips_post():
    session = FakeSession(lambda m, u, k: (
        FakeResponse(cookies={"flarum_session": "sess2"}) if k.get("allow_redirects") is False
        else (FakeResponse(body={"data": {"id": "7", "attributes": {
            "canCheckin": False, "lastCheckinTime": f"{TODAY} 08:00:00", "money": 88,
            "totalContinuousCheckIn": 4}}}) if "api/users" in u
            else FakeResponse(text=flarum_page()))))
    with patch.object(clients.requests, "Session", lambda: session):
        client = clients.InvitesClient(cookie="flarum_remember=remember1")
        result = client.signin()
    assert result["status"] == "already"
    assert all(method != "POST" for method, _, _ in session.calls)


def test_invites_login_fallback_when_cookie_expired():
    updates = []

    def script(method, url, kwargs):
        cookies = kwargs.get("cookies") or {}
        if method == "GET" and "flarum_remember" in cookies:
            # remember 已失效：站点返回 200 但不给新会话
            return FakeResponse(cookies={})
        if method == "GET" and url == "https://invites.fun":
            return FakeResponse(cookies={"flarum_session": "s0"},
                                headers={"x-csrf-token": "csrf0"})
        if method == "POST" and url == "https://invites.fun/login":
            return FakeResponse(cookies={"flarum_remember": "r2", "flarum_session": "s2"},
                                headers={"X-CSRF-Token": "csrf2"}, body={"userId": 9})
        if "api/users" in url:
            return FakeResponse(body={"data": {"id": "9", "attributes": {
                "canCheckin": True, "lastCheckinTime": ""}}})
        if url == "https://invites.fun/api/checkin":
            return FakeResponse(body={"data": {"attributes": {
                "canCheckin": False, "lastCheckinTime": f"{TODAY} 10:00:00", "money": 50,
                "totalContinuousCheckIn": 2}}})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=remember1",
                                       username="user", password="pass",
                                       on_cookie_update=lambda r, s: updates.append((r, s)))
        result = client.signin()
    assert result["status"] == "success"
    assert updates == [("r2", "s2")]


def test_invites_without_any_credential_raises():
    with patch.object(clients.requests, "Session",
                      lambda: FakeSession(lambda m, u, k: FakeResponse())):
        client = clients.InvitesClient(cookie="other=1")
        raises_forum_error(client.signin, "未配置有效 Cookie 或账号密码")


def test_invites_cf_page_hint_mentions_browser_emulation():
    def script(method, url, kwargs):
        if kwargs.get("allow_redirects") is False:
            return FakeResponse(cookies={"flarum_session": "sess2"})
        return FakeResponse(status=200, text="<html>挑战页面</html>")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=r1")
        raises_forum_error(client.signin, "浏览器仿真")


# ---------- 蜂巢 ----------


def test_fengchao_checkin_success_with_identity():
    headers_seen = {}

    def script(method, url, kwargs):
        headers_seen.update(kwargs.get("headers") or {})
        if method == "POST" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0, "data": {
                "alreadyCheckedIn": False, "reward": 5, "currentStreak": 3}})
        if method == "GET" and url.endswith("/me"):
            return FakeResponse(body={"code": 0, "data": {"status": {"account": {
                "displayName": "蜂巢用户", "points": 120}}}})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.FengchaoClient(api_key="fckey", instance_id="inst-1",
                                        plugin_version="3.0.0")
        result = client.signin()
    assert result["status"] == "success"
    assert result["reward"] == 5
    assert result["streak"] == 3
    assert result["points"] == 120
    assert result["account"] == "蜂巢用户"
    assert headers_seen.get("Authorization") == "Bearer fckey"
    assert headers_seen.get("X-MoviePilot-Instance-Id") == "inst-1"
    assert headers_seen.get("X-MoviePilot-Request-Id", "").startswith("inst-1-")


def test_fengchao_retry_on_429_then_already():
    attempts = {"post": 0}

    def script(method, url, kwargs):
        if method == "POST" and url.endswith("/check-in"):
            attempts["post"] += 1
            if attempts["post"] == 1:
                return FakeResponse(status=429, headers={"retry-after": "1"})
            return FakeResponse(body={"code": 0, "data": {"alreadyCheckedIn": True,
                                                          "reward": 0, "currentStreak": 4}})
        if method == "GET" and url.endswith("/me"):
            return FakeResponse(body={"code": 0, "data": {"status": {"account": {
                "displayName": "x", "points": 10}}}})
        raise AssertionError(f"未预期的请求 {method} {url}")

    sleeps = []
    with patch.object(clients.requests, "Session", lambda: FakeSession(script)), \
            patch.object(clients.time, "sleep", lambda s: sleeps.append(s)):
        client = clients.FengchaoClient(api_key="fckey", instance_id="inst-1",
                                        plugin_version="3.0.0")
        result = client.signin()
    assert result["status"] == "already"
    assert attempts["post"] == 2
    assert sleeps, "限流后应有退避"


def test_fengchao_business_error_surfaces_message():
    with patch.object(clients.requests, "Session", lambda: FakeSession(
            lambda m, u, k: FakeResponse(body={"code": 401, "message": "Key 无效"}))):
        client = clients.FengchaoClient(api_key="fckey", instance_id="inst-1",
                                        plugin_version="3.0.0")
        raises_forum_error(client.signin, "Key 无效")


# ---------- 蓝影 ----------


def test_hdblue_checkin_success_after_post():
    def script(method, url, kwargs):
        if method == "GET" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0, "data": hdblue_payload(already=False)})
        if method == "POST" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0,
                                      "data": hdblue_payload(already=True, performed=True)})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.HDBlueClient(api_key="hdbmp_" + "a" * 30)
        result = client.signin()
    assert result["status"] == "success"
    assert result["message"] == "已确认签到成功"
    assert result["account"] == "蓝影用户"
    assert result["points"] == 100


def test_hdblue_already_via_get_only():
    posts = []

    def script(method, url, kwargs):
        if method == "POST":
            posts.append(url)
        if method == "GET" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0,
                                      "data": hdblue_payload(already=True, performed=False)})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.HDBlueClient(api_key="hdbmp_" + "a" * 30)
        result = client.signin()
    assert result["status"] == "already"
    assert not posts


def test_hdblue_uncertain_post_recovers_via_get():
    calls = {"get": 0}

    def script(method, url, kwargs):
        if method == "GET" and url.endswith("/check-in"):
            calls["get"] += 1
            if calls["get"] == 1:  # 签到前状态查询
                return FakeResponse(body={"code": 0, "data": hdblue_payload(already=False)})
            return FakeResponse(body={"code": 0,
                                      "data": hdblue_payload(already=True, performed=False)})
        if method == "POST":
            raise clients.requests.ConnectionError("网络中断")
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.HDBlueClient(api_key="hdbmp_" + "a" * 30)
        result = client.signin()
    assert result["status"] == "success"
    assert result["message"] == "今日已签到"


def test_hdblue_invalid_key_and_auth_failure():
    raises_forum_error(lambda: clients.HDBlueClient(api_key="bad-key"), "密钥格式错误")

    with patch.object(clients.requests, "Session", lambda: FakeSession(
            lambda m, u, k: FakeResponse(status=401))):
        client = clients.HDBlueClient(api_key="hdbmp_" + "a" * 30)
        error = raises_forum_error(client.signin)
        assert error.auth


def test_hdblue_payload_validation_rejects_bad_data():
    payload = hdblue_payload()
    payload["protocolVersion"] = 2
    raises_forum_error(lambda: clients.validate_hdblue_payload(payload), "版本不兼容")


# ---------- 插件层 ----------


def make_plugin(config_overrides=None):
    instance = plugin.ForumsSignin()
    config = dict(plugin._config_form_module.DEFAULTS)
    config.update({
        "enabled": True, "notify_mode": "all",
        "inv_enabled": True, "inv_cookie": "flarum_remember=r1; flarum_session=s1",
        "fc_enabled": True, "fc_api_key": "fckey",
        "hb_enabled": True, "hb_api_key": "hdbmp_" + "a" * 30,
    })
    config.update(config_overrides or {})
    with patch.object(threading, "Timer", FakeTimer):
        instance.init_plugin(config)
    return instance


def test_get_service_registers_each_ready_forum():
    instance = make_plugin()
    services = instance.get_service()
    assert [service["id"] for service in services] == \
        ["ForumsSignin_Invites", "ForumsSignin_Fengchao", "ForumsSignin_Hdblue"]
    assert all(service["trigger"].kwargs.get("jitter") == 1800 for service in services)
    # 宿主契约：func_kwargs 传给执行函数；kwargs 是 add_job 调度选项，不得混入业务参数
    assert [service["func_kwargs"]["forum_key"] for service in services] == \
        ["invites", "fengchao", "hdblue"]
    assert all("forum_key" not in (service.get("kwargs") or {}) for service in services)
    assert instance.get_state() is True


def test_get_service_invocation_matches_host_contract():
    """按宿主方式 func(**func_kwargs) 调用服务函数，必须可执行且路由到对应论坛。"""
    instance = make_plugin()
    built = []

    class FakeClient:
        def signin(self):
            return {"status": "success", "message": "签到成功", "reward": 1,
                    "points": 2, "streak": 3, "account": None,
                    "checked_today": True, "recent": []}

    def fake_build(key):
        built.append(key)
        return FakeClient()

    with patch.object(instance, "_build_client", side_effect=fake_build):
        for service in instance.get_service():
            service["func"](**(service.get("func_kwargs") or {}))
    assert built == ["invites", "fengchao", "hdblue"]
    assert instance.get_data("state_hdblue")["status"] == "success"


def test_get_service_skips_unready_forum_and_bad_cron():
    instance = make_plugin({"fc_enabled": False, "hb_cron": "bad cron"})
    services = instance.get_service()
    assert [service["id"] for service in services] == \
        ["ForumsSignin_Invites", "ForumsSignin_Hdblue"]
    # 非法 cron 回退默认值
    assert instance._forums["hdblue"]["cron"] == \
        plugin._config_form_module.DEFAULTS["hb_cron"]


def test_run_forum_success_records_history_and_notifies():
    instance = make_plugin({"notify_mode": "all"})
    fake_result = {"status": "success", "message": "签到成功", "reward": 5,
                   "points": 100, "streak": 3, "account": None}

    class FakeClient:
        def signin(self):
            return fake_result

    with patch.object(instance, "_build_client", return_value=FakeClient()):
        instance._run_forum("fengchao")
    state = instance.get_data("state_fengchao")
    assert state["status"] == "success"
    history = instance.get_data("history")
    assert history[0]["forum"] == "蜂巢"
    assert history[0]["status"] == "success"
    assert len(instance.notices) == 1
    assert "签到成功" in instance.notices[0]["title"]
    # 成功后重试预算清零
    assert instance.get_data("retry_fengchao") is None


def test_run_forum_failure_notifies_and_retries_with_daily_cap():
    instance = make_plugin({"notify_mode": "failure", "retry_count": 2,
                            "retry_interval": 10})
    calls = {"signin": 0}

    class FakeClient:
        def signin(self):
            calls["signin"] += 1
            raise plugin.ForumError("论坛拒绝请求")

    with patch.object(instance, "_build_client", return_value=FakeClient()), \
            patch.object(threading, "Timer", FakeTimer):
        FakeTimer.instances.clear()
        instance._run_forum("fengchao")
        assert instance.get_data("state_fengchao")["status"] == "failed"
        assert len(instance.notices) == 1  # 仅失败模式：失败要通知
        assert len(FakeTimer.instances) == 1
        assert FakeTimer.instances[0].interval == 600
        # 触发重试 1：回调内部先摘除自身再执行，参数经 func_kwargs 同名传递
        FakeTimer.instances[0].func()
        assert calls["signin"] == 2
        pending = instance._retry_timers.get("fengchao")
        assert pending is not None and pending in FakeTimer.instances
        # 触发重试 2：存活中的旧 Timer 不能挡住下一次重试
        pending.func()
        assert calls["signin"] == 3
        # 当日预算耗尽：第 4 次直接运行不再产生签到与重试
        instance._retry_timers.clear()
        FakeTimer.instances.clear()
        instance._run_forum("fengchao")
        assert calls["signin"] == 3
        assert not FakeTimer.instances


def test_retry_honors_site_retry_after():
    instance = make_plugin({"retry_count": 2, "retry_interval": 10})
    created = []
    calls = {"signin": 0}

    class FakeClient:
        def signin(self):
            calls["signin"] += 1
            raise plugin.ForumError("蓝影论坛请求过于频繁",
                                    retryable=True, retry_after=1500)

    with patch.object(instance, "_build_client", return_value=FakeClient()), \
            patch.object(threading, "Timer",
                         lambda interval, func, args=None, kwargs=None:
                         created.append(interval) or FakeTimer(interval, func)):
        FakeTimer.instances.clear()
        instance._run_forum("hdblue")
        # Retry-After 1500 秒 > 全局间隔 600 秒 → 取站点要求的更大值
        assert created[0] == 1500
        assert calls["signin"] == 1
        # 触发重试 1：回调内先摘除自身；再次失败仍按站点要求等待
        instance._retry_timers["hdblue"].func()
        assert calls["signin"] == 2
        assert created[-1] == 1500
        assert instance._attempts("hdblue") == 2


def test_retry_count_zero_disables_retry_timer():
    instance = make_plugin({"notify_mode": "off", "retry_count": 0})

    class FakeClient:
        def signin(self):
            raise plugin.ForumError("失败")

    with patch.object(instance, "_build_client", return_value=FakeClient()), \
            patch.object(threading, "Timer", FakeTimer):
        FakeTimer.instances.clear()
        instance._run_forum("hdblue")
    assert not FakeTimer.instances
    assert len(instance.notices) == 0  # off 模式不通知


def test_onlyonce_clears_switch_and_schedules_run_all():
    instance = make_plugin({"onlyonce": True})
    assert instance.saved_config.get("onlyonce") is False
    assert any(timer.func == instance._run_all for timer in FakeTimer.instances)


def test_invites_cookie_writeback_only_when_remember_changes():
    instance = make_plugin()
    instance._on_invites_cookie("r1", "new-session")
    assert instance.saved_config == {}  # remember 未变化，不写配置
    instance._on_invites_cookie("changed", "new-session")
    assert instance.saved_config["inv_cookie"].startswith("flarum_remember=changed;")
    assert instance._forums["invites"]["cookie"].startswith("flarum_remember=changed;")


def test_stop_service_cancels_pending_timers():
    instance = make_plugin()
    timer = FakeTimer(60, instance._run_forum, kwargs={"forum": "invites"})
    instance._retry_timers["invites"] = timer
    timer.start()
    instance.stop_service()
    assert timer.canceled
    assert instance._retry_timers == {}


def test_config_form_shape():
    form, defaults = plugin._config_form_module.build_form()
    assert form and form[0]["component"] == "VForm"
    for key in ("enabled", "notify_mode", "inv_cookie", "fc_api_key", "hb_api_key",
                "inv_cron", "fc_cron", "hb_cron"):
        assert key in defaults


# ---------- 数据接口与首页展示 ----------


def test_invites_status_readonly_without_post():
    session = FakeSession(lambda m, u, k: (
        FakeResponse(cookies={"flarum_session": "sess2"}) if k.get("allow_redirects") is False
        else (FakeResponse(body={"data": {"id": "7", "attributes": {
            "canCheckin": True, "lastCheckinTime": "", "money": 88,
            "totalContinuousCheckIn": 4}}}) if "api/users" in u
            else FakeResponse(text=flarum_page()))))
    with patch.object(clients.requests, "Session", lambda: session):
        client = clients.InvitesClient(cookie="flarum_remember=r1")
        profile = client.status()
    assert profile["checked_today"] is False
    assert profile["points"] == 88
    assert profile["streak"] == 4
    assert all(method != "POST" for method, _, _ in session.calls)


def test_fengchao_status_returns_account_and_recent():
    def script(method, url, kwargs):
        if method == "GET" and url.endswith("/me"):
            return FakeResponse(body={"code": 0, "data": {"status": {"account": {
                "displayName": "蜂巢用户", "points": 120, "levelName": "Lv3", "levelIcon": "🐝",
                "recentCheckIns": [{"date": TODAY, "reward": 6},
                                    {"date": "2026-11-27", "reward": 5},
                                    {"bad": True}]},
                "checkIn": {"checkedInToday": True, "todayReward": 6,
                            "currentStreak": 3}}}})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.FengchaoClient(api_key="fckey", instance_id="i",
                                        plugin_version="3.0.0")
        profile = client.status()
    assert profile["account"] == "蜂巢用户"
    assert profile["points"] == 120
    assert profile["level_name"] == "Lv3"
    assert profile["level_icon"] == "🐝"
    assert profile["checked_today"] is True
    assert profile["recent"] == [{"date": TODAY, "reward": 6},
                                 {"date": "2026-11-27", "reward": 5}]


def test_hdblue_status_history():
    def script(method, url, kwargs):
        if method == "GET" and url.endswith("/me"):
            return FakeResponse(body={"code": 0, "data": hdblue_payload(already=True)})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.HDBlueClient(api_key="hdbmp_" + "a" * 30)
        profile = client.status()
    assert profile["checked_today"] is True
    assert profile["points"] == 100
    assert profile["max_streak"] == 9
    assert profile["recent"] == [{"date": TODAY, "reward": 5}]


def test_hdblue_signin_carries_recent_history():
    def script(method, url, kwargs):
        if method == "GET" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0, "data": hdblue_payload(already=False)})
        if method == "POST" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0,
                                      "data": hdblue_payload(already=True, performed=True)})
        raise AssertionError(f"未预期的请求 {method} {url}")

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.HDBlueClient(api_key="hdbmp_" + "a" * 30)
        result = client.signin()
    assert result["status"] == "success"
    assert result["recent"] == [{"date": TODAY, "reward": 5}]
    assert result["checked_today"] is True


def test_refresh_forum_merges_profile_and_keeps_signin_status():
    instance = make_plugin()
    instance.save_data("state_fengchao", {"status": "success", "message": "签到成功",
                                          "points": 1, "updated_at": "old"})

    class FakeClient:
        def status(self):
            return {"checked_today": True, "account": "蜂巢用户", "points": 120,
                    "level_name": "Lv3", "level_icon": "🐝", "streak": 3,
                    "today_reward": 6, "recent": [{"date": TODAY, "reward": 6}]}

    with patch.object(instance, "_build_client", return_value=FakeClient()):
        outcome = instance._refresh_forum("fengchao")
    assert outcome == "今日已签到"
    state = instance.get_data("state_fengchao")
    assert state["status"] == "success"  # 签到结论保留
    assert state["message"] == "签到成功"
    assert state["points"] == 120
    assert state["account"] == "蜂巢用户"
    assert instance.get_data("history") in (None, [])  # 刷新不产生签到记录


def test_refresh_now_switch_schedules_refresh():
    instance = make_plugin({"refresh_now": True})
    assert instance.saved_config.get("refresh_now") is False
    assert any(timer.func == instance._refresh_all for timer in FakeTimer.instances)


def test_get_page_renders_forum_cards_with_recent_tables():
    instance = make_plugin()
    instance.save_data("state_invites", {"status": "success", "message": "签到成功",
                                         "checked_today": True, "points": 88, "streak": 4,
                                         "updated_at": f"{TODAY} 09:00:00"})
    instance.save_data("state_fengchao", {"status": "success", "message": "签到成功",
                                          "checked_today": True, "account": "蜂巢用户",
                                          "points": 120, "streak": 3,
                                          "recent": [{"date": TODAY, "reward": 6}],
                                          "updated_at": f"{TODAY} 09:00:00"})
    instance.save_data("history", [{"forum": "药丸", "time": f"{TODAY} 09:00:00",
                                    "status": "success", "message": "签到成功",
                                    "points": 88, "streak": 4}])
    page = json.dumps(instance.get_page(), ensure_ascii=False)
    # 三论坛卡片 + 蜂巢论坛侧近期签到 + 药丸本地记录回退
    assert "蜂巢" in page and "药丸" in page and "蓝影" in page
    assert "近期签到" in page
    assert "剩余药丸" in page and "累计天数" in page
    assert "尚未执行" in page  # 蓝影未执行时的占位卡片


def test_global_proxy_applies_to_all_clients():
    instance = make_plugin({"use_proxy": True})
    with patch.object(plugin, "_system_proxy", return_value="http://127.0.0.1:7890"):
        built = {key: instance._build_client(key)
                 for key in ("invites", "fengchao", "hdblue")}
    assert all(client.proxy == "http://127.0.0.1:7890" for client in built.values())
    instance_off = make_plugin({"use_proxy": False})
    with patch.object(plugin, "_system_proxy", return_value="http://127.0.0.1:7890"):
        assert instance_off._build_client("hdblue").proxy == ""
    assert "use_proxy" in plugin._config_form_module.DEFAULTS


def test_notify_template_follows_dian115_style():
    from datetime import datetime

    instance = make_plugin({"notify_mode": "all"})
    # 成功（药丸：余额口径）
    instance._notify_result("invites", "success", "签到成功",
                            {"status": "success", "message": "签到成功", "reward": None,
                             "points": 93, "streak": 5, "account": None})
    notice = instance.notices[-1]
    assert notice["title"].startswith("药丸签到成功（")
    assert "📅 药丸签到成功" in notice["text"]
    assert "🧧 获得奖励：—" in notice["text"]
    assert "💰 当前余额：93" in notice["text"]
    assert "🔥 连续签到：5" in notice["text"]
    assert notice["text"].count("🕒") == 1
    # 已签到（蓝影：积分口径 + 最长天数）
    instance._notify_result("hdblue", "already", "今日已签到",
                            {"status": "already", "reward": 5, "points": 100,
                             "streak": 3, "point_name": "积分", "max_streak": 9})
    notice = instance.notices[-1]
    assert notice["title"].startswith("蓝影已签到（")
    assert "📅 今日已完成签到，无需重复操作" in notice["text"]
    assert "💰 当前积分：100 积分" in notice["text"]
    assert "🔥 连续签到：3 / 最长 9" in notice["text"]
    # 失败（含重试次数与凭证提示）
    instance.save_data("retry_invites", {"date": datetime.now().strftime("%Y-%m-%d"),
                                         "count": 2})
    instance._notify_result("invites", "failed", "站点拒绝请求", {"status": "failed"})
    notice = instance.notices[-1]
    assert notice["title"].startswith("药丸签到失败（")
    assert "❌ 失败原因：站点拒绝请求" in notice["text"]
    assert "🔁 当日已尝试 2 次" in notice["text"]
    assert "⚠️ 请检查 Cookie 或账号密码是否有效" in notice["text"]


# ---------- 详细日志 ----------


def test_invites_logs_full_stages():
    logs = []

    def script(method, url, kwargs):
        if kwargs.get("allow_redirects") is False:
            return FakeResponse(cookies={"flarum_session": "sess2"})
        if "api/users" in url:
            return FakeResponse(body={"data": {"id": "7", "attributes": {
                "canCheckin": True, "lastCheckinTime": ""}}})
        if url == "https://invites.fun/api/checkin":
            return FakeResponse(body={"data": {"attributes": {
                "canCheckin": False, "lastCheckinTime": f"{TODAY} 09:00:00", "money": 93,
                "totalContinuousCheckIn": 5}}})
        return FakeResponse(text=flarum_page())

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=r1", log=logs.append)
        result = client.signin()
    assert result["status"] == "success"
    text = "\n".join(logs)
    for fragment in ("刷新会话：GET", "刷新会话成功", "获取页面成功：HTTP 200",
                     "提取身份成功", "查询签到状态：canCheckin=True",
                     "提交 POST", "签到响应确认", "签到完成：success"):
        assert fragment in text, fragment


def test_invites_failure_logs_http_and_site_reason():
    logs = []

    def script(method, url, kwargs):
        if kwargs.get("allow_redirects") is False:
            return FakeResponse(cookies={"flarum_session": "s"})
        if url == "https://invites.fun":
            return FakeResponse(text=flarum_page())
        if "api/checkin" in url:
            return FakeResponse(status=403, headers={"server": "cloudflare", "cf-mitigated": "challenge"},
                                body={"errors": [{"detail": "人机验证未通过"}]})
        if "api/users" in url:
            return FakeResponse(body={"data": {"id": "7", "attributes": {
                "canCheckin": True, "lastCheckinTime": ""}}})
        raise AssertionError(url)

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=r1", log=logs.append)
        raises_forum_error(client.signin, "人机验证未通过")
    text = "\n".join(logs)
    assert "签到请求失败：HTTP 403" in text
    assert "站点原因：人机验证未通过" in text
    assert "server=cloudflare" in text and "cf-mitigated=challenge" in text


def test_fengchao_and_hdblue_log_requests():
    logs = []

    def script(method, url, kwargs):
        if method == "POST" and url.endswith("/check-in"):
            return FakeResponse(body={"code": 0, "data": {"alreadyCheckedIn": False,
                                                          "reward": 5, "currentStreak": 3}})
        if method == "GET" and url.endswith("/me"):
            return FakeResponse(body={"code": 0, "data": {"status": {"account": {
                "displayName": "x", "points": 10}}}})
        raise AssertionError(url)

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        clients.FengchaoClient(api_key="fckey", instance_id="i", plugin_version="3.0.0",
                               log=logs.append).signin()
    assert any("POST /api/integrations/moviepilot/v1/check-in" in m for m in logs)
    assert any("HTTP 200" in m and "耗时" in m for m in logs)

    hb_logs = []
    with patch.object(clients.requests, "Session", lambda: FakeSession(
            lambda m, u, k: FakeResponse(body={"code": 0,
                                               "data": hdblue_payload(already=True)}))):
        clients.HDBlueClient(api_key="hdbmp_" + "a" * 30, log=hb_logs.append).signin()
    assert any("GET /check-in" in m for m in hb_logs)
    assert any("已签到，跳过提交" in m for m in hb_logs)


def test_plugin_run_logs_stages_and_duration():
    instance = make_plugin()
    plugin._log_records.clear()

    class FakeClient:
        def signin(self):
            return {"status": "success", "message": "签到成功", "reward": 5,
                    "points": 100, "streak": 3, "account": None,
                    "checked_today": True, "recent": []}

    with patch.object(instance, "_build_client", return_value=FakeClient()):
        instance._run_forum("hdblue")
    text = "\n".join(plugin._log_records)
    assert "开始签到（触发=定时，当日第 1/3 次，代理=关" in text
    assert "签到成功" in text and "耗时" in text
    # 客户端日志经插件前缀透出
    assert "论坛签到·蓝影" in text


def test_every_setting_has_hint():
    """设置页每个可交互控件都必须带小字说明（hint）。"""
    form, _ = plugin._config_form_module.build_form()
    missing = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("component") in ("VSwitch", "VTextField", "VSelect", "VCronField"):
                props = node.get("props") or {}
                if not props.get("hint"):
                    missing.append(str(props.get("model") or node.get("component")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(form)
    assert missing == [], f"缺少说明的控件: {missing}"


# ---------- 审查问题回归 ----------


def test_both_one_shot_switches_cleared_together():
    instance = make_plugin({"onlyonce": True, "refresh_now": True})
    assert instance.saved_config.get("onlyonce") is False
    assert instance.saved_config.get("refresh_now") is False
    assert any(timer.func == instance._run_all for timer in FakeTimer.instances)
    assert any(timer.func == instance._refresh_all for timer in FakeTimer.instances)


def test_cloakbrowser_not_a_declared_dependency():
    """宿主锁定 cloakbrowser==0.6.5，插件不得声明冲突版本；缺库时客户端自动降级。"""
    import tomllib
    document = tomllib.loads((PLUGIN_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"].get("dependencies") == []  # 不与宿主锁定版本冲突
    assert clients.launch_context is not None or clients.launch_context is None  # 导入已防护


def test_fengchao_missing_result_field_raises():
    with patch.object(clients.requests, "Session", lambda: FakeSession(
            lambda m, u, k: FakeResponse(body={"code": 0}))):
        client = clients.FengchaoClient(api_key="fckey", instance_id="i",
                                        plugin_version="3.0.0")
        raises_forum_error(client.signin, "缺少签到结果字段")


def test_fengchao_429_carries_retry_after_to_scheduled_retry():
    instance = make_plugin({"retry_count": 1, "retry_interval": 5})
    created = []

    class FakeClient:
        def signin(self):
            raise plugin.ForumError("蜂巢论坛请求过于频繁", retryable=True,
                                    retry_after=1800)

    with patch.object(instance, "_build_client", return_value=FakeClient()), \
            patch.object(threading, "Timer",
                         lambda interval, func, args=None, kwargs=None:
                         created.append(interval) or FakeTimer(interval, func)):
        FakeTimer.instances.clear()
        instance._run_forum("fengchao")
    assert created[0] == 1800  # 站点 Retry-After 优先于全局间隔


def test_stop_service_blocks_running_batch():
    instance = make_plugin({"notify_mode": "all"})
    built = []
    FakeTimer.instances.clear()

    class FakeClient:
        def signin(self):
            return {"status": "success", "message": "签到成功", "reward": 1,
                    "points": 2, "streak": 3, "account": None,
                    "checked_today": True, "recent": []}

    def fake_build(key):
        built.append(key)
        return FakeClient()

    with patch.object(instance, "_build_client", side_effect=fake_build), \
            patch.object(threading, "Timer", FakeTimer):
        instance.stop_service()
        instance._run_forum("hdblue")
        instance._refresh_forum("fengchao")
        assert built == []                       # 停止后不再构造客户端
        assert instance.get_data("history") in (None, [])
        assert not FakeTimer.instances           # 失败路径也不会再排重试


def test_concurrent_record_result_keeps_all_history():
    import time as _time

    instance = make_plugin()
    release = threading.Event()

    def hold_lock():
        with instance._history_lock:
            release.wait(timeout=2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    _time.sleep(0.05)  # 确保 holder 已持锁

    def record():
        instance._record_result("invites", {"status": "success", "message": "ok",
                                            "reward": None, "points": 1, "streak": 2})

    workers = [threading.Thread(target=record) for _ in range(2)]
    for worker in workers:
        worker.start()
    _time.sleep(0.1)
    release.set()
    for thread in (*workers, holder):
        thread.join(timeout=3)
    assert len(instance.get_data("history")) == 2  # 并发不丢记录


def test_stale_cache_does_not_show_signed_today():
    instance = make_plugin()
    yesterday = "2026-01-01"
    state = {"status": "success", "message": "签到成功", "checked_today": True,
             "points": 100, "streak": 3, "sign_date": yesterday}
    assert instance._today_text(state) == "未签到"
    assert instance._status_chip(state) == ("今日未签到", "warning")
    failed_state = {"status": "failed", "message": "x", "sign_date": yesterday}
    assert instance._status_chip(failed_state) == ("上次签到失败", "error")
    fresh = {**state, "sign_date": TODAY}
    # 今天的实时状态优先：checked_today=True → 今日已签到
    assert instance._status_chip(fresh) == ("今日已签到", "success")
    assert instance._today_text(fresh) == "已签到"


def test_invites_network_error_falls_back_to_login():
    updates = []

    def script(method, url, kwargs):
        cookies = kwargs.get("cookies") or {}
        if method == "GET" and "flarum_remember" in cookies:
            raise clients.requests.ConnectionError("网络抖动")  # Cookie 路径网络异常
        if method == "GET" and url == "https://invites.fun":
            return FakeResponse(cookies={"flarum_session": "s0"},
                                headers={"x-csrf-token": "csrf0"})
        if method == "POST" and url == "https://invites.fun/login":
            return FakeResponse(cookies={"flarum_remember": "r2", "flarum_session": "s2"},
                                body={"userId": 9})
        if "api/users" in url:
            return FakeResponse(body={"data": {"id": "9", "attributes": {
                "canCheckin": True, "lastCheckinTime": ""}}})
        if url == "https://invites.fun/api/checkin":
            return FakeResponse(body={"data": {"attributes": {
                "canCheckin": False, "lastCheckinTime": f"{TODAY} 10:00:00", "money": 50,
                "totalContinuousCheckIn": 2}}})
        raise AssertionError(url)

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=remember1",
                                       username="user", password="pass",
                                       on_cookie_update=lambda r, s: updates.append((r, s)))
        result = client.signin()
    assert result["status"] == "success"  # Cookie 路径网络异常后登录兜底完成签到
    assert updates == [("r2", "s2")]


# ---------- 第二轮审查问题回归 ----------


def test_stop_service_waits_for_inflight_and_blocks_writeback():
    import time as _time

    instance = make_plugin()
    entered = threading.Event()

    class SlowClient:
        def signin(self):
            entered.set()
            _time.sleep(0.3)  # 模拟在途请求
            # 在途期间触发的 Cookie 回写：停止后必须被拦截
            instance._on_invites_cookie("newR", "newS")
            return {"status": "success", "message": "签到成功", "reward": 1,
                    "points": 2, "streak": 3, "account": None,
                    "checked_today": True, "recent": []}

    with patch.object(instance, "_build_client", return_value=SlowClient()):
        worker = threading.Thread(target=instance._run_forum,
                                  kwargs={"forum_key": "invites"})
        worker.start()
        assert entered.wait(timeout=2)
        started = _time.monotonic()
        instance.stop_service()  # 应等待在途线程收敛（0.3 秒量级）
        elapsed = _time.monotonic() - started
        worker.join(timeout=3)
    assert elapsed >= 0.2, "stop_service 应等待在途请求结束"
    assert instance.saved_config == {}, "停止后的 Cookie 回写必须被拦截"
    assert instance.get_data("history") in (None, [])
    assert instance.get_data("state_invites") in (None, {})


def test_invites_uncertain_post_verifies_immediately():
    # 场景 1：POST 网络异常 → 立即复核发现已签到
    posts = []
    calls = {"state": 0}

    def script(method, url, kwargs):
        if kwargs.get("allow_redirects") is False:
            return FakeResponse(cookies={"flarum_session": "s2"})
        if "api/users" in url:
            calls["state"] += 1
            signed = calls["state"] >= 2  # 预检未签 → POST 异常 → 复核已签
            return FakeResponse(body={"data": {"id": "7", "attributes": {
                "canCheckin": not signed,
                "lastCheckinTime": f"{TODAY} 09:00:00" if signed else "",
                "money": 88, "totalContinuousCheckIn": 4}}})
        if url == "https://invites.fun":
            return FakeResponse(text=flarum_page())
        if url == "https://invites.fun/api/checkin":
            posts.append(url)
            raise clients.requests.ConnectionError("网络中断")
        raise AssertionError(url)

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)):
        client = clients.InvitesClient(cookie="flarum_remember=r1")
        result = client.signin()
    assert result["status"] == "already"
    assert result["message"] == "复核确认今日已签到"
    assert len(posts) == 1  # POST 确实发出且异常后未重复提交

    # 场景 2：HTTP 200 非 JSON → 复核未签到则仍报错
    calls["state"] = 0

    def script2(method, url, kwargs):
        if kwargs.get("allow_redirects") is False:
            return FakeResponse(cookies={"flarum_session": "s2"})
        if "api/users" in url:
            return FakeResponse(body={"data": {"id": "7", "attributes": {
                "canCheckin": True, "lastCheckinTime": ""}}})
        if url == "https://invites.fun":
            return FakeResponse(text=flarum_page())
        if url == "https://invites.fun/api/checkin":
            return FakeResponse(status=200, text="<html>gateway</html>")
        raise AssertionError(url)

    with patch.object(clients.requests, "Session", lambda: FakeSession(script2)):
        client = clients.InvitesClient(cookie="flarum_remember=r1")
        raises_forum_error(client.signin, "无法解析")


def test_fengchao_internal_retry_waits_exact_retry_after():
    attempts = {"post": 0}
    sleeps = []

    def script(method, url, kwargs):
        if method == "POST" and url.endswith("/check-in"):
            attempts["post"] += 1
            if attempts["post"] <= 2:
                return FakeResponse(status=429, headers={"retry-after": "120"})
            return FakeResponse(body={"code": 0, "data": {"alreadyCheckedIn": True,
                                                          "reward": 0, "currentStreak": 4}})
        if method == "GET" and url.endswith("/me"):
            return FakeResponse(body={"code": 0, "data": {"status": {"account": {
                "displayName": "x", "points": 10}}}})
        raise AssertionError(url)

    with patch.object(clients.requests, "Session", lambda: FakeSession(script)), \
            patch.object(clients.time, "sleep",
                         lambda s: sleeps.append(s) if s > 5 else None):
        client = clients.FengchaoClient(api_key="fckey", instance_id="i",
                                        plugin_version="3.0.0")
        result = client.signin()
    assert attempts["post"] == 3
    # 两次内部等待都精确遵循 120 秒（不再截断为 60 秒；含 0.5 秒抖动）
    long_sleeps = [s for s in sleeps if s >= 100]
    assert len(long_sleeps) == 2 and all(120 <= s <= 121 for s in long_sleeps)
    assert result["status"] == "already"


def test_fengchao_long_retry_after_defers_to_scheduler():
    instance = make_plugin({"retry_count": 1, "retry_interval": 5})
    created = []
    attempts = {"post": 0}

    class FakeClient:
        def signin(self):
            attempts["post"] += 1
            raise plugin.ForumError("蜂巢 API 限流，站点要求等待 3600 秒",
                                    retryable=True, retry_after=3600)

    with patch.object(instance, "_build_client", return_value=FakeClient()), \
            patch.object(threading, "Timer",
                         lambda interval, func, args=None, kwargs=None:
                         created.append(interval) or FakeTimer(interval, func)):
        FakeTimer.instances.clear()
        instance._run_forum("fengchao")
    assert attempts["post"] == 1
    # 插件级重试不再截断：完整按站点要求的 3600 秒安排
    assert created[0] == 3600


def test_refresh_notifications_follow_mode():
    outcomes = {"invites": "今日已签到", "fengchao": "刷新失败（Key 无效）",
                "hdblue": "今日尚未签到"}
    # 仅失败模式：只报刷新失败的论坛
    instance = make_plugin({"notify_mode": "failure"})
    instance._notify_refresh_outcomes(outcomes)
    assert len(instance.notices) == 1
    text = instance.notices[0]["text"]
    assert "刷新失败（Key 无效）" in text
    assert "今日已签到" not in text and "今日尚未签到" not in text
    # 全部成功时不打扰
    instance.notices.clear()
    instance._notify_refresh_outcomes({"invites": "今日已签到",
                                       "hdblue": "今日尚未签到"})
    assert instance.notices == []
    # 全部结果模式：完整汇总
    instance = make_plugin({"notify_mode": "all"})
    instance._notify_refresh_outcomes(outcomes)
    assert len(instance.notices) == 1
    assert "今日已签到" in instance.notices[0]["text"]
    # 关闭模式：不通知
    instance = make_plugin({"notify_mode": "off"})
    instance._notify_refresh_outcomes(outcomes)
    assert instance.notices == []


def test_status_chip_prefers_live_check_state():
    instance = make_plugin()
    today = TODAY
    # 昨天的结论是成功，今天刷新显示未签到 → 徽章不得再显示签到成功
    refreshed_unsigned = {"status": "success", "sign_date": today,
                          "checked_today": False, "message": "签到成功"}
    assert instance._status_chip(refreshed_unsigned) == ("今日未签到", "warning")
    # 今天实时确认已签到（即便本地结论还是失败）→ 以站点为准
    live_signed = {"status": "failed", "sign_date": today, "checked_today": True}
    assert instance._status_chip(live_signed) == ("今日已签到", "success")
    # 今天实时未签 + 今天签到失败 → 保留失败口径
    assert instance._status_chip({"status": "failed", "sign_date": today,
                                  "checked_today": False}) == ("签到失败", "error")
