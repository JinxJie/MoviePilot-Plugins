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
        self._sleep = patch.object(module.time, "sleep", return_value=None)
        self._jitter = patch.object(module.random, "uniform", return_value=15.0)
        self._sleep.start()
        self._jitter.start()
        self.addCleanup(self._sleep.stop)
        self.addCleanup(self._jitter.stop)

    def test_pacing_uses_random_minimum_interval_before_next_read_and_across_games(self):
        p = self.plugin
        p._max_spins = 2
        p._monopoly_enabled = True
        p._monopoly_max_spins = 1
        calls = []
        clock = [100.0]
        def fake(method, path, **kw):
            calls.append((method, path, clock[0]))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 3, "cost": 5},
                                  "monopoly": {"used_today": 0, "max_plays": 3, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            if path == p.WHEEL:
                return {"cost": 5, "prize": {"label": "+2", "points": 2}}
            if path == p.MONOPOLY:
                return {"award_points": 2, "steps": [{"pos": 1}]}
            self.fail("unexpected endpoint")
        p._request = fake
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(module.time, "time", side_effect=lambda: clock[0]), \
             patch.object(module.time, "sleep", side_effect=sleep), \
             patch.object(module.random, "uniform", side_effect=[17.0, 19.0]) as jitter:
            self.assertEqual(len(p._run()["data"]), 3)
        self.assertEqual(jitter.call_count, 2)
        self.assertEqual([t for m, path, t in calls if m == "POST"], [100.0, 117.0, 136.0])
        self.assertEqual([t for m, path, t in calls if m == "GET" and path == p.STATUS], [100.0, 117.0, 136.0])

    def test_cooldown_survives_new_plugin_instance(self):
        p = self.plugin
        calls = []
        clock = [1000.0]
        def fake(method, path, **kw):
            calls.append((method, path, clock[0]))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            if path == p.WHEEL:
                return {"cost": 5, "prize": {"label": "+2", "points": 2}}
            self.fail("unexpected endpoint")
        p._request = fake
        with patch.object(module.time, "time", side_effect=lambda: clock[0]), \
             patch.object(module.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(module.random, "uniform", return_value=17.0):
            self.assertEqual(len(p._run()["data"]), 1)
            self.assertEqual(p.get_data("last_paid_attempt_at"), 1000.0)
            restarted = Dian115Wheel()
            restarted.data = p.data
            restarted._token = p._token
            restarted._notify = False
            restarted._request = fake
            clock[0] = 1003.0
            self.assertEqual(len(restarted._run()["data"]), 1)
        self.assertEqual([t for m, path, t in calls if m == "POST"], [1000.0, 1017.0])
        self.assertEqual([t for m, path, t in calls if m == "GET" and path == p.STATUS], [1000.0, 1017.0])

    def test_cooldown_storage_failure_never_posts(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            self.fail("should never POST")
        p._request = fake
        original = p.save_data
        p.save_data = lambda key, value: None if key == "last_paid_attempt_at" else original(key, value)
        self.assertEqual(p._run()["code"], 1)
        self.assertNotIn(("POST", p.WHEEL), calls)
        self.assertIsNotNone(p.get_data("wheel_pending"))

    def test_rate_limited_post_is_not_retried_and_keeps_pending(self):
        p = self.plugin
        p._wheel_enabled = False
        p._monopoly_enabled = True
        p._monopoly_max_spins = 3
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"monopoly": {"used_today": 0, "max_plays": 3, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            if path == p.MONOPOLY:
                if calls.count(("POST", p.MONOPOLY)) == 1:
                    return {"award_points": 2, "steps": [{"pos": 1}]}
                raise RuntimeError("站点接口失败：HTTP 400，代码 rate_limited")
            self.fail("unexpected endpoint")
        p._request = fake
        with patch.object(module.time, "sleep"), patch.object(module.random, "uniform", return_value=15):
            result = p._run()
        self.assertEqual(len(result["data"]), 1)
        self.assertIn("rate_limited", result["msg"])
        self.assertEqual(calls.count(("POST", p.MONOPOLY)), 2)
        self.assertIsNotNone(p.get_data("monopoly_pending"))
        p._run()
        self.assertEqual(calls.count(("POST", p.MONOPOLY)), 2)

    def test_success_notification_waits_before_balance_read(self):
        p = self.plugin
        p._notify = True
        calls = []
        clock = [100.0]
        def fake(method, path, **kw):
            calls.append((method, path, clock[0]))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 1, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            if path == p.WHEEL:
                return {"cost": 5, "prize": {"label": "+2", "points": 2}}
            self.fail("unexpected endpoint")
        p._request = fake
        with patch.object(module.time, "time", side_effect=lambda: clock[0]), \
             patch.object(module.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(module.random, "uniform", return_value=18.0) as jitter:
            self.assertEqual(len(p._run()["data"]), 1)
        jitter.assert_called_once_with(15.0, 25.0)
        self.assertEqual([t for m, path, t in calls if path == p.ME], [100.0, 118.0])
        self.assertIn("当前积分：100", p.notices[0]["text"])

    def test_rate_limited_notification_skips_balance_read(self):
        for game in ("wheel", "monopoly"):
            with self.subTest(game=game):
                p = Dian115Wheel()
                p.init_plugin({"token": "test-token", "game": game, "max_spins": 2,
                               "monopoly_max_spins": 2, "notify": True})
                calls = []
                def fake(method, path, **kw):
                    calls.append((method, path))
                    paid = p.WHEEL if game == "wheel" else p.MONOPOLY
                    if path == p.STATUS:
                        item = "daily_wheel" if game == "wheel" else "monopoly"
                        return {"items": {item: {"used_today": 0, "max_plays": 3, "cost": 5}}}
                    if path == p.ME:
                        return {"user": {"points": 100}}
                    if path == paid:
                        if calls.count(("POST", paid)) == 1:
                            return ({"prize": {"label": "+2", "points": 2}, "cost": 5}
                                    if game == "wheel" else {"award_points": 2, "steps": [{"pos": 1}]})
                        raise RuntimeError("站点接口失败：HTTP 429，代码 rate_limited")
                    self.fail("unexpected endpoint")
                p._request = fake
                with patch.object(module.time, "sleep"), patch.object(module.random, "uniform", return_value=15):
                    result = p._run()
                self.assertEqual(len(result["data"]), 1)
                self.assertEqual(calls.count(("GET", p.ME)), 2)
                self.assertIn("当前积分：获取失败", p.notices[0]["text"])
                self.assertIn("rate_limited", p.notices[0]["text"])

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
        self.assertEqual(by_model["max_spins"]["props"]["label"], "转盘 · 自定义次数")
        self.assertEqual(by_model["maximum_spins"]["component"], "VSwitch")
        self.assertEqual(by_model["maximum_spins"]["props"]["label"], "转盘 · 最大次数")
        self.assertEqual(by_model["wheel_enabled"]["component"], "VSwitch")
        self.assertEqual(by_model["monopoly_enabled"]["component"], "VSwitch")
        self.assertTrue(defaults["wheel_enabled"])
        self.assertFalse(defaults["monopoly_enabled"])
        self.assertEqual(defaults["monopoly_max_spins"], 1)
        self.assertFalse(defaults["monopoly_maximum_spins"])
        self.assertEqual(defaults["monopoly_min_balance"], 5)
        self.assertFalse(any(f["component"] == "VSelect" for f in fields))
        self.assertNotIn("spin_mode", by_model)
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

    def test_each_game_uses_own_count_and_balance(self):
        p = Dian115Wheel()
        p.init_plugin({"token": "test-token", "wheel_enabled": True, "monopoly_enabled": True,
                       "max_spins": 1, "min_balance": 5, "monopoly_max_spins": 2,
                       "monopoly_min_balance": 50, "notify": False})
        calls = []
        def fake(method, path, **kwargs):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 5, "cost": 5},
                                  "monopoly": {"used_today": 0, "max_plays": 5, "cost": 7}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            if path == p.WHEEL:
                return {"cost": 5, "prize": {"label": "积分", "points": 2}}
            if path == p.MONOPOLY:
                return {"award_points": 3, "steps": [{"pos": 1}]}
            self.fail("unexpected endpoint")
        p._request = fake
        self.assertEqual(p._run()["code"], 0)
        self.assertEqual([path for method, path in calls if method == "POST"],
                         [p.WHEEL, p.MONOPOLY, p.MONOPOLY])
        p2 = Dian115Wheel()
        p2.init_plugin({"token": "test-token", "wheel_enabled": True, "monopoly_enabled": True,
                        "max_spins": 1, "min_balance": 100, "monopoly_max_spins": 1,
                        "monopoly_min_balance": 90, "notify": False})
        calls.clear()
        p2._request = fake
        self.assertEqual(p2._run()["code"], 0)
        self.assertEqual([path for method, path in calls if method == "POST"], [p.MONOPOLY])

    def test_independent_game_settings_and_legacy_wheel_config(self):
        p = Dian115Wheel()
        p.init_plugin({"token": "test-token", "max_spins": 2, "maximum_spins": True,
                       "min_balance": 18, "monopoly_max_spins": 4,
                       "monopoly_maximum_spins": False, "monopoly_min_balance": 36})
        self.assertEqual((p._max_spins, p._spin_mode, p._min_balance), (2, "maximum", 18))
        self.assertEqual((p._monopoly_max_spins, p._monopoly_spin_mode, p._monopoly_min_balance),
                         (4, "custom", 36))
        form, defaults = p.get_form()
        fields = form[0]["content"]
        wheel = next(f for f in fields if f.get("props", {}).get("model") == "wheel_enabled")
        monopoly = next(f for f in fields if f.get("props", {}).get("model") == "monopoly_enabled")
        self.assertLess(fields.index(wheel), fields.index(monopoly))
        self.assertEqual([f["props"]["model"] for f in fields[fields.index(wheel)+1:fields.index(monopoly)]],
                         ["max_spins", "maximum_spins", "min_balance"])
        self.assertEqual([f["props"]["model"] for f in fields[fields.index(monopoly)+1:fields.index(monopoly)+4]],
                         ["monopoly_max_spins", "monopoly_maximum_spins", "monopoly_min_balance"])
        self.assertEqual(defaults["max_spins"], defaults["monopoly_max_spins"])
        legacy = Dian115Wheel()
        legacy.init_plugin({"token": "test-token", "game": "monopoly", "max_spins": 3,
                            "maximum_spins": True, "min_balance": 27})
        self.assertEqual((legacy._monopoly_max_spins, legacy._monopoly_spin_mode,
                          legacy._monopoly_min_balance), (3, "maximum", 27))
        with patch.object(module.threading, "Thread"):
            legacy.init_plugin({"token": "test-token", "game": "monopoly", "onlyonce": True,
                                "max_spins": 3, "maximum_spins": True, "min_balance": 27,
                                "monopoly_max_spins": 4, "monopoly_maximum_spins": False,
                                "monopoly_min_balance": 36})
        self.assertEqual((legacy.saved_config["monopoly_max_spins"],
                          legacy.saved_config["monopoly_maximum_spins"],
                          legacy.saved_config["monopoly_min_balance"]), (4, False, 36))

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
        self.assertEqual(p.notices[0]["title"], "癫影娱乐 · 转盘完成")
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

    def test_custom_mode_stops_at_initial_remaining_when_site_count_is_stale(self):
        p = self.plugin
        p._spin_mode = "custom"
        p._max_spins = 5
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

    def test_stale_wheel_quota_across_runs_does_not_exceed_daily_limit(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 1, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        self.assertEqual(p._run()["code"], 0)
        self.assertEqual(p._run()["code"], 1)
        restarted = Dian115Wheel()
        restarted.data = p.data
        restarted._token = p._token
        restarted._notify = False
        restarted._request = fake
        self.assertEqual(restarted._run()["code"], 1)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)

    def test_wheel_pending_storage_failure_never_spends(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            self.fail("pending write failure must prevent POST")
        p._request = fake
        p.save_data = lambda key, value: None
        self.assertEqual(p._run()["code"], 1)
        self.assertNotIn(("POST", p.WHEEL), calls)

    def test_wheel_record_failure_keeps_pending_guard(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        original = p.save_data
        p.save_data = lambda key, value: original(key, value) if key != "wheel_records" else None
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)
        self.assertEqual(p.get_data("wheel_pending"), module.datetime.now().date().isoformat())

    def test_wheel_progress_failure_keeps_pending_guard(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"cost": 5, "prize": {"label": "+3", "points": 3}}
        p._request = fake
        original = p.save_data
        p.save_data = lambda key, value: original(key, value) if key != "wheel_progress" else None
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)
        self.assertEqual(p.get_data("wheel_pending"), module.datetime.now().date().isoformat())

    def test_ambiguous_wheel_response_blocks_later_run_and_restart(self):
        p = self.plugin
        calls = []
        def fake(method, path, **kw):
            calls.append((method, path))
            if path == p.STATUS:
                return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 5, "cost": 5}}}
            if path == p.ME:
                return {"user": {"points": 100}}
            return {"code": "ok"}
        p._request = fake
        self.assertEqual(p._run()["code"], 1)
        self.assertEqual(p._run()["code"], 1)
        restarted = Dian115Wheel()
        restarted.data = p.data
        restarted._token = p._token
        restarted._notify = False
        restarted._request = fake
        self.assertEqual(restarted._run()["code"], 1)
        self.assertEqual(calls.count(("POST", p.WHEEL)), 1)

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


class MonopolyTests(unittest.TestCase):
    def setUp(self):
        self.p = Dian115Wheel()
        self.p.init_plugin({"token": "test-token", "game": "monopoly", "notify": False})
        self.calls = []
        self._sleep = patch.object(module.time, "sleep", return_value=None)
        self._jitter = patch.object(module.random, "uniform", return_value=15.0)
        self._sleep.start()
        self._jitter.start()
        self.addCleanup(self._sleep.stop)
        self.addCleanup(self._jitter.stop)

    def fake(self, method, path, **kwargs):
        self.calls.append((method, path))
        if path == self.p.STATUS:
            return {"items": {"monopoly": {"used_today": 0, "max_plays": 2, "cost": 7},
                              "daily_wheel": {"used_today": 0, "max_plays": 99, "cost": 1}}}
        if path == self.p.ME:
            return {"user": {"points": 20}}
        if path == self.p.MONOPOLY:
            return {"award_points": 3, "steps": [{"pos": 1}]}
        self.fail(f"unexpected endpoint: {path}")

    def test_default_wheel_and_config_reset(self):
        original = Dian115Wheel()
        self.assertTrue(original._wheel_enabled)
        self.assertFalse(original._monopoly_enabled)
        with patch.object(module.threading, "Thread"):
            original.init_plugin({"token": "test-token", "game": "monopoly", "onlyonce": True})
        self.assertFalse(original.saved_config["wheel_enabled"])
        self.assertTrue(original.saved_config["monopoly_enabled"])
        original.init_plugin({"token": "test-token", "game": "invalid"})
        self.assertTrue(original._wheel_enabled)
        self.assertFalse(original._monopoly_enabled)

    def test_monopoly_spends_only_selected_game_and_records(self):
        self.p._request = self.fake
        self.assertEqual(self.p._run()["code"], 0)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 1)
        self.assertNotIn(("POST", self.p.WHEEL), self.calls)
        self.assertEqual(self.p.get_data("wheel_records")[0]["game"], "monopoly")
        self.assertIsNone(self.p.get_data("monopoly_pending"))

    def test_both_enabled_run_wheel_then_monopoly(self):
        self.p.init_plugin({"token": "test-token", "wheel_enabled": True, "monopoly_enabled": True, "notify": False})
        def both(method, path, **kwargs):
            if path == self.p.WHEEL:
                self.calls.append((method, path))
                return {"prize": {"label": "积分", "points": 2}, "cost": 5}
            return self.fake(method, path, **kwargs)
        self.p._request = both
        result = self.p._run()
        self.assertEqual(result["code"], 0)
        self.assertEqual([path for method, path in self.calls if method == "POST"], [self.p.WHEEL, self.p.MONOPOLY])
        self.assertEqual(len(result["data"]), 2)

    def test_no_game_enabled_never_sends_paid_request(self):
        self.p.init_plugin({"token": "test-token", "wheel_enabled": False, "monopoly_enabled": False, "notify": False})
        self.p._request = self.fake
        result = self.p._run()
        self.assertEqual(result["code"], 1)
        self.assertEqual(self.calls, [])

    def test_exhausted_wheel_quota_still_checks_monopoly(self):
        self.p.init_plugin({"token": "test-token", "wheel_enabled": True, "monopoly_enabled": True, "notify": False})
        def exhausted(method, path, **kwargs):
            if path == self.p.STATUS:
                self.calls.append((method, path))
                return {"items": {"daily_wheel": {"used_today": 2, "max_plays": 2, "cost": 5},
                                  "monopoly": {"used_today": 0, "max_plays": 1, "cost": 7}}}
            return self.fake(method, path, **kwargs)
        self.p._request = exhausted
        result = self.p._run()
        self.assertEqual(result["code"], 0)
        self.assertEqual([path for method, path in self.calls if method == "POST"], [self.p.MONOPOLY])

    def test_missing_wheel_status_stops_before_monopoly(self):
        self.p.init_plugin({"token": "test-token", "wheel_enabled": True, "monopoly_enabled": True, "notify": False})
        def no_wheel(method, path, **kwargs):
            self.calls.append((method, path))
            return {"items": {"monopoly": {"used_today": 0, "max_plays": 2, "cost": 7}}}
        self.p._request = no_wheel
        result = self.p._run()
        self.assertEqual(result["code"], 1)
        self.assertEqual(self.calls, [("GET", self.p.STATUS)])

    def test_wheel_failure_stops_before_monopoly(self):
        self.p.init_plugin({"token": "test-token", "wheel_enabled": True, "monopoly_enabled": True, "notify": False})
        def ambiguous(method, path, **kwargs):
            if path == self.p.WHEEL:
                self.calls.append((method, path))
                return {"code": "ok"}
            return self.fake(method, path, **kwargs)
        self.p._request = ambiguous
        result = self.p._run()
        self.assertEqual(result["code"], 1)
        self.assertEqual([path for method, path in self.calls if method == "POST"], [self.p.WHEEL])

    def test_missing_cost_or_limit_or_balance_fails_closed(self):
        for field in ("cost", "used_today", "max_plays"):
            with self.subTest(field=field):
                self.calls.clear()
                def missing(method, path, **kw):
                    self.calls.append((method, path))
                    return {"items": {"monopoly": {k: v for k, v in {"cost": 7, "used_today": 0, "max_plays": 2}.items() if k != field}}}
                self.p._request = missing
                self.assertEqual(self.p._run()["code"], 1)
                self.assertEqual(self.calls, [("GET", self.p.STATUS)])
        self.calls.clear()
        self.p._monopoly_min_balance = 15
        self.p._request = self.fake
        self.assertEqual(self.p._run()["code"], 1)
        self.assertNotIn(("POST", self.p.MONOPOLY), self.calls)

    def test_ambiguous_response_persists_guard_across_runs(self):
        self.p._max_spins = 2
        def ambiguous(method, path, **kw):
            if method == "POST":
                self.calls.append((method, path))
                return {"code": "ok"}
            return self.fake(method, path, **kw)
        self.p._request = ambiguous
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.p.get_data("monopoly_pending"), module.datetime.now().date().isoformat())
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 1)

    def test_transport_error_after_post_keeps_guard_and_never_retries(self):
        self.p._max_spins = 3
        def timeout(method, path, **kw):
            if method == "POST":
                self.calls.append((method, path))
                raise TimeoutError("response lost")
            return self.fake(method, path, **kw)
        self.p._request = timeout
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 1)
        self.assertEqual(self.p.get_data("monopoly_pending"), module.datetime.now().date().isoformat())

    def test_maximum_caps_at_initial_quota_despite_stale_counter(self):
        self.p._monopoly_spin_mode = "maximum"
        self.p._monopoly_max_spins = 1
        def stale(method, path, **kw):
            if path == self.p.STATUS:
                self.calls.append((method, path))
                return {"items": {"monopoly": {"used_today": 1, "max_plays": 3, "cost": 7}}}
            return self.fake(method, path, **kw)
        self.p._request = stale
        result = self.p._run()
        self.assertEqual(len(result["data"]), 2)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 2)

    def test_custom_mode_caps_at_initial_quota_despite_stale_counter(self):
        self.p._monopoly_spin_mode = "custom"
        self.p._monopoly_max_spins = 5
        def stale(method, path, **kw):
            if path == self.p.STATUS:
                self.calls.append((method, path))
                return {"items": {"monopoly": {"used_today": 1, "max_plays": 3, "cost": 7}}}
            return self.fake(method, path, **kw)
        self.p._request = stale
        result = self.p._run()
        self.assertEqual(len(result["data"]), 2)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 2)

    def test_stale_site_quota_across_runs_cannot_double_spend(self):
        self.p._request = self.fake
        self.p._monopoly_max_spins = 1
        self.assertEqual(self.p._run()["code"], 0)
        self.assertEqual(self.p._run()["code"], 0)
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 2)
        restarted = Dian115Wheel()
        restarted.data = self.p.data
        restarted.init_plugin({"token": "test-token", "wheel_enabled": False, "monopoly_enabled": True, "notify": False})
        restarted._request = self.fake
        self.assertEqual(restarted._run()["code"], 1)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 2)

    def test_monopoly_progress_storage_failure_keeps_pending_guard(self):
        self.p._request = self.fake
        original = self.p.save_data
        self.p.save_data = lambda key, value: original(key, value) if key != "monopoly_progress" else None
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 1)
        self.assertEqual(self.p.get_data("monopoly_pending"), module.datetime.now().date().isoformat())

    def test_persistence_failure_never_sends_paid_post(self):
        self.p._request = self.fake
        self.p.save_data = lambda key, value: None
        self.assertEqual(self.p._run()["code"], 1)
        self.assertNotIn(("POST", self.p.MONOPOLY), self.calls)

    def test_negative_award_is_recorded_not_misclassified_as_failure(self):
        def negative(method, path, **kw):
            if path == self.p.MONOPOLY:
                self.calls.append((method, path))
                return {"award_points": -2, "steps": [{"pos": 3}]}
            return self.fake(method, path, **kw)
        self.p._request = negative
        self.assertEqual(self.p._run()["code"], 0)
        self.assertEqual(self.p.get_data("wheel_records")[0]["points"], -2)
        self.assertIsNone(self.p.get_data("monopoly_pending"))

    def test_record_storage_failure_keeps_guard_and_stops_next_run(self):
        self.p._request = self.fake
        original = self.p.save_data
        def fail_record(key, value):
            if key != "wheel_records":
                original(key, value)
        self.p.save_data = fail_record
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.p._run()["code"], 1)
        self.assertEqual(self.calls.count(("POST", self.p.MONOPOLY)), 1)
        self.assertEqual(self.p.get_data("monopoly_pending"), module.datetime.now().date().isoformat())

    def test_missing_monopoly_status_never_falls_back_to_wheel(self):
        self.p._request = lambda method, path, **kw: {"items": {"daily_wheel": {"used_today": 0, "max_plays": 10, "cost": 5}}}
        response = self.p._run()
        self.assertEqual(response["code"], 1)
        self.assertIn("未返回大富翁状态", response["msg"])
        self.assertIsNone(self.p.get_data("monopoly_pending"))

    def test_notification_reads_final_balance(self):
        self.p._notify = True
        def fake(method, path, **kw):
            if path == self.p.ME and ("POST", self.p.MONOPOLY) in self.calls:
                self.calls.append((method, path))
                return {"user": {"points": 16}}
            return self.fake(method, path, **kw)
        self.p._request = fake
        self.p._run()
        self.assertIn("当前积分：16", self.p.notices[0]["text"])
        self.assertIn("大富翁奖励 3 积分", self.p.notices[0]["text"])


if __name__ == "__main__":
    unittest.main()
