"""HDHaven 栖影签到离线测试：HTTP 全伪造、无需真实凭证。

经生产命名空间 ``app.plugins.hdhavensign`` 导入插件，复用宿主引导的真实
设置、消息类型、Cron 解析与插件数据库（conftest 已隔离 CONFIG_DIR）；
外部 HTTP 以假会话替换，Cookie 解析、调度返回约定、账号隔离、竞态同步
与 Browserless 脚本行为均有覆盖。
"""
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta
from json import dumps as jdumps
from unittest.mock import Mock

import pytest

import app.plugins.hdhavensign as plugin_module
from app.plugins.hdhavensign import HDHavenSign

NODE = shutil.which("node")
TZ = "Asia/Shanghai"
DATA_KEYS = ("last_result", "sign_records", "pending_attempts", "last_error")


class Response:
    def __init__(self, status, data, content_type="application/json"):
        self.status_code = status
        self.data = data
        self.headers = {"content-type": content_type}

    def json(self):
        return self.data


SUCCESS = Response(201, {"success": True, "data": {"mode": "normal", "earned": 5,
                                                   "points": 25, "checkin_streak": 1}})
ACCOUNT = Response(200, {"success": True, "data": {"id": 1, "checkin_today": False}})


class Session:
    def __init__(self, account=ACCOUNT, result=SUCCESS):
        self.account = account
        self.result = result
        self.posts = []
        self.paths = []
        self.closed = False

    def get(self, path, **kwargs):
        self.paths.append(path)
        return self.account

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


class FakeJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain="", path="/", secure=False):
        self.items.append((name, value, domain, path, secure))


class FakeCurlSession:
    def __init__(self, impersonate=None, **kwargs):
        self.impersonate = impersonate
        self.trust_env = kwargs.get("trust_env")
        self.allow_redirects = kwargs.get("allow_redirects")
        self.proxies = {}
        self.headers = {}
        self.cookies = FakeJar()


def _account(account_id=1, **extra):
    return Response(200, {"success": True, "data": {"id": account_id, **extra}})


@pytest.fixture()
def notices():
    return []


@pytest.fixture()
def clean_plugin(notices):
    """未初始化的全新插件实例：数据键清空，通知与配置持久化替换为测试桩。"""
    plugin = HDHavenSign()
    plugin.post_message = lambda **kwargs: notices.append(kwargs)
    plugin.update_config = lambda config: None
    for key in DATA_KEYS:
        plugin.del_data(key)
    yield plugin
    for timer in getattr(plugin, "_timers", []):
        timer.cancel()


@pytest.fixture()
def plugin(clean_plugin):
    clean_plugin.init_plugin({"enabled": True, "cookie": "hdh_session=placeholder",
                              "notify": False})
    return clean_plugin


# ---------- 生命周期与调度合同 ----------


def test_lifecycle_and_defaults(clean_plugin):
    clean_plugin.init_plugin({})
    form, defaults = clean_plugin.get_form()
    assert form
    assert defaults["enabled"] is False
    assert clean_plugin.get_service() == []
    assert clean_plugin.get_state() is False
    assert clean_plugin.get_api() == []
    assert clean_plugin.get_page()
    clean_plugin.stop_service()
    clean_plugin.init_plugin({"enabled": True, "cookie": "hdh_session=placeholder",
                              "notify": False})
    assert clean_plugin.get_state() is True
    assert len(clean_plugin.get_service()) == 1
    assert clean_plugin.get_service()[0]["func"].__name__ == "_service_run"


def test_service_run_returns_host_tuple(plugin):
    session = Session()
    plugin._session = lambda *a, **k: session
    ok = plugin._service_run()
    assert isinstance(ok, tuple)
    assert ok[0] is True
    assert ok[1]
    session.account = Response(403, {"success": False})
    bad = plugin._service_run()
    assert isinstance(bad, tuple)
    assert bad[0] is False
    assert bad[1]


def test_stop_service_reports_unconverged_tasks(plugin, monkeypatch):
    plugin.STOP_WAIT_SECONDS = 0.2
    assert plugin._lock.acquire()
    try:
        fake_logger = Mock()
        monkeypatch.setattr(plugin_module, "logger", fake_logger)
        # 宿主只把字面量 False 视为未收敛：超时必须显式返回 False。
        assert plugin.stop_service() is False
        assert "未收敛" in str(fake_logger.warning.call_args_list)
    finally:
        plugin._lock.release()
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    assert plugin.stop_service() is True
    assert "未收敛" not in str(fake_logger.warning.call_args_list)


def test_reinit_cancels_previous_once_timer(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "onlyonce": True})
    assert len(clean_plugin._timers) == 1
    first = clean_plugin._timers[0]
    first_generation = clean_plugin._context.generation
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "onlyonce": True})
    assert first.finished.is_set()
    assert len(clean_plugin._timers) == 1
    assert clean_plugin._timers[0] is not first
    assert clean_plugin._context.generation == first_generation + 1


