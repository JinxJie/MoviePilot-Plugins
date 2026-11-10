"""离线单元测试：绝不向癫影站点发送真实抽奖请求。"""
import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


class _Base:
    def __init__(self):
        self.data = {}
        self.notices = []

    def get_data(self, key):
        return self.data.get(key)

    def save_data(self, key, value):
        self.data[key] = value

    def post_message(self, **kw):
        self.notices.append(kw)

    def update_config(self, config):
        self.saved_config = config


def _install_stubs():
    modules = {key: types.ModuleType(key) for key in (
        "apscheduler", "apscheduler.triggers", "apscheduler.triggers.cron",
        "app", "app.plugins", "app.sdk", "app.sdk.config", "app.sdk.logging",
        "app.schemas", "app.schemas.types")}
    setattr(modules["apscheduler.triggers.cron"], "CronTrigger", types.SimpleNamespace(from_crontab=lambda x: x))
    setattr(modules["app.plugins"], "_PluginBase", _Base)
    setattr(modules["app.sdk.config"], "settings", types.SimpleNamespace(PROXY=types.SimpleNamespace(host="")))
    setattr(modules["app.sdk.logging"], "logger", types.SimpleNamespace(warning=lambda *a, **kw: None))
    setattr(modules["app.schemas.types"], "NotificationType", types.SimpleNamespace(Plugin="Plugin"))
    return patch.dict(sys.modules, modules)


