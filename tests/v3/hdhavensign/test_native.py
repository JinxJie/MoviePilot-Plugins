"""Credential-free offline tests for HDHaven V3. All HTTP calls are fake."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[3] / "plugins.v3/hdhavensign/__init__.py"


class Base:
    def __init__(self):
        self.data = {}
        self.notices = []

    def get_data(self, key):
        return self.data.get(key)

    def save_data(self, key, value):
        self.data[key] = value

    def update_config(self, config):
        self.config = config

    def post_message(self, **kwargs):
        self.notices.append(kwargs)


def load(proxy=None):
    names = ("app", "app.plugins", "app.sdk", "app.sdk.config", "app.sdk.logging",
             "app.schemas", "app.schemas.types", "apscheduler", "apscheduler.triggers",
             "apscheduler.triggers.cron")
    modules = {name: types.ModuleType(name) for name in names}
    modules["app.plugins"]._PluginBase = Base
    modules["app.sdk.config"].settings = types.SimpleNamespace(PROXY=proxy)
    modules["app.sdk.logging"].logger = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)
    modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Plugin="plugin")
    modules["apscheduler.triggers.cron"].CronTrigger = types.SimpleNamespace(from_crontab=lambda x: x)
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("hdhaven_test", PLUGIN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


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


class HDHavenTests(unittest.TestCase):
    def plugin(self):
        p = load().HDHavenSign()
        p.init_plugin({"enabled": True, "cookie": "hdh_session=placeholder", "notify": False})
        return p

    def test_lifecycle_and_defaults(self):
        p = load().HDHavenSign()
        p.init_plugin({})
        form, defaults = p.get_form()
        self.assertTrue(form)
        self.assertFalse(defaults["enabled"])
        self.assertEqual(p.get_service(), [])
        self.assertFalse(p.get_state())
        self.assertEqual(p.get_api(), [])
        self.assertTrue(p.get_page())
        p.stop_service()
        p = self.plugin()
        self.assertTrue(p.get_state())
        self.assertEqual(len(p.get_service()), 1)
        self.assertEqual(p.get_service()[0]["func"].__name__, "_run")

    def test_proxy_shapes(self):
        for proxy, expected in [({"https": "http://example:1", "http": "http://example:2"}, "http://example:1"),
                                ({"http": "http://example:2"}, "http://example:2"),
                                (types.SimpleNamespace(host="http://example:3"), "http://example:3")]:
            with patch.dict("os.environ", {"PROXY_HOST": "", "https_proxy": "", "HTTPS_PROXY": ""}):
                self.assertEqual(load(proxy).HDHavenSign._proxy_from_settings(), expected)

    def test_session_applies_system_proxy_to_curl_cffi(self):
        class FakeCurlSession:
            def __init__(self, impersonate):
                self.impersonate = impersonate
                self.proxies = {}
                self.headers = {}

        for proxy, expected in [({"https": "http://example:1", "http": "http://example:2"}, "http://example:1"),
                                (types.SimpleNamespace(host="http://example:3"), "http://example:3"),
                                (None, "http://example:4")]:
            module = load(proxy)
            setattr(module, "curl_requests", types.SimpleNamespace(Session=FakeCurlSession))
            with patch.dict("os.environ", {"PROXY_HOST": "http://example:4", "https_proxy": "", "HTTPS_PROXY": ""}):
                plugin = module.HDHavenSign()
                plugin.init_plugin({"cookie": "hdh_session=placeholder"})
                session = plugin._session()
                self.assertEqual(session.proxies, {"http": expected, "https": expected})
                self.assertEqual(session.impersonate, "chrome110")
        module = load({"https": "http://example:1"})
        setattr(module, "curl_requests", types.SimpleNamespace(Session=FakeCurlSession))
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "use_system_proxy": False,
                            "proxy": "http://example:5"})
        self.assertEqual(plugin._session().proxies, {"http": "http://example:5", "https": "http://example:5"})
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "use_system_proxy": False})
        self.assertEqual(plugin._session().proxies, {})

    def test_session_logs_proxy_choice_without_address(self):
        class FakeCurlSession:
            def __init__(self, impersonate):
                self.proxies = {}
                self.headers = {}

        module = load({"https": "http://user:secret@example:1"})
        module.curl_requests = types.SimpleNamespace(Session=FakeCurlSession)
        with patch.object(module.logger, "info") as info, patch.object(module.logger, "warning") as warning:
            plugin = module.HDHavenSign()
            plugin.init_plugin({"cookie": "hdh_session=placeholder"})
            plugin._session()
            self.assertIn("系统", str(info.call_args_list))
            self.assertNotIn("secret", str(info.call_args_list))
            self.assertNotIn("example", str(info.call_args_list))
            plugin.init_plugin({"cookie": "hdh_session=placeholder", "use_system_proxy": False,
                                "proxy": "http://user:secret@example:2"})
            plugin._session()
            self.assertIn("自定义", str(info.call_args_list))
            self.assertNotIn("example", str(info.call_args_list))
            plugin.init_plugin({"cookie": "hdh_session=placeholder", "use_system_proxy": False})
            plugin._session()
            self.assertIn("直连", str(info.call_args_list))
            with patch.object(module.HDHavenSign, "_proxy_from_settings", return_value=""):
                plugin.init_plugin({"cookie": "hdh_session=placeholder"})
                plugin._session()
            self.assertIn("将直连", str(warning.call_args_list))

    def test_preflight_logs_redacted_status_and_failure_stage(self):
        module = load()
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        response = Response(403, {"success": False, "message": "private-token-secret"})
        response.history = [object()]
        session = Session(account=response)
        plugin._session = lambda: session
        with patch.object(module.logger, "info") as info, patch.object(module.logger, "warning") as warning:
            result = plugin._run(manual=True)
        self.assertFalse(result["success"])
        self.assertEqual(session.posts, [])
        self.assertIn("403", str(info.call_args_list))
        self.assertIn("JSON", str(info.call_args_list))
        self.assertIn("登录预检", str(warning.call_args_list))
        self.assertNotIn("private-token-secret", str(info.call_args_list) + str(warning.call_args_list))

    def test_non_json_preflight_logs_response_type_without_body(self):
        module = load()
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        session = Session(account=Response(403, "private-challenge-html", "text/html"))
        plugin._session = lambda: session
        with patch.object(module.logger, "info") as info, patch.object(module.logger, "warning") as warning:
            result = plugin._run(manual=True)
        self.assertIn("可能遭遇人机验证", result["message"])
        self.assertIn("非JSON", str(info.call_args_list))
        self.assertEqual(session.posts, [])
        self.assertNotIn("private-challenge-html", str(info.call_args_list) + str(warning.call_args_list))

    def test_preflight_request_exception_logs_stage_not_url(self):
        module = load()
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        session = Session()
        session.get = Mock(side_effect=RuntimeError("private-proxy-url"))
        plugin._session = lambda: session
        with patch.object(module.logger, "warning") as warning:
            plugin._run(manual=True)
        self.assertIn("登录预检", str(warning.call_args_list))
        self.assertNotIn("private-proxy-url", str(warning.call_args_list))

    def test_cookie_parsing(self):
        p = self.plugin()
        self.assertEqual(p._parse_cookie("Cookie: hdh_session=placeholder; cf_clearance=placeholder"),
                         "hdh_session=placeholder; cf_clearance=placeholder")
        for value in ("", "placeholder", "cf_clearance=placeholder", "session=placeholder",
                      "hdh_session=placeholder\nInjected: x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                p._parse_cookie(value)

    def test_parser(self):
        p = self.plugin()
        self.assertEqual(p._interpret(SUCCESS)["earned"], 5)
        for response in (Response(409, {"success": False, "message": "unknown"}),
                         Response(200, "html", "text/html"), Response(401, {"success": False}),
                         Response(201, {"success": True, "data": {"mode": "normal"}})):
            with self.subTest(status=response.status_code), self.assertRaises(RuntimeError):
                p._interpret(response)

    def test_success_empty_body_and_duplicate_gate(self):
        p = self.plugin()
        s = Session()
        p._session = lambda: s
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(s.paths, [p.BASE + "/api/auth/me"])
        self.assertEqual(len(s.posts), 1)
        self.assertEqual(s.posts[0], ((p.BASE + "/api/account/checkin",), {"timeout": 20}))
        self.assertTrue(s.closed)
        self.assertEqual(p.get_data("last_result")["state"], "success")
        s.account = Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": datetime.now().date().isoformat()}})
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(len(s.posts), 1)

    def test_today_pending_checks_site_and_skips_when_signed(self):
        module = load()
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        today = module.datetime.now().date().isoformat()
        plugin.save_data("last_result", {"date": today, "state": "pending"})
        session = Session(account=Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": today}}))
        plugin._session = lambda: session
        with patch.object(module.logger, "info") as info:
            result = plugin._run(manual=True)
        self.assertTrue(result["success"])
        self.assertEqual(session.paths, [plugin.BASE + "/api/auth/me"])
        self.assertEqual(session.posts, [])
        self.assertEqual(plugin.get_data("last_result"), {"date": today, "state": "success"})
        self.assertIn("站点确认今日已签到", str(info.call_args_list))

    def test_today_pending_checks_site_and_signs_when_not_signed(self):
        module = load()
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        today = module.datetime.now().date().isoformat()
        plugin.save_data("last_result", {"date": today, "state": "pending"})
        session = Session(account=Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": False, "last_checkin": (datetime.now().date() - timedelta(days=1)).isoformat()}}))
        plugin._session = lambda: session
        self.assertTrue(plugin._run(manual=True)["success"])
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(plugin.get_data("last_result")["state"], "success")

    def test_today_success_still_checks_site(self):
        module = load()
        plugin = module.HDHavenSign()
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        today = module.datetime.now().date().isoformat()
        plugin.save_data("last_result", {"date": today, "state": "success"})
        session = Session(account=Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": False}}))
        plugin._session = lambda: session
        self.assertTrue(plugin._run(manual=True)["success"])
        self.assertEqual(len(session.posts), 1)

    def test_unknown_site_status_preserves_pending_without_post(self):
        module = load()
        today = module.datetime.now().date().isoformat()
        for data in ({"id": 1}, {"id": 1, "checkin_today": None},
                     {"id": 1, "checkin_today": 0},
                     {"id": 1, "checkin_today": False, "last_checkin": today},
                     {"id": 1, "checkin_today": True, "last_checkin": (datetime.now().date() - timedelta(days=1)).isoformat()}):
            with self.subTest(data=data):
                plugin = module.HDHavenSign()
                plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
                plugin.save_data("last_result", {"date": today, "state": "pending"})
                session = Session(account=Response(200, {"success": True, "data": data}))
                plugin._session = lambda: session
                result = plugin._run(manual=True)
                self.assertFalse(result["success"])
                self.assertEqual(session.posts, [])
                self.assertEqual(plugin.get_data("last_result"), {"date": today, "state": "pending"})

    def test_preflight_failure_preserves_pending_without_post(self):
        plugin = self.plugin()
        today = datetime.now().date().isoformat()
        plugin.save_data("last_result", {"date": today, "state": "pending"})
        session = Session(account=Response(403, {"success": False}))
        plugin._session = lambda: session
        self.assertFalse(plugin._run(manual=True)["success"])
        self.assertEqual(session.posts, [])
        self.assertEqual(plugin.get_data("last_result"), {"date": today, "state": "pending"})

    def test_invalid_session_never_posts(self):
        p = self.plugin()
        s = Session(account=Response(401, {"success": False}))
        p._session = lambda: s
        self.assertFalse(p._run(manual=True)["success"])
        self.assertEqual(s.posts, [])
        self.assertTrue(s.closed)
        self.assertIsNone(p.get_data("last_result"))

    def test_ambiguous_post_rechecks_site_before_retry(self):
        p = self.plugin()
        s = Session(result=TimeoutError("redacted"))
        p._session = lambda: s
        self.assertFalse(p._run(manual=True)["success"])
        self.assertEqual(p.get_data("last_result")["state"], "pending")
        s.account = Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": datetime.now().date().isoformat()}})
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(len(s.posts), 1)
        self.assertEqual(p.get_data("last_result")["state"], "success")

    def test_failed_marker_write_never_posts(self):
        p = self.plugin()
        s = Session()
        p._session = lambda: s
        p.save_data = lambda key, value: None
        self.assertFalse(p._run(manual=True)["success"])
        self.assertEqual(s.posts, [])
        self.assertTrue(s.closed)

    def test_unknown_business_response_stays_pending(self):
        p = self.plugin()
        s = Session(result=Response(409, {"success": False, "message": "unknown"}))
        p._session = lambda: s
        self.assertFalse(p._run(manual=True)["success"])
        self.assertEqual(p.get_data("last_result")["state"], "pending")
        self.assertEqual(len(s.posts), 1)


if __name__ == "__main__":
    unittest.main()