def test_cancelled_run_never_posts(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    session = Session()
    clean_plugin._session = lambda *a, **k: session
    clean_plugin._context.cancel.set()
    result = clean_plugin._run(manual=True)
    assert result["success"] is False
    assert result["message"] == "配置已变更或插件已停止，签到未提交"
    assert len(session.paths) == 1
    assert session.posts == []
    assert session.closed is True
    assert clean_plugin.get_data("last_result") is None


def test_reload_during_preflight_aborts_stale_run(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    old_ctx = clean_plugin._context
    seen = []
    session = Session()
    entered = threading.Event()
    gate = threading.Event()

    def get_hook(path, **kwargs):
        entered.set()
        gate.wait(2)
        return session.account

    session.get = get_hook

    def make_session(cfg=None):
        seen.append(cfg)
        return session

    clean_plugin._session = make_session
    runner = threading.Thread(target=clean_plugin._run, kwargs={"manual": True})
    runner.start()
    try:
        assert entered.wait(2)
        clean_plugin.init_plugin({"cookie": "hdh_session=new-cookie", "notify": False})
        gate.set()
    finally:
        gate.set()
        runner.join(5)
    assert runner.is_alive() is False
    # 运行全程使用捕获的旧代上下文：不会混入新 Cookie，且被换代取消中止。
    assert seen[0] is old_ctx.config
    assert seen[0].cookie == "hdh_session=placeholder"
    assert session.posts == []
    assert clean_plugin.get_data("last_result") is None
    assert clean_plugin._context.generation == old_ctx.generation + 1
    assert clean_plugin._context.config.cookie == "hdh_session=new-cookie"


def test_reload_waits_for_inflight_commit(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    session = Session()
    clean_plugin._session = lambda *a, **k: session
    post_started = threading.Event()
    release_post = threading.Event()
    posts = []

    def post_hook(*args, **kwargs):
        post_started.set()
        posts.append(args)
        release_post.wait(3)
        return SUCCESS

    session.post = post_hook
    init_done = []
    runner = threading.Thread(target=clean_plugin._run, kwargs={"manual": True})
    runner.start()
    try:
        assert post_started.wait(2)
        reloader = threading.Thread(
            target=lambda: (clean_plugin.init_plugin({"cookie": "hdh_session=new-cookie",
                                                      "notify": False}), init_done.append(True)))
        reloader.start()
        time.sleep(0.3)
        # POST 在提交临界区内：重载必须等待，不能插入中止或换代。
        assert init_done == []
    finally:
        release_post.set()
    runner.join(5)
    reloader.join(5)
    assert init_done == [True]
    assert len(posts) == 1
    assert clean_plugin.get_data("last_result")["state"] == "success"
    assert clean_plugin._context.config.cookie == "hdh_session=new-cookie"


def test_run_restarts_on_midnight_rollover(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    times = [datetime(2026, 1, 1, 23, 59, 59), datetime(2026, 1, 1, 23, 59, 59),
             datetime(2026, 1, 2, 0, 0, 1), datetime(2026, 1, 2, 0, 0, 1),
             datetime(2026, 1, 2, 0, 0, 2), datetime(2026, 1, 2, 0, 0, 2),
             datetime(2026, 1, 2, 0, 0, 3)]
    state = {"i": 0}

    def fake_now():
        if state["i"] < len(times) - 1:
            state["i"] += 1
        return times[state["i"]]

    clean_plugin._now = fake_now
    session = Session()
    clean_plugin._session = lambda *a, **k: session
    result = clean_plugin._run(manual=True)
    assert result["success"] is True
    assert len(session.posts) == 1
    # 重启后按新日期预检、落库：pending、确认结果与历史全部归属新的一天
    assert clean_plugin.get_data("last_result")["date"] == "2026-01-02"
    assert clean_plugin.get_data("pending_attempts") == []
    assert clean_plugin.get_data("sign_records")[0]["date"] == "2026-01-02"


def test_run_aborts_on_second_rollover(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    days = itertools.cycle([datetime(2026, 1, 1, 23, 59, 59), datetime(2026, 1, 2, 0, 0, 1)])
    clean_plugin._now = lambda: next(days)
    session = Session()
    clean_plugin._session = lambda *a, **k: session
    result = clean_plugin._run(manual=True)
    assert result["success"] is False
    assert result["message"] == "宿主日期连续跨日，本次未提交签到"
    assert session.posts == []
    assert clean_plugin.get_data("last_result") is None
    assert clean_plugin.get_data("sign_records") is None


def test_cron_validation_falls_back_to_default(clean_plugin):
    from apscheduler.triggers.cron import CronTrigger

    clean_plugin.init_plugin({"enabled": True, "cookie": "hdh_session=placeholder",
                              "notify": False, "cron": "not-a-cron"})
    assert clean_plugin._cron == clean_plugin.DEFAULT_CRON
    # 回退后的默认 Cron 是合法值：服务照常注册，不出现“启用但无任务”。
    service = clean_plugin.get_service()
    assert len(service) == 1
    expected = CronTrigger.from_crontab(clean_plugin.DEFAULT_CRON)
    assert str(service[0]["trigger"]) == str(expected)


def test_cron_trigger_uses_host_timezone(clean_plugin):
    clean_plugin.init_plugin({"enabled": True, "cookie": "hdh_session=placeholder",
                              "notify": False, "cron": "15 8 * * *"})
    service = clean_plugin.get_service()
    assert len(service) == 1
    assert str(service[0]["trigger"].timezone) == TZ


# ---------- 会话构建与代理 ----------


@pytest.mark.parametrize("proxy,expected", [
    ({"https": "http://example:1", "http": "http://example:2"}, "http://example:1"),
    ({"http": "http://example:2"}, "http://example:2"),
    (types.SimpleNamespace(host="http://example:3"), "http://example:3"),
])
def test_proxy_shapes(monkeypatch, proxy, expected):
    monkeypatch.setattr(plugin_module, "settings", types.SimpleNamespace(PROXY=proxy))
    monkeypatch.setenv("PROXY_HOST", "")
    monkeypatch.setenv("https_proxy", "")
    monkeypatch.setenv("HTTPS_PROXY", "")
    assert HDHavenSign._proxy_from_settings() == expected


@pytest.mark.parametrize("proxy,expected", [
    ({"https": "http://example:1", "http": "http://example:2"}, "http://example:1"),
    (types.SimpleNamespace(host="http://example:3"), "http://example:3"),
    (None, "http://example:4"),
])
def test_session_applies_system_proxy_to_curl_cffi(clean_plugin, monkeypatch,
                                                   proxy, expected):
    monkeypatch.setattr(plugin_module, "settings", types.SimpleNamespace(PROXY=proxy))
    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeCurlSession))
    clean_plugin.init_plugin({"enabled": True, "cookie": "hdh_session=placeholder",
                              "notify": False})
    monkeypatch.setenv("PROXY_HOST", "http://example:4")
    monkeypatch.setenv("https_proxy", "")
    monkeypatch.setenv("HTTPS_PROXY", "")
    session = clean_plugin._session()
    assert session.proxies == {"http": expected, "https": expected}
    assert session.impersonate == "chrome110"


def test_session_disables_env_proxy_and_uses_cookie_jar(clean_plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeCurlSession))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    session = clean_plugin._session()
    assert session.trust_env is False
    assert session.allow_redirects is False
    assert ("hdh_session", "placeholder", "hdhaven.com", "/", True) in session.cookies.items
    assert "Cookie" not in session.headers


def test_session_snapshot_is_used_not_live_fields(clean_plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeCurlSession))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    cfg = clean_plugin._context.config
    session = clean_plugin._session(cfg)
    assert ("hdh_session", "placeholder", "hdhaven.com", "/", True) in session.cookies.items
    other = plugin_module._RunConfig(cookie="hdh_session=other-cookie")
    session2 = clean_plugin._session(other)
    assert ("hdh_session", "other-cookie", "hdhaven.com", "/", True) in session2.cookies.items


def test_session_logs_proxy_choice_without_address(clean_plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "settings",
                        types.SimpleNamespace(PROXY={"https": "http://user:secret@example:1"}))
    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeCurlSession))
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    clean_plugin._session()
    assert "系统" in str(fake_logger.info.call_args_list)
    assert "secret" not in str(fake_logger.info.call_args_list)
    assert "example" not in str(fake_logger.info.call_args_list)
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_system_proxy": False, "proxy": "http://user:secret@example:2"})
    clean_plugin._session()
    assert "自定义" in str(fake_logger.info.call_args_list)
    assert "example" not in str(fake_logger.info.call_args_list)
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_system_proxy": False})
    clean_plugin._session()
    assert "直连" in str(fake_logger.info.call_args_list)
    monkeypatch.setattr(HDHavenSign, "_proxy_from_settings", Mock(return_value=""))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    clean_plugin._session()
    assert "将直连" in str(fake_logger.warning.call_args_list)