with _install_stubs():
    plugin_file = Path(__file__).resolve().parents[3] / "plugins.v3/dian115wheel/__init__.py"
    spec = importlib.util.spec_from_file_location("dian115wheel_test_module", plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
Dian115Wheel = module.Dian115Wheel


class WheelTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Dian115Wheel()
        self.plugin._token = "fake-for-test-only"
        self.plugin._notify = False

    def test_system_proxy_dict_is_used_by_http_session(self):
        class FakeSession:
            def __init__(self, **kwargs):
                self.proxies = {}
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(url)

        settings = module.settings
        with patch.object(settings, "PROXY", {"http": "http://http.example:8080",
                                              "https": "http://https.example:8080"}), \
                patch.dict(module.os.environ, {}, clear=True), \
                patch.object(module, "curl_requests", types.SimpleNamespace(Session=FakeSession)):
            session = self.plugin._http()
        self.assertEqual(session.proxies, {"http": "http://https.example:8080",
                                           "https": "http://https.example:8080"})
        self.assertEqual(len(session.calls), 1)

    def test_system_proxy_formats_and_fallback(self):
        cases = [
            ({"http": "http://http.example:8080"}, {}, "http://http.example:8080"),
            ({"http": "http://http.example:8080", "https": ""}, {}, "http://http.example:8080"),
            (types.SimpleNamespace(host="http://legacy.example:8080"), {}, "http://legacy.example:8080"),
            ({}, {"PROXY_HOST": "http://env.example:8080"}, "http://env.example:8080"),
            (None, {}, ""),
        ]
        for raw, env, expected in cases:
            with self.subTest(raw_type=type(raw).__name__, expected=expected), \
                    patch.object(module.settings, "PROXY", raw), \
                    patch.dict(module.os.environ, env, clear=True):
                self.assertEqual(self.plugin._system_proxy(), expected)

    def test_custom_proxy_when_system_proxy_disabled(self):
        class FakeSession:
            def __init__(self, **kwargs):
                self.proxies = {}

            def get(self, url, **kwargs):
                pass

        self.plugin._use_system_proxy = False
        self.plugin._proxy = "http://custom.example:8080"
        with patch.object(module.settings, "PROXY", {"https": "http://system.example:8080"}), \
                patch.object(module, "curl_requests", types.SimpleNamespace(Session=FakeSession)):
            session = self.plugin._http()
        self.assertEqual(session.proxies, {"http": "http://custom.example:8080",
                                           "https": "http://custom.example:8080"})

    def test_defaults_are_inert_and_v3_manifest_form(self):
        p = Dian115Wheel()
        self.assertFalse(p.get_state())
        self.assertEqual(p.get_service(), [])
        form, defaults = p.get_form()
        self.assertFalse(defaults["enabled"])
        self.assertFalse(defaults["onlyonce"])
        self.assertEqual(defaults["max_spins"], 1)
        self.assertFalse(defaults["maximum_spins"])
        fields = form[0]["content"]
        by_model = {f.get("props", {}).get("model"): f for f in fields if "model" in f.get("props", {})}
        self.assertEqual(by_model["max_spins"]["component"], "VTextField")
        self.assertEqual(by_model["max_spins"]["props"]["label"], "自定义次数")
        self.assertEqual(by_model["maximum_spins"]["component"], "VSwitch")
        self.assertEqual(by_model["maximum_spins"]["props"]["label"], "最大次数")
        self.assertFalse(any(f["component"] == "VSelect" for f in fields))
        self.assertEqual(form[0]["component"], "VForm")
        self.assertEqual(p.get_api()[0]["methods"], ["POST"])

    def test_v2_signature_is_raw_webcrypto_format(self):
        p = self.plugin
        p._key = ec.generate_private_key(ec.SECP256R1())
        p._session_id = "session-test"
        p._signature_version = 2
        with patch.object(module.time, "time", return_value=1700000000), patch.object(module.os, "urandom", return_value=b"1" * 24):
            headers = p._signed_headers("GET", p.STATUS)
        self.assertEqual(headers["X-Portal-Browser-Version"], "2")
        signature = base64.urlsafe_b64decode(headers["X-Portal-Browser-Sig"] + "==")
        self.assertEqual(len(signature), 64)
        message = f"portal-browser-request/v2\nsession-test\nGET\n{p.STATUS}\n{headers['X-Portal-Browser-TS']}\n{headers['X-Portal-Browser-Nonce']}".encode()
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
        p._key.public_key().verify(der, message, ec.ECDSA(hashes.SHA256()))

    def test_lifecycle_and_one_shot_config_reset(self):
        p = Dian115Wheel()
        with patch.object(module.threading, "Thread") as worker:
            p.init_plugin({"enabled": False, "token": "__Host-portal_token=test-value", "onlyonce": True,
                           "max_spins": 2, "min_balance": 20})
        self.assertEqual(p.saved_config["onlyonce"], False)
        self.assertEqual(p._token, "test-value")
        self.assertEqual(p._max_spins, 2)
        self.assertEqual(p.saved_config["spin_mode"], "custom")
        self.assertFalse(p.saved_config["maximum_spins"])
        self.assertEqual(p._min_balance, 20)
        self.assertEqual(p.get_service(), [])
        worker.assert_called_once()
        session = types.SimpleNamespace(close=lambda: None)
        p._session = session
        p.stop_service()
        self.assertIsNone(p._session)

    def test_bad_cookie_does_not_become_token(self):
        self.assertEqual(Dian115Wheel._extract_token("other=abc; browser=xyz"), "")
        self.assertEqual(Dian115Wheel._extract_token("__Host-portal_token=expected; other=xyz"), "expected")

    def test_browser_session_v2_challenge_and_signed_get(self):
        p = self.plugin
        class Response:
            status_code = 200
            def __init__(self, body):
                self.body = body
            def raise_for_status(self):
                pass
            def json(self):
                return self.body
        class HTTP:
            def __init__(self):
                self.calls = []
            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return Response({"proof": "test-proof", "ttl": 600})
            def post(self, url, **kwargs):
                self.calls.append(("POST", url, kwargs))
                return Response({"code": "ok", "signature_version": 2,
                                 "session_id": "test-session", "ttl": 1800})
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return Response({"code": "ok", "items": {"daily_wheel": {"cost": 5}}})
        http = HTTP()
        p._http = lambda: http
        result = p._request("GET", p.STATUS)
        self.assertEqual(result["items"]["daily_wheel"]["cost"], 5)
        self.assertEqual(http.calls[1][2]["json"]["signature_version"], 2)
        self.assertEqual(http.calls[2][2]["headers"]["X-Portal-Browser-Session"], "test-session")
        self.assertEqual(http.calls[2][2]["headers"]["X-Portal-Browser-Proof"], "test-proof")

    def test_bootstrap_http_errors_report_stage_status_without_secrets_or_paid_post(self):
        class FakeResponse:
            def __init__(self, status):
                self.status_code = status
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise ValueError("secret-token-in-exception")
            def json(self):
                return {"proof": "test-proof", "ttl": 600}
        for stage, status in (("challenge", 403), ("session", 429)):
            with self.subTest(stage=stage):
                p = Dian115Wheel()
                p._token = "secret-token-in-exception"
                calls = []
                class FakeHTTP:
                    def get(self, url, **kw):
                        calls.append("challenge")
                        return FakeResponse(403 if stage == "challenge" else 200)
                    def post(self, url, **kw):
                        calls.append("session")
                        return FakeResponse(429)
                    def request(self, *args, **kw):
                        calls.append("paid")
                        raise AssertionError("no business request expected")
                p._http = lambda: FakeHTTP()
                result = p._run()
                self.assertEqual(result["code"], 1)
                self.assertIn(f"browser-{stage}", result["msg"])
                self.assertIn(f"HTTP {status}", result["msg"])
                self.assertNotIn("secret-token-in-exception", result["msg"])
                self.assertNotIn("paid", calls)

    def test_bootstrap_transport_error_does_not_reflect_sensitive_details(self):
        p = Dian115Wheel()
        p._token = "secret-token-in-exception"
        class FakeHTTP:
            def get(self, url, **kw):
                raise ValueError("secret-token-in-exception")
        p._http = lambda: FakeHTTP()
        result = p._run()
        self.assertIn("browser-challenge", result["msg"])
        self.assertNotIn("secret-token-in-exception", result["msg"])

    def test_read_only_checks_prevent_spending_below_balance(self):
        p = self.plugin
        p._min_balance = 10
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
            return {"user": {"points": 12}}
        p._request = fake
        result = p._run()
        self.assertEqual(result["code"], 1)
        self.assertNotIn(("POST", p.WHEEL), calls)

    def test_daily_limit_prevents_spending(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append(path)
            return {"items": {"daily_wheel": {"used_today": 2, "max_plays": 2, "cost": 5}}}
        p._request = fake
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(calls, [p.STATUS])

    def test_success_records_and_balances(self):
        p = self.plugin
        p._max_spins = 1
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 1, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        result = p._run()
        self.assertEqual(result["code"], 0)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)
        self.assertEqual(p.data["wheel_records"][0]["points"], 3)
        self.assertTrue(p.get_page())

    def test_current_points_page_is_read_only_and_never_uses_old_balance(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            return {"user": {"points": 12345678901234567890}}
        p._request = fake
        page = p.get_page()
        self.assertEqual(calls, [("GET", p.ME)])
        self.assertEqual(page[0]["content"][0]["content"][0]["content"][0]["content"][0]["text"], "12345678901234567890")
        self.assertIn("当前积分", page[0]["content"][0]["content"][0]["content"][0]["content"][1]["text"])
        p._request = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline"))
        page = p.get_page()
        self.assertEqual(page[0]["content"][0]["content"][0]["content"][0]["content"][0]["text"], "获取失败")

    def test_notification_pairs_prizes_and_shows_balance(self):
        p = self.plugin
        p._notify = True
        p._max_spins = 3
        calls = []
        prizes = iter(("3 积分", "5 积分", "谢谢参与"))
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 20, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100 if calls.count(("POST", p.WHEEL)) < 3 else 91}}
            return {"cost": 5, "prize": {"label": next(prizes), "points": 0}}
        p._request = fake
        self.assertEqual(len(p._run()["data"]), 3)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 3)
        self.assertEqual(len(p.notices), 1)
        self.assertEqual(p.notices[0]["title"], "癫影幸运转盘 · 抽奖完成")
        self.assertEqual(p.notices[0]["text"],
                         "🎲 本轮抽奖：3 次  ｜  消耗积分：15\n💰 当前积分：91\n\n"
                         "🎁 奖品结果\n3 积分  ｜  5 积分\n谢谢参与")

    def test_notification_balance_failure_is_explicit_and_stop_reason_retained(self):
        p = self.plugin
        p._notify = True
        p._max_spins = 2
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                if calls.count(("POST", p.WHEEL)):
                    raise RuntimeError("站点暂停")
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 20, "cost": 5}}}
            if path == p.ME:
                if calls.count(("POST", p.WHEEL)):
                    raise RuntimeError("余额不可读")
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "3 积分", "points": 3}}
        p._request = fake
        p._run()
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)
        self.assertIn("💰 当前积分：获取失败", p.notices[0]["text"])
        self.assertIn("⚠️ 提前结束：站点暂停", p.notices[0]["text"])

    def test_invalid_points_are_not_presented_as_balance(self):
        p = self.plugin
        for value in (None, True, -1):
            with self.subTest(value=value):
                p._request = lambda *a, **kw: {"user": {"points": value}}
                with self.assertRaises(ValueError):
                    p._current_points()

    def test_missing_token_page_does_not_fetch_balance(self):
        p = Dian115Wheel()
        p._request = lambda *a, **kw: self.fail("missing token must not fetch")
        self.assertEqual(p.get_page()[0]["content"][0]["content"][0]["content"][0]["content"][0]["text"], "获取失败")

    def test_switch_mode_resolution_and_legacy_configuration(self):
        for config, expected in (
            ({}, "custom"),
            ({"spin_mode": "maximum"}, "maximum"),
            ({"maximum_spins": False}, "custom"),
            ({"maximum_spins": True}, "maximum"),
            ({"spin_mode": "maximum", "maximum_spins": False}, "custom"),
        ):
            with self.subTest(config=config):
                p = Dian115Wheel()
                p.init_plugin(config)
                self.assertEqual(p._spin_mode, expected)

    def test_maximum_mode_uses_remaining_site_quota_and_ignores_custom_count(self):
        p = self.plugin
        p._spin_mode = "maximum"
        p._max_spins = 1
        used = 17
        calls = []
        def fake(method, path, **kw):
            nonlocal used
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": used, "max_plays": 20, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            used += 1
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        result = p._run()
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["data"]), 3)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 3)
        self.assertEqual(calls.count(("GET", p.STATUS)), 4)

    def test_maximum_mode_stops_at_initial_remaining_when_site_count_is_stale(self):
        p = self.plugin
        p._spin_mode = "maximum"
        p._max_spins = 1
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 18, "max_plays": 20, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        self.assertEqual(len(p._run()["data"]), 2)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 2)

    def test_maximum_mode_respects_balance_limit(self):
        p = self.plugin
        p.init_plugin({"token": "test-token", "maximum_spins": True, "max_spins": 1,
                       "min_balance": 10})
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 20, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 12}}
            self.fail("balance guard should prevent POST")
        p._request = fake
        result = p._run()
        self.assertEqual(result["code"], 1)
        self.assertEqual(calls, [("GET", p.STATUS), ("GET", p.ME)])

    def test_custom_mode_ignores_remaining_quota_above_configured_count(self):
        p = self.plugin
        p._spin_mode = "custom"
        p._max_spins = 2
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 20, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        self.assertEqual(len(p._run()["data"]), 2)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 2)

    def test_missing_cost_prevents_spending(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2}}}
        p._request = fake
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(calls, [("GET", p.STATUS)])

    def test_ambiguous_post_response_never_retries_or_records_success(self):
        p = self.plugin
        p._max_spins = 5
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 5, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"code": "ok"}
        p._request = fake
        response = p._run()
        self.assertEqual(response["code"], 1)
        self.assertIn("勿重试", response["msg"])
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)
        self.assertFalse(p.get_data("wheel_records"))

    def test_post_error_never_retries(self):
        p = self.plugin
        p._max_spins = 5
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 5, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            raise RuntimeError("unexpected response")
        p._request = fake
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)


if __name__ == "__main__":
    unittest.main()
