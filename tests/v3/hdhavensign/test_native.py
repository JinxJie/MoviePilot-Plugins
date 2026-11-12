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

    def test_page_reads_account_without_signing_and_shows_history(self):
        p = self.plugin()
        today = datetime.now().date().isoformat()
        p.save_data("last_result", {"date": today, "state": "success", "earned": 5,
                                    "points": 25, "streak": 2})
        p.save_data("sign_records", [{"date": today, "state": "success", "earned": 5,
                                      "points": 25, "streak": 2, "time": "09:30:00"},
                                     {"date": "2026-01-01", "state": "success", "earned": 3,
                                      "points": 20, "streak": 1, "time": "09:30:00"}])
        session = Session(account=Response(200, {"success": True, "data": {
            "id": 1, "nickname": "测试账号", "points": 123456789012345,
            "checkin_streak": 2, "checkin_today": True}}))
        p._session = lambda: session
        page = p.get_page()
        self.assertEqual(session.paths, [p.BASE + "/api/auth/me"])
        self.assertEqual(session.posts, [])
        self.assertTrue(session.closed)
        tiles = page[0]["content"]
        self.assertEqual(len(tiles), 4)
        texts = str(page)
        for value in ("测试账号", "123,456,789,012,345", "2 天", "8", "已签到",
                      "2026-01-01", "overflow-x:auto"):
            self.assertIn(value, texts)
        self.assertEqual(len(page[2]["content"][1]["content"][0]["content"][0]["content"][1]["content"]), 2)

    def test_page_failure_preserves_pending_without_post(self):
        p = self.plugin()
        today = datetime.now().date().isoformat()
        p.save_data("last_result", {"date": today, "state": "pending"})
        session = Session(account=Response(403, {"success": False}))
        p._session = lambda: session
        page = p.get_page()
        self.assertEqual(session.posts, [])
        self.assertTrue(session.closed)
        self.assertIn("获取失败", str(page))
        self.assertIn("结果待核对", str(page))
        self.assertEqual(p.get_data("last_result"), {"date": today, "state": "pending"})

    def test_pending_page_does_not_claim_unsigned_from_read_only_status(self):
        p = self.plugin()
        today = datetime.now().date().isoformat()
        p.save_data("last_result", {"date": today, "state": "pending"})
        session = Session()
        p._session = lambda: session
        page = p.get_page()
        self.assertIn("结果待核对", str(page))
        self.assertNotIn("未签到", str(page))
        self.assertEqual(session.posts, [])

    def test_pending_page_stays_pending_when_site_says_signed(self):
        p = self.plugin()
        today = datetime.now().date().isoformat()
        p.save_data("last_result", {"date": today, "state": "pending"})
        session = Session(account=Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True}}))
        p._session = lambda: session
        page = p.get_page()
        self.assertIn("结果待核对（站点显示已签到）", str(page))
        self.assertEqual(p.get_data("last_result")["state"], "pending")
        self.assertEqual(session.posts, [])

    def test_history_keeps_confirmed_award_on_already_signed(self):
        p = self.plugin()
        s = Session()
        p._session = lambda: s
        self.assertTrue(p._run(manual=True)["success"])
        today = datetime.now().date().isoformat()
        s.account = Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": today}})
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(len(s.posts), 1)
        records = p.get_data("sign_records")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["earned"], 5)
        self.assertEqual(p.get_data("last_result")["earned"], 5)

    def test_history_deduplicates_and_keeps_only_thirty_days(self):
        p = self.plugin()
        start = datetime(2026, 1, 1)
        for day in range(31):
            p._remember_result({"date": (start + timedelta(days=day)).date().isoformat(),
                                "state": "success", "earned": day, "points": day,
                                "streak": day})
        records = p.get_data("sign_records")
        self.assertEqual(len(records), 30)
        self.assertEqual(records[0]["date"], "2026-01-02")
        self.assertEqual(records[-1]["date"], "2026-01-31")
        p._remember_result({"date": "2026-01-31", "state": "success", "earned": 30,
                            "points": 30, "streak": 30})
        self.assertEqual(len(p.get_data("sign_records")), 30)

    def test_history_deduplicates_legacy_rows_and_limits_display_sum(self):
        p = self.plugin()
        start = datetime(2026, 1, 1)
        rows = [{"date": (start + timedelta(days=day)).date().isoformat(),
                 "state": "success", "earned": 1} for day in range(31)]
        rows.append({"date": "2026-01-31", "state": "success", "earned": 99})
        p.save_data("sign_records", rows)
        session = Session()
        p._session = lambda: session
        page = p.get_page()
        texts = str(page)
        self.assertIn("本地有积分记录 30 天", texts)
        self.assertIn("128", texts)
        self.assertNotIn("2026-01-01", texts)
        self.assertEqual(len(page[2]["content"][1]["content"][0]["content"][0]["content"][1]["content"]), 30)
        self.assertEqual(session.posts, [])
        p._remember_result({"date": "2026-02-01", "state": "success", "earned": 2})
        self.assertEqual(len(p.get_data("sign_records")), 30)
        self.assertEqual(sum(row["date"] == "2026-01-31" for row in p.get_data("sign_records")), 1)

    def test_history_write_failure_does_not_change_confirmed_signin(self):
        p = self.plugin()
        session = Session()
        p._session = lambda: session
        original_save = p.save_data

        def save(key, value):
            if key == "sign_records":
                raise RuntimeError("history storage unavailable")
            original_save(key, value)

        p.save_data = save
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(p.get_data("last_result")["state"], "success")

    def test_page_without_cookie_does_not_query(self):
        p = load().HDHavenSign()
        p.init_plugin({})
        with patch.object(p, "_session", side_effect=AssertionError("unexpected request")):
            page = p.get_page()
        self.assertIn("获取失败", str(page))

    def test_notification_templates_and_no_post_on_already_signed(self):
        p = load().HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
        signed = Session()
        p._session = lambda: signed
        self.assertTrue(p._run(manual=True)["success"])
        notice = p.notices[-1]
        self.assertIn("栖影签到成功（", notice["title"])
        for line in ("✅ 签到模式：普通签", "🧧 获得积分：+5", "💰 当前积分：25",
                     "🔥 连续签到：1 天", "🕒 "):
            self.assertIn(line, notice["text"])
        self.assertNotIn("栖影签到成功", notice["text"])

        today = datetime.now().date().isoformat()
        signed.account = Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": today, "points": 30,
            "checkin_streak": 2}})
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(len(signed.posts), 1)
        notice = p.notices[-1]
        self.assertIn("栖影已签到（", notice["title"])
        self.assertIn("今日已完成签到，无需重复操作", notice["text"])
        self.assertIn("💰 当前积分：30", notice["text"])
        self.assertIn("🔥 连续签到：2 天", notice["text"])
        self.assertNotIn("获得积分", notice["text"])

    def test_notification_missing_fields_and_preflight_failure(self):
        p = load().HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
        today = datetime.now().date().isoformat()
        session = Session(account=Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": today}}))
        p._session = lambda: session
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(session.posts, [])
        self.assertIn("💰 当前积分：未提供", p.notices[-1]["text"])
        self.assertIn("🔥 连续签到：未提供 天", p.notices[-1]["text"])
        session.account = Response(403, {"success": False, "message": "private-token-secret"})
        self.assertFalse(p._run(manual=True)["success"])
        self.assertIn("栖影签到未完成（", p.notices[-1]["title"])
        self.assertNotIn("private-token-secret", str(p.notices))

    def test_notification_ambiguous_post_is_pending_and_disabled_is_silent(self):
        p = load().HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
        session = Session(result=TimeoutError("secret-cookie-value"))
        p._session = lambda: session
        self.assertFalse(p._run(manual=True)["success"])
        notice = p.notices[-1]
        self.assertIn("栖影签到待核对（", notice["title"])
        self.assertIn("勿直接重复提交", notice["text"])
        self.assertNotIn("secret-cookie-value", str(notice))
        self.assertEqual(p.get_data("last_result")["state"], "pending")
        count = len(p.notices)
        p._notify = False
        today = datetime.now().date().isoformat()
        session.account = Response(200, {"success": True, "data": {
            "id": 1, "checkin_today": True, "last_checkin": today}})
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(len(p.notices), count)
        self.assertEqual(len(session.posts), 1)

    def test_notification_signed_award_and_ambiguous_storage(self):
        p = load().HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
        p._send_notify("success", "2026-01-01", {"earned": -5, "points": 20, "streak": 1})
        self.assertIn("🧧 获得积分：-5", p.notices[-1]["text"])
        self.assertNotIn("+-5", p.notices[-1]["text"])
        p._send_notify("success", "2026-01-01", {})
        self.assertIn("🧧 获得积分：未提供", p.notices[-1]["text"])

    def test_browserless_off_by_default_and_missing_url(self):
        module = load()
        p = module.HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False})
        form, defaults = p.get_form()
        self.assertFalse(defaults["use_browserless"])
        self.assertIn("browserless_url", str(form))
        nodes = form[0]["content"]
        cards = [node for node in nodes if node["component"] == "VCard"]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["props"]["class"], "mt-4 mb-3")
        self.assertEqual(card["content"][0]["text"], "Browserless 备用模式（可选）")
        fields = card["content"][1]["content"]
        self.assertEqual([item["props"]["model"] for item in fields],
                         ["use_browserless", "browserless_url"])
        self.assertEqual(fields[1]["props"]["type"], "password")
        self.assertTrue(fields[1]["props"]["persistent-hint"])
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                       "use_browserless": True})
        self.assertFalse(p._run(manual=True)["success"])
        self.assertIsNone(p.get_data("last_result"))

    def test_browserless_fake_preflight_and_sign(self):
        module = load()
        calls = []
        class FakeClient:
            def __init__(self, impersonate):
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
                    body = '{"success":true,"data":{"mode":"normal","earned":5,"points":25,"checkin_streak":1}}'
                    status = 201
                return Response(200, {"status": status, "contentType": "application/json", "body": body})
            def close(self):
                pass
        module.curl_requests = types.SimpleNamespace(Session=FakeClient)
        p = module.HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                       "use_browserless": True,
                       "browserless_url": "https://browser.example/function?token=placeholder"})
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(calls, ["GET", "POST"])

    def test_browserless_preflight_failure_and_invalid_url_never_post(self):
        module = load()
        module.curl_requests = types.SimpleNamespace(Session=Mock())
        for url in ("http://browser.example/function?token=x", "https://browser.example/other?token=x",
                    "https://browser.example/function", "https://browser.example/function?token=",
                    "https://user:secret@browser.example/function?token=x"):
            p = module.HDHavenSign()
            p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                           "use_browserless": True, "browserless_url": url})
            self.assertFalse(p._run(manual=True)["success"])
            self.assertIsNone(p.get_data("last_result"))
        calls = []
        class FakeClient:
            def __init__(self, impersonate):
                self.proxies = {}
            def post(self, endpoint, json, timeout):
                calls.append(json["context"]["request"]["method"])
                return Response(200, {"status": 403, "contentType": "text/html", "body": "blocked"})
            def close(self):
                pass
        module.curl_requests = types.SimpleNamespace(Session=FakeClient)
        p = module.HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                       "use_browserless": True,
                       "browserless_url": "https://browser.example/function?token=placeholder"})
        self.assertFalse(p._run(manual=True)["success"])
        self.assertEqual(calls, ["GET"])

    def test_browserless_timeout_rechecks_status_without_reposting(self):
        module = load()
        calls = []
        class FakeClient:
            def __init__(self, impersonate):
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
                                                "body": __import__("json").dumps(body)},
                                      "type": "application/json"})
            def close(self):
                pass
        module.curl_requests = types.SimpleNamespace(Session=FakeClient)
        p = module.HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                       "use_browserless": True,
                       "browserless_url": "https://browser.example/function?token=placeholder"})
        self.assertFalse(p._run(manual=True)["success"])
        self.assertEqual(p.get_data("last_result")["state"], "pending")
        self.assertTrue(p._run(manual=True)["success"])
        self.assertEqual(calls, ["GET", "POST", "GET"])

    def test_browserless_uses_system_proxy_without_logging_secret(self):
        module = load({"https": "http://user:secret@example:1"})
        clients = []
        class FakeClient:
            def __init__(self, impersonate):
                self.proxies = {}
                clients.append(self)
            def close(self):
                pass
        module.curl_requests = types.SimpleNamespace(Session=FakeClient)
        p = module.HDHavenSign()
        p.init_plugin({"cookie": "hdh_session=placeholder", "notify": False,
                       "use_browserless": True,
                       "browserless_url": "https://browser.example/function?token=placeholder"})
        with patch.object(module.logger, "info") as info:
            p._session().close()
        self.assertEqual(clients[0].proxies["https"], "http://user:secret@example:1")
        self.assertNotIn("secret", str(info.call_args_list))
        self.assertNotIn("placeholder", str(info.call_args_list))

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
        plugin.init_plugin({"cookie": "hdh_session=placeholder", "notify": True})
        session = Session()
        session.get = Mock(side_effect=RuntimeError("private-proxy-url"))
        plugin._session = lambda: session
        with patch.object(module.logger, "warning") as warning:
            result = plugin._run(manual=True)
        self.assertIn("登录预检", str(warning.call_args_list))
        self.assertNotIn("private-proxy-url", str(warning.call_args_list))
        self.assertNotIn("private-proxy-url", result["message"])
        self.assertNotIn("private-proxy-url", str(plugin.notices))
        self.assertEqual(session.posts, [])

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