# ---------- Cookie 解析与预检诊断 ----------


def test_cookie_parsing_normalizes_prefix(plugin):
    assert plugin._parse_cookie("Cookie: hdh_session=placeholder; cf_clearance=placeholder") == \
        "hdh_session=placeholder; cf_clearance=placeholder"


@pytest.mark.parametrize("value", ["", "placeholder", "cf_clearance=placeholder",
                                   "session=placeholder", "hdh_session=placeholder\nInjected: x"])
def test_cookie_parsing_rejects_invalid(plugin, value):
    with pytest.raises(ValueError):
        plugin._parse_cookie(value)


def test_cookie_names_logged_without_values(clean_plugin, monkeypatch):
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    cookie = clean_plugin._parse_cookie(
        "Cookie: hdh_session=cookie-secret; cf_clearance=clear-secret")
    assert "hdh_session" in cookie
    assert "cf_clearance" in cookie
    logged = str(fake_logger.info.call_args_list)
    assert "cf_clearance" in logged
    assert "hdh_session" in logged
    assert "cookie-secret" not in logged
    assert "clear-secret" not in logged


def test_preflight_rejection_logs_site_reason_without_secrets(plugin, monkeypatch):
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    response = Response(403, {"success": False, "code": 4001, "message": "登录状态已失效"})
    response.history = [object()]
    session = Session(account=response)
    plugin._session = lambda *a, **k: session
    result = plugin._run(manual=True)
    assert result["success"] is False
    assert session.posts == []
    logged = str(fake_logger.info.call_args_list) + str(fake_logger.warning.call_args_list)
    assert "403" in logged
    assert "JSON" in logged
    assert "登录预检" in logged
    assert "4001" in logged
    assert "登录状态已失效" in logged
    assert "placeholder" not in logged


def test_session_verify_required_maps_to_clear_guidance(plugin):
    session = Session(account=Response(403, {"success": False,
                                             "code": "SESSION_VERIFY_REQUIRED",
                                             "message": "网络环境已变化，请完成人机验证后继续"}))
    plugin._session = lambda *a, **k: session
    result = plugin._run(manual=True)
    assert result["success"] is False
    assert session.posts == []
    assert "网络环境变化" in result["message"]
    assert "人机验证" in result["message"]
    assert "代理节点" in result["message"]


def test_non_json_preflight_logs_response_type_without_body(plugin, monkeypatch):
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    session = Session(account=Response(403, "private-challenge-html", "text/html"))
    plugin._session = lambda *a, **k: session
    result = plugin._run(manual=True)
    assert "可能遭遇人机验证" in result["message"]
    assert "非JSON" in str(fake_logger.info.call_args_list)
    assert "站点拒绝" in str(fake_logger.warning.call_args_list)
    assert session.posts == []
    all_logs = str(fake_logger.info.call_args_list) + str(fake_logger.warning.call_args_list)
    assert "private-challenge-html" not in all_logs


def test_preflight_request_exception_logs_stage_not_url(plugin, notices, monkeypatch):
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    session = Session()
    session.get = Mock(side_effect=RuntimeError("private-proxy-url"))
    plugin._session = lambda *a, **k: session
    result = plugin._run(manual=True)
    assert "登录预检" in str(fake_logger.warning.call_args_list)
    assert "private-proxy-url" not in str(fake_logger.warning.call_args_list)
    assert "private-proxy-url" not in result["message"]
    assert "private-proxy-url" not in str(notices)
    assert session.posts == []


# ---------- 解析与提交门 ----------


def test_parser_accepts_site_success(plugin):
    assert plugin._interpret(SUCCESS)["earned"] == 5


@pytest.mark.parametrize("status,data,content_type", [
    (409, {"success": False, "message": "unknown"}, "application/json"),
    (200, "html", "text/html"),
    (401, {"success": False}, "application/json"),
    (201, {"success": True, "data": {"mode": "normal"}}, "application/json"),
])
def test_parser_rejects_unconfirmed(plugin, status, data, content_type):
    with pytest.raises(RuntimeError):
        plugin._interpret(Response(status, data, content_type))


@pytest.mark.parametrize("key", ["earned", "points", "checkin_streak"])
def test_parser_rejects_bool_numbers(plugin, key):
    data = {"mode": "normal", "earned": 5, "points": 25, "checkin_streak": 1}
    data[key] = True
    with pytest.raises(RuntimeError):
        plugin._interpret(Response(201, {"success": True, "data": data}))


def test_success_empty_body_and_duplicate_gate(plugin):
    session = Session()
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is True
    assert session.paths == [plugin.BASE + "/api/auth/me"]
    assert len(session.posts) == 1
    assert session.posts[0] == ((plugin.BASE + "/api/account/checkin",), {"timeout": 20})
    assert session.closed is True
    assert plugin.get_data("last_result")["state"] == "success"
    session.account = _account(checkin_today=True,
                               last_checkin=datetime.now().date().isoformat())
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1


def test_failed_marker_write_never_posts(plugin):
    session = Session()
    plugin._session = lambda *a, **k: session
    plugin.save_data = lambda key, value: None
    assert plugin._run(manual=True)["success"] is False
    assert session.posts == []
    assert session.closed is True


def test_unknown_business_response_stays_pending(plugin):
    session = Session(result=Response(409, {"success": False, "message": "unknown"}))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is False
    assert plugin.get_data("last_result")["state"] == "pending"
    assert len(session.posts) == 1


def test_ambiguous_post_rechecks_site_before_retry(plugin):
    session = Session(result=TimeoutError("redacted"))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is False
    assert plugin.get_data("last_result")["state"] == "pending"
    session.account = _account(checkin_today=True,
                               last_checkin=datetime.now().date().isoformat())
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1
    assert plugin.get_data("last_result")["state"] == "success"


@pytest.mark.parametrize("extra", [
    {}, {"checkin_today": None}, {"checkin_today": 0},
    {"checkin_today": False, "last_checkin": "today"},
    {"checkin_today": True, "last_checkin": "yesterday"},
])
def test_unknown_site_status_preserves_pending_without_post(plugin, extra):
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    data = {"id": 1}
    for key, value in extra.items():
        data[key] = today if value == "today" else yesterday if value == "yesterday" else value
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=Response(200, {"success": True, "data": data}))
    plugin._session = lambda *a, **k: session
    result = plugin._run(manual=True)
    assert result["success"] is False
    assert session.posts == []
    assert plugin.get_data("last_result") == {"date": today, "state": "pending"}


def test_today_pending_checks_site_and_skips_when_signed(plugin, monkeypatch):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=_account(checkin_today=True, last_checkin=today))
    plugin._session = lambda *a, **k: session
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    result = plugin._run(manual=True)
    assert result["success"] is True
    assert session.paths == [plugin.BASE + "/api/auth/me"]
    assert session.posts == []
    assert plugin.get_data("last_result") == {"date": today, "state": "success",
                                              "account_id": 1}
    assert "站点确认今日已签到" in str(fake_logger.info.call_args_list)


def test_today_pending_checks_site_and_signs_when_not_signed(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=_account(checkin_today=False,
                                       last_checkin=(datetime.now().date()
                                                     - timedelta(days=1)).isoformat()))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1
    assert plugin.get_data("last_result")["state"] == "success"


def test_today_success_still_checks_site(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "success"})
    session = Session(account=_account(checkin_today=False))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1


def test_invalid_session_never_posts(plugin):
    session = Session(account=Response(401, {"success": False}))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is False
    assert session.posts == []
    assert session.closed is True
    assert plugin.get_data("last_result") is None


def test_preflight_failure_preserves_pending_without_post(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=Response(403, {"success": False}))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is False
    assert session.posts == []
    assert plugin.get_data("last_result") == {"date": today, "state": "pending"}


# ---------- 历史、账号隔离与待核对尝试 ----------


def test_history_keeps_confirmed_award_on_already_signed(plugin):
    session = Session()
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is True
    today = datetime.now().date().isoformat()
    session.account = _account(checkin_today=True, last_checkin=today)
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1
    records = plugin.get_data("sign_records")
    assert len(records) == 1
    assert records[0]["earned"] == 5
    assert plugin.get_data("last_result")["earned"] == 5


def test_history_does_not_reuse_numbers_across_accounts(plugin):
    session = Session()
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is True
    today = datetime.now().date().isoformat()
    session.account = _account(account_id=2, checkin_today=True, last_checkin=today,
                               points=7, checkin_streak=3)
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1
    last = plugin.get_data("last_result")
    assert last["account_id"] == 2
    assert "earned" not in last
    assert "points" not in last
    assert "streak" not in last


def test_history_keeps_same_day_rows_per_account(plugin):
    today = datetime.now().date().isoformat()
    plugin._remember_result({"date": today, "state": "success", "earned": 5, "points": 25,
                             "streak": 1, "account_id": 1, "time": "09:30:00"})
    plugin._remember_result({"date": today, "state": "success", "earned": 7, "points": 32,
                             "streak": 2, "account_id": 2, "time": "10:00:00"})
    records = plugin.get_data("sign_records")
    assert len(records) == 2
    assert {row["account_id"] for row in records} == {1, 2}
    # 旧版未绑定记录与已绑定记录同日不互斥
    plugin._remember_result({"date": today, "state": "success", "earned": 9})
    assert len(plugin.get_data("sign_records")) == 3
    # 页面按当前账号过滤：账号 1 只看到自己的行与未绑定行，看不到账号 2 的数字
    session = Session(account=_account(checkin_today=True))
    plugin._session = lambda *a, **k: session
    texts = str(plugin.get_page())
    assert "09:30:00" in texts
    assert "未绑定" in texts
    assert "10:00:00" not in texts
    assert "+7" not in texts


def test_history_deduplicates_and_keeps_only_thirty_days(plugin):
    start = datetime(2026, 1, 1)
    for day in range(31):
        plugin._remember_result({"date": (start + timedelta(days=day)).date().isoformat(),
                                 "state": "success", "earned": day, "points": day,
                                 "streak": day})
    records = plugin.get_data("sign_records")
    assert len(records) == 30
    assert records[0]["date"] == "2026-01-02"
    assert records[-1]["date"] == "2026-01-31"
    plugin._remember_result({"date": "2026-01-31", "state": "success", "earned": 30,
                             "points": 30, "streak": 30})
    assert len(plugin.get_data("sign_records")) == 30


def test_history_deduplicates_legacy_rows_and_limits_display_sum(plugin):
    start = datetime(2026, 1, 1)
    rows = [{"date": (start + timedelta(days=day)).date().isoformat(),
             "state": "success", "earned": 1} for day in range(31)]
    rows.append({"date": "2026-01-31", "state": "success", "earned": 99})
    plugin.save_data("sign_records", rows)
    session = Session()
    plugin._session = lambda *a, **k: session
    texts = str(plugin.get_page())
    assert "未绑定" in texts
    assert "本账号有积分记录 0 天" in texts
    assert "未提供" in texts
    assert "2026-01-01" not in texts
    rows_node = plugin.get_page()[2]["content"][1]["content"][0]["content"][0]["content"][1]["content"]
    assert len(rows_node) == 30
    assert session.posts == []
    plugin._remember_result({"date": "2026-02-01", "state": "success", "earned": 2})
    assert len(plugin.get_data("sign_records")) == 30
    assert sum(row["date"] == "2026-01-31" for row in plugin.get_data("sign_records")) == 1


def test_history_write_failure_does_not_change_confirmed_signin(plugin):
    session = Session()
    plugin._session = lambda *a, **k: session
    original_save = plugin.save_data

    def save(key, value):
        if key == "sign_records":
            raise RuntimeError("history storage unavailable")
        original_save(key, value)

    plugin.save_data = save
    assert plugin._run(manual=True)["success"] is True
    assert len(session.posts) == 1
    assert plugin.get_data("last_result")["state"] == "success"


def test_confirmed_success_survives_storage_failure(plugin, notices):
    plugin._notify = True
    session = Session()
    plugin._session = lambda *a, **k: session
    original_save = plugin.save_data

    def save(key, value):
        if key == "last_result" and isinstance(value, dict) and value.get("state") == "success":
            raise OSError("storage down")
        original_save(key, value)

    plugin.save_data = save
    result = plugin._run(manual=True)
    assert result["success"] is True
    assert "本地记录保存失败" in result["message"]
    assert result["data"]["earned"] == 5
    assert len(session.posts) == 1
    notice = notices[-1]
    assert "栖影签到成功（" in notice["title"]
    assert "🧧 获得积分：+5" in notice["text"]
    assert "本地记录保存失败" in notice["text"]
    # pending 标记与未决尝试保持一致（都不单边清除），页面按站点确认展示已签到
    assert plugin.get_data("last_result")["state"] == "pending"
    assert len(plugin.get_data("pending_attempts")) == 1


def test_confirmed_success_storage_failure_recovers_on_retry(plugin, notices):
    plugin._notify = True
    session = Session()
    plugin._session = lambda *a, **k: session
    original_save = plugin.save_data
    fails = {"left": 1}

    def save(key, value):
        if (key == "last_result" and isinstance(value, dict)
                and value.get("state") == "success" and fails["left"] > 0):
            fails["left"] -= 1
            raise OSError("transient")
        original_save(key, value)

    plugin.save_data = save
    result = plugin._run(manual=True)
    assert result["success"] is True
    assert result["message"] == "签到成功"
    assert plugin.get_data("last_result")["state"] == "success"
    assert plugin.get_data("pending_attempts") == []
    assert "本地记录保存失败" not in str(notices)


def test_already_signed_survives_storage_failure(plugin, notices):
    plugin._notify = True
    today = datetime.now().date().isoformat()
    session = Session(account=_account(checkin_today=True, last_checkin=today,
                                       points=30, checkin_streak=2))
    plugin._session = lambda *a, **k: session
    original_save = plugin.save_data

    def save(key, value):
        if key == "last_result" and isinstance(value, dict) and value.get("state") == "success":
            raise OSError("storage down")
        original_save(key, value)

    plugin.save_data = save
    result = plugin._run(manual=True)
    assert result["success"] is True
    assert "本地记录保存失败" in result["message"]
    assert session.posts == []
    notice = notices[-1]
    assert "栖影已签到（" in notice["title"]
    assert "💰 当前积分：30" in notice["text"]
    assert "本地记录保存失败" in notice["text"]


def test_stale_pending_attempts_survive_new_day(plugin):
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    plugin.save_data("pending_attempts", [{"attempt_id": "old", "date": yesterday,
                                           "account_id": 1, "started_at": "2026-01-01 23:59:00"}])
    session = Session()
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is True
    rows = plugin.get_data("pending_attempts")
    assert len(rows) == 1
    assert rows[0]["date"] == yesterday
    page_text = str(plugin.get_page())
    assert "历史待核对" in page_text
    assert yesterday in page_text


def test_failure_reason_persisted_and_cleared_on_success(plugin):
    session = Session(result=TimeoutError("redacted"))
    plugin._session = lambda *a, **k: session
    assert plugin._run(manual=True)["success"] is False
    error = plugin.get_data("last_error")
    assert error["message"]
    assert error["date"] == datetime.now().date().isoformat()
    assert "redacted" not in str(error)
    session.result = SUCCESS
    assert plugin._run(manual=True)["success"] is True
    assert plugin.get_data("last_error") == {}


# ---------- 首页展示 ----------


def test_page_reads_account_without_signing_and_shows_history(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "success", "earned": 5,
                                     "points": 25, "streak": 2, "account_id": 1})
    plugin.save_data("sign_records", [
        {"date": today, "state": "success", "earned": 5, "points": 25, "streak": 2,
         "time": "09:30:00", "account_id": 1},
        {"date": "2026-01-01", "state": "success", "earned": 3, "points": 20, "streak": 1,
         "time": "09:30:00", "account_id": 1}])
    session = Session(account=_account(nickname="测试账号", points=123456789012345,
                                       checkin_streak=2, checkin_today=True))
    plugin._session = lambda *a, **k: session
    page = plugin.get_page()
    assert session.paths == [plugin.BASE + "/api/auth/me"]
    assert session.posts == []
    assert session.closed is True
    assert len(page[0]["content"]) == 4
    texts = str(page)
    for value in ("测试账号", "123,456,789,012,345", "2 天", "8", "已签到",
                  "2026-01-01", "overflow-x:auto"):
        assert value in texts
    rows = page[2]["content"][1]["content"][0]["content"][0]["content"][1]["content"]
    assert len(rows) == 2


def test_page_failure_preserves_pending_without_post(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=Response(403, {"success": False}))
    plugin._session = lambda *a, **k: session
    page = plugin.get_page()
    assert session.posts == []
    assert session.closed is True
    assert "获取失败" in str(page)
    assert "结果待核对" in str(page)
    assert plugin.get_data("last_result") == {"date": today, "state": "pending"}


def test_pending_page_does_not_claim_unsigned_from_read_only_status(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session()
    plugin._session = lambda *a, **k: session
    page = plugin.get_page()
    assert "结果待核对" in str(page)
    assert "未签到" not in str(page)
    assert session.posts == []


def test_page_pending_stays_pending_when_site_says_signed(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=_account(checkin_today=True))
    plugin._session = lambda *a, **k: session
    page = plugin.get_page()
    assert "已签到（站点确认；本地待核对标记待更新）" in str(page)
    assert "结果待核对" not in str(page)
    assert plugin.get_data("last_result")["state"] == "pending"
    assert session.posts == []


def test_page_shows_local_confirmed_when_realtime_query_fails(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "success", "earned": 5,
                                     "points": 25, "streak": 2, "account_id": 1})
    session = Session(account=Response(403, {"success": False}))
    plugin._session = lambda *a, **k: session
    texts = str(plugin.get_page())
    assert "已签到（本地已确认，实时查询失败）" in texts
    assert "获取失败" in texts
    assert session.posts == []


def test_page_today_state_is_account_scoped(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending", "account_id": 1,
                                     "attempt_id": "a", "started_at": "x"})
    plugin.save_data("pending_attempts", [{"attempt_id": "a", "date": today,
                                           "account_id": 1, "started_at": "x"}])
    session = Session(account=_account(account_id=2, checkin_today=False))
    plugin._session = lambda *a, **k: session
    texts = str(plugin.get_page())
    # 账号 A 的待核对不得污染账号 B 的今日状态
    assert "未签到" in texts
    assert "结果待核对" not in texts
    assert f"最近记录：{today}" not in texts
    # 账号 A 同日的未决尝试仍以审计信息呈现
    assert "历史待核对" in texts
    session.account = _account(account_id=2, checkin_today=True)
    texts = str(plugin.get_page())
    assert "已签到" in texts
    assert "结果待核对" not in texts


def test_page_today_state_keeps_legacy_pending_compat(plugin):
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "pending"})
    session = Session(account=_account(account_id=2, checkin_today=False))
    plugin._session = lambda *a, **k: session
    texts = str(plugin.get_page())
    # 旧版未绑定记录走兼容路径：仍作为当前账号的待核对展示
    assert "结果待核对" in texts
    assert session.posts == []


def test_page_recovers_today_pending_from_account_attempts(plugin):
    today = datetime.now().date().isoformat()
    # last_result 属于账号 B；账号 A 的同日未决尝试保存在 attempts 中
    plugin.save_data("last_result", {"date": today, "state": "success", "earned": 9,
                                     "points": 40, "streak": 4, "account_id": 2})
    plugin.save_data("pending_attempts", [{"attempt_id": "a", "date": today,
                                           "account_id": 1, "started_at": "x"}])
    # 站点查询成功但缺少 checkin_today 字段
    session = Session(account=Response(200, {"success": True, "data": {"id": 1}}))
    plugin._session = lambda *a, **k: session
    texts = str(plugin.get_page())
    assert "结果待核对" in texts
    assert "无法确认" not in texts
    # 同日本账号的未决尝试不属于“历史待核对”，避免重复提示
    assert "历史待核对" not in texts
    session.account = _account(checkin_today=True)
    texts = str(plugin.get_page())
    assert "已签到（站点确认；本地待核对标记待更新）" in texts


def test_store_confirmed_rejects_foreign_same_day_success(plugin):
    """读回校验含账号：异账号同日 success 不得被当作本账号已落盘。"""
    today = datetime.now().date().isoformat()
    plugin.save_data("last_result", {"date": today, "state": "success", "earned": 99,
                                     "points": 99, "streak": 9, "account_id": 1})
    original_save = plugin.save_data

    def save(key, value):
        if key == "last_result":
            return  # 静默不落盘，不抛异常
        original_save(key, value)

    plugin.save_data = save
    confirmed = {"date": today, "state": "success", "account_id": 2,
                 "earned": 5, "points": 25, "streak": 1}
    assert plugin._store_confirmed(confirmed, today) is False
    # 存储内容未被误改写，仍是账号 A 的记录
    assert plugin.get_data("last_result")["account_id"] == 1


def test_confirmed_success_write_silently_ignored_keeps_state(plugin, notices):
    plugin._notify = True
    session = Session(account=_account(account_id=2, checkin_today=False))
    plugin._session = lambda *a, **k: session
    original_save = plugin.save_data

    def save(key, value):
        if key == "last_result" and isinstance(value, dict) and value.get("state") == "success":
            return  # 静默不落盘，不抛异常
        original_save(key, value)

    plugin.save_data = save
    result = plugin._run(manual=True)
    assert result["success"] is True
    assert "本地记录保存失败" in result["message"]
    # 确认结果未落盘：pending 标记与未决尝试保持一致，不误清本账号待核对状态
    assert plugin.get_data("last_result")["state"] == "pending"
    assert plugin.get_data("last_result")["account_id"] == 2
    assert len(plugin.get_data("pending_attempts")) == 1
    assert "本地记录保存失败" in notices[-1]["text"]


def test_post_crossing_midnight_keeps_pending_state(clean_plugin, notices):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
    # 时钟序列：initial / 尝试开始 / 提交前复核 均为 D 日；POST 响应返回后已是 D+1
    times = [None, datetime(2026, 1, 1, 23, 59, 59), datetime(2026, 1, 1, 23, 59, 59),
             datetime(2026, 1, 1, 23, 59, 59), datetime(2026, 1, 2, 0, 0, 30),
             datetime(2026, 1, 2, 0, 0, 30), datetime(2026, 1, 2, 0, 0, 31)]
    state = {"i": 0}

    def fake_now():
        if state["i"] < len(times) - 1:
            state["i"] += 1
        return times[state["i"]]

    clean_plugin._now = fake_now
    session = Session()
    clean_plugin._session = lambda *a, **k: session
    result = clean_plugin._run(manual=True)
    assert result["success"] is True
    assert "跨午夜" in result["message"]
    assert len(session.posts) == 1
    # 不把结果静默归入旧日期：pending 标记与未决尝试原样保留
    last = clean_plugin.get_data("last_result")
    assert last["date"] == "2026-01-01"
    assert last["state"] == "pending"
    assert len(clean_plugin.get_data("pending_attempts")) == 1
    assert clean_plugin.get_data("sign_records") is None
    assert "栖影签到成功（" in notices[-1]["title"]
    assert "跨过午夜" in notices[-1]["text"]


def test_page_without_cookie_does_not_query(clean_plugin):
    clean_plugin.init_plugin({"enabled": False, "notify": False})
    clean_plugin._session = Mock(side_effect=AssertionError("unexpected request"))
    page = clean_plugin.get_page()
    assert "获取失败" in str(page)


# ---------- 通知 ----------


def test_notification_templates_and_no_post_on_already_signed(clean_plugin, notices):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
    signed = Session()
    clean_plugin._session = lambda *a, **k: signed
    assert clean_plugin._run(manual=True)["success"] is True
    notice = notices[-1]
    assert "栖影签到成功（" in notice["title"]
    for line in ("✅ 签到模式：普通签", "🧧 获得积分：+5", "💰 当前积分：25",
                 "🔥 连续签到：1 天", "🕒 "):
        assert line in notice["text"]
    assert "栖影签到成功" not in notice["text"]

    today = datetime.now().date().isoformat()
    signed.account = _account(checkin_today=True, last_checkin=today, points=30,
                              checkin_streak=2)
    assert clean_plugin._run(manual=True)["success"] is True
    assert len(signed.posts) == 1
    notice = notices[-1]
    assert "栖影已签到（" in notice["title"]
    assert "今日已完成签到，无需重复操作" in notice["text"]
    assert "💰 当前积分：30" in notice["text"]
    assert "🔥 连续签到：2 天" in notice["text"]
    assert "获得积分" not in notice["text"]


def test_notification_missing_fields_and_preflight_failure(clean_plugin, notices):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
    today = datetime.now().date().isoformat()
    session = Session(account=_account(checkin_today=True, last_checkin=today))
    clean_plugin._session = lambda *a, **k: session
    assert clean_plugin._run(manual=True)["success"] is True
    assert session.posts == []
    assert "💰 当前积分：未提供" in notices[-1]["text"]
    assert "🔥 连续签到：未提供 天" in notices[-1]["text"]
    session.account = Response(403, {"success": False, "message": "private-token-secret"})
    assert clean_plugin._run(manual=True)["success"] is False
    assert "栖影签到未完成（" in notices[-1]["title"]
    assert "private-token-secret" not in str(notices)


def test_notification_ambiguous_post_is_pending_and_disabled_is_silent(clean_plugin, notices):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
    session = Session(result=TimeoutError("secret-cookie-value"))
    clean_plugin._session = lambda *a, **k: session
    assert clean_plugin._run(manual=True)["success"] is False
    notice = notices[-1]
    assert "栖影签到待核对（" in notice["title"]
    assert "勿直接重复提交" in notice["text"]
    assert "secret-cookie-value" not in str(notice)
    assert clean_plugin.get_data("last_result")["state"] == "pending"
    count = len(notices)
    clean_plugin._notify = False
    today = datetime.now().date().isoformat()
    session.account = _account(checkin_today=True, last_checkin=today)
    assert clean_plugin._run(manual=True)["success"] is True
    assert len(notices) == count
    assert len(session.posts) == 1


def test_notification_signed_award_and_ambiguous_storage(clean_plugin, notices):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
    clean_plugin._send_notify("success", "2026-01-01", {"earned": -5, "points": 20, "streak": 1})
    assert "🧧 获得积分：-5" in notices[-1]["text"]
    assert "+-5" not in notices[-1]["text"]
    clean_plugin._send_notify("success", "2026-01-01", {})
    assert "🧧 获得积分：未提供" in notices[-1]["text"]


# ---------- 设置页 ----------


def test_form_uses_grid_layout_with_all_fields(clean_plugin):
    form, defaults = clean_plugin.get_form()
    rows = [item for item in form[0]["content"] if item.get("component") == "VRow"]
    assert len(rows) >= 3

    def collect_models(node):
        found = set()
        if isinstance(node, dict):
            props = node.get("props")
            if isinstance(props, dict) and "model" in props:
                found.add(props["model"])
            for value in node.values():
                found |= collect_models(value)
        elif isinstance(node, list):
            for item in node:
                found |= collect_models(item)
        return found

    models = collect_models(form)
    for model in ("enabled", "onlyonce", "notify", "cookie", "cron",
                  "use_system_proxy", "proxy", "use_browserless", "browserless_url"):
        assert model in models


def test_browserless_off_by_default_and_missing_url(clean_plugin):
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
    form, defaults = clean_plugin.get_form()
    assert defaults["use_browserless"] is False
    assert "browserless_url" in str(form)

    def find_components(node, component):
        found = []
        if isinstance(node, dict):
            if node.get("component") == component:
                found.append(node)
            for value in node.values():
                found.extend(find_components(value, component))
        elif isinstance(node, list):
            for item in node:
                found.extend(find_components(item, component))
        return found

    cards = find_components(form, "VCard")
    assert len(cards) == 1
    card = cards[0]
    assert card["props"]["class"] == "mt-4 mb-3"
    card_text = card["content"][1]
    assert card_text["component"] == "VCardText"
    fields = find_components(card_text, "VSwitch") + \
        [item for item in find_components(card_text, "VTextField")
         if item["props"]["model"] == "browserless_url"]
    assert [item["props"]["model"] for item in fields] == ["use_browserless", "browserless_url"]
    assert fields[1]["props"]["type"] == "password"
    assert fields[1]["props"]["persistent-hint"]
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True})
    assert clean_plugin._run(manual=True)["success"] is False
    assert clean_plugin.get_data("last_result") is None


# ---------- Browserless 备用模式 ----------


def test_browserless_fake_preflight_and_sign(clean_plugin, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, impersonate=None, **kwargs):
            self.proxies = {}

        def post(self, endpoint, json, timeout):
            request = json["context"]["request"]
            calls.append(request["method"])
            assert endpoint.endswith("/function?token=placeholder&timeout=45000")
            assert timeout == 55
            assert "placeholder" not in json["code"]
            assert "AbortController" in json["code"]
            assert json["context"]["cookies"][0]["value"] == "placeholder"
            assert json["context"]["cookies"][0]["httpOnly"] is True
            assert json["context"]["cookies"][0]["secure"] is True
            if request["method"] == "GET":
                body = '{"success":true,"data":{"id":1,"checkin_today":false}}'
                status = 200
            else:
                body = ('{"success":true,"data":{"mode":"normal","earned":5,'
                        '"points":25,"checkin_streak":1}}')
                status = 201
            return Response(200, {"status": status, "contentType": "application/json",
                                  "body": body})

        def close(self):
            pass

    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeClient))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True,
                              "browserless_url": "https://browser.example/function?token=placeholder"})
    assert clean_plugin._run(manual=True)["success"] is True
    assert calls == ["GET", "POST"]


def test_browserless_passes_rotated_cookies_to_next_call(clean_plugin, monkeypatch):
    cookies_seen = []

    class FakeClient:
        def __init__(self, impersonate=None, **kwargs):
            self.proxies = {}

        def post(self, endpoint, json, timeout):
            request = json["context"]["request"]
            cookies_seen.append({item["name"]: item["value"]
                                 for item in json["context"]["cookies"]})
            if request["method"] == "GET":
                body = {"success": True, "data": {"id": 1, "checkin_today": False}}
                return Response(200, {"data": {"status": 200, "contentType": "application/json",
                                               "body": jdumps(body)},
                                      "cookies": [{"name": "hdh_session", "value": "rotated",
                                                   "domain": "hdhaven.com"}]})
            body = {"success": True, "data": {"mode": "normal", "earned": 5,
                                              "points": 25, "checkin_streak": 1}}
            return Response(200, {"data": {"status": 201, "contentType": "application/json",
                                           "body": jdumps(body)}})

        def close(self):
            pass

    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeClient))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True,
                              "browserless_url": "https://browser.example/function?token=placeholder"})
    assert clean_plugin._run(manual=True)["success"] is True
    assert cookies_seen[0]["hdh_session"] == "placeholder"
    assert cookies_seen[1]["hdh_session"] == "rotated"


def test_browserless_script_export_contract():
    """请求体 context 只是普通 JSON：Cookie 导出必须走远端运行时 API，禁止 context.cookies()。"""
    script = plugin_module._BROWSERLESS_SCRIPT
    assert "context.cookies(" not in script
    assert "page.cookies(" in script
    assert "page.context().cookies(" in script
    assert "page.setCookie(...context.cookies)" in script
    assert "addCookies(context.cookies)" in script
    assert "AbortController" in script


EXEC_HARNESS = r'''
import { readFileSync } from "node:fs";
const [scriptPath, scenario] = process.argv.slice(2);
// 同 realm 求值：脚本里的 fetch 只可能解析到本文件的桩，绝不触网。
const raw = readFileSync(scriptPath, "utf8").replace("export default ", "");
const mod = {default: eval("(" + raw + ")")};
if (typeof globalThis.AbortController === "undefined") {
  globalThis.AbortController = class {
    constructor() { this.signal = {aborted: false, addEventListener() {}}; }
    abort() { this.signal.aborted = true; }
  };
}
if (typeof globalThis.setTimeout === "undefined") globalThis.setTimeout = (fn) => 0;
if (typeof globalThis.clearTimeout === "undefined") globalThis.clearTimeout = () => {};
const injected = [];
const added = [];
let fetchCalls = 0;
globalThis.fetch = async (url, opts) => {
  fetchCalls += 1;
  if (!url.startsWith("https://hdhaven.com/")) {
    throw new Error("unexpected fetch target: " + url);
  }
  return {status: 200, headers: {get: () => "application/json"},
          text: async () => JSON.stringify({ok: true})};
};
const rotated = [{name: "hdh_session", value: "rotated", domain: "hdhaven.com"}];
let page;
if (scenario === "puppeteer") {
  page = {setCookie: async (...c) => { injected.push(c); },
          goto: async () => {}, evaluate: async (fn, arg) => fn(arg),
          cookies: async (url) => rotated};
} else if (scenario === "playwright") {
  page = {context: () => ({addCookies: async (c) => { added.push(...c); },
                            cookies: async (url) => rotated}),
          goto: async () => {}, evaluate: async (fn, arg) => fn(arg)};
} else {
  page = {goto: async () => {}, evaluate: async (fn, arg) => fn(arg)};
}
const body = {cookies: [{name: "hdh_session", value: "placeholder"}],
              request: {method: "GET", url: "https://hdhaven.com/api/auth/me"}};
const result = await mod.default({page, context: body});
process.stdout.write(JSON.stringify({result, injected, added, fetchCalls}));
'''


@pytest.mark.parametrize("scenario", ["puppeteer", "playwright", "no-cookie-api"])
def test_browserless_script_executes_on_both_runtimes(scenario):
    if NODE is None:
        pytest.skip("本机无 node，跳过嵌入脚本可执行测试")
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "script.mjs")
        harness_path = os.path.join(tmp, "harness.mjs")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(plugin_module._BROWSERLESS_SCRIPT)
        with open(harness_path, "w", encoding="utf-8") as fh:
            fh.write(EXEC_HARNESS)
        proc = subprocess.run([NODE, harness_path, script_path, scenario],
                              capture_output=True, text=True, timeout=30)
        if scenario == "no-cookie-api":
            # 两种运行时 API 都缺失时必须响亮失败，而不是无 Cookie 静默签到。
            assert proc.returncode != 0
            return
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["result"]["data"]["status"] == 200
        assert payload["result"]["cookies"][0]["value"] == "rotated"
        assert payload["fetchCalls"] == 1
        if scenario == "puppeteer":
            assert payload["injected"] == [[{"name": "hdh_session", "value": "placeholder"}]]
            assert payload["added"] == []
        else:
            assert payload["added"] == [{"name": "hdh_session", "value": "placeholder"}]
            assert payload["injected"] == []


@pytest.mark.parametrize("url", [
    "http://browser.example/function?token=x",
    "https://browser.example/other?token=x",
    "https://browser.example/function",
    "https://browser.example/function?token=",
    "https://user:secret@browser.example/function?token=x",
])
def test_browserless_invalid_url_never_post(clean_plugin, monkeypatch, url):
    monkeypatch.setattr(plugin_module, "curl_requests", types.SimpleNamespace(Session=Mock()))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True, "browserless_url": url})
    assert clean_plugin._run(manual=True)["success"] is False
    assert clean_plugin.get_data("last_result") is None


def test_browserless_preflight_failure_never_post(clean_plugin, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, impersonate=None, **kwargs):
            self.proxies = {}

        def post(self, endpoint, json, timeout):
            calls.append(json["context"]["request"]["method"])
            return Response(200, {"status": 403, "contentType": "text/html", "body": "blocked"})

        def close(self):
            pass

    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeClient))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True,
                              "browserless_url": "https://browser.example/function?token=placeholder"})
    assert clean_plugin._run(manual=True)["success"] is False
    assert calls == ["GET"]


def test_browserless_timeout_rechecks_status_without_reposting(clean_plugin, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, impersonate=None, **kwargs):
            self.proxies = {}

        def post(self, endpoint, json, timeout):
            method = json["context"]["request"]["method"]
            calls.append(method)
            if method == "POST":
                raise TimeoutError("redacted")
            signed = calls.count("GET") > 1
            body = {"success": True, "data": {"id": 1, "checkin_today": signed}}
            if signed:
                body["data"]["last_checkin"] = datetime.now().date().isoformat()
            return Response(200, {"data": {"status": 200, "contentType": "application/json",
                                           "body": jdumps(body)},
                                  "type": "application/json"})

        def close(self):
            pass

    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeClient))
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True,
                              "browserless_url": "https://browser.example/function?token=placeholder"})
    assert clean_plugin._run(manual=True)["success"] is False
    assert clean_plugin.get_data("last_result")["state"] == "pending"
    assert clean_plugin._run(manual=True)["success"] is True
    assert calls == ["GET", "POST", "GET"]


def test_browserless_uses_system_proxy_without_logging_secret(clean_plugin, monkeypatch):
    monkeypatch.setattr(plugin_module, "settings",
                        types.SimpleNamespace(PROXY={"https": "http://user:secret@example:1"},
                                              TZ=TZ))
    clients = []

    class FakeClient:
        def __init__(self, impersonate=None, **kwargs):
            self.proxies = {}
            clients.append(self)

        def close(self):
            pass

    monkeypatch.setattr(plugin_module, "curl_requests",
                        types.SimpleNamespace(Session=FakeClient))
    fake_logger = Mock()
    monkeypatch.setattr(plugin_module, "logger", fake_logger)
    clean_plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                              "use_browserless": True,
                              "browserless_url": "https://browser.example/function?token=placeholder"})
    clean_plugin._session().close()
    assert clients[0].proxies["https"] == "http://user:secret@example:1"
    assert "secret" not in str(fake_logger.info.call_args_list)
    assert "placeholder" not in str(fake_logger.info.call_args_list)
