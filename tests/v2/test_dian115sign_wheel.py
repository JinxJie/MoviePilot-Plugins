"""Offline integration tests: no real site calls or paid lottery requests."""
import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "plugins.v2/dian115sign"

class Base:
    def __init__(self):
        self.data = {}
        self.notices = []
    def get_data(self, key):
        return self.data.get(key)
    def save_data(self, key, value):
        self.data[key] = value
    def update_config(self, value):
        self.config = value
    def post_message(self, **value):
        self.notices.append(value)

modules = {key: types.ModuleType(key) for key in (
    "app", "app.core", "app.core.context", "app.log", "app.schemas", "app.schemas.types",
    "app.plugins", "apscheduler", "apscheduler.triggers", "apscheduler.triggers.cron")}
modules["app.core.context"].Context = object
modules["app.log"].logger = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                                  error=lambda *a: None, debug=lambda *a: None)
modules["app.schemas.types"].EventType = types.SimpleNamespace(PluginAction="plugin")
modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Plugin="plugin")
modules["app.plugins"]._PluginBase = Base
modules["apscheduler.triggers.cron"].CronTrigger = types.SimpleNamespace(from_crontab=lambda x: x)
with patch.dict(sys.modules, modules):
    spec = importlib.util.spec_from_file_location("dian115sign", PACKAGE / "__init__.py",
                                                  submodule_search_locations=[str(PACKAGE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dian115sign"] = mod
    spec.loader.exec_module(mod)
sys.modules["dian115sign"] = mod
Sign = mod.Dian115Sign

class SignWheelTests(unittest.TestCase):
    def setUp(self):
        self.p = Sign()
        self.p._enabled = True
        self.p._token = "fake-test-token"
        self.p._email = self.p._password = ""
        self.p._notify = True
        self.p._retry = 0
        self.p._lucky_mode = False
        self.p._cron = "30 9 * * *"
        self.p._proxy = ""
        self.p._use_system_proxy = True
        self.p._onlyonce = False
        self.p.save_data("dian115_records", [])
        self.p._maximum_wheel = True

    def mock_site(self, *, used=18, maximum=20, cost=5, points=100, fail=None, stale=False):
        calls = []
        def request(method, path, retry=True):
            calls.append((method, path))
            if path.endswith("/games/status"):
                return {"items": {"daily_wheel": {"used_today": used if stale else used + len([x for x in calls if x[0] == "POST"]),
                                                     "max_plays": maximum, "cost": cost}}}
            if path.endswith("/me"):
                return {"user": {"points": points - cost * len([x for x in calls if x[0] == "POST"])}}
            if method == "POST":
                if fail:
                    raise RuntimeError(fail)
                return {"prize": {"label": "3 积分", "points": 3, "vip_days": 0}}
            raise AssertionError(path)
        self.p._wheel_request = request
        return calls

    def test_lifecycle_and_disabled_behavior(self):
        with patch.dict(sys.modules, modules):
            self.assertEqual(self.p.get_service()[0]["id"], "Dian115Sign")
            self.assertEqual(self.p.get_api()[0]["methods"], ["GET"])
            self.assertEqual(self.p.get_command()[0]["event"], "plugin")
            self.assertTrue(self.p.get_state())
            self.p.stop_service()
            self.p._maximum_wheel = False
            self.p._ensure_token = lambda: True
            self.p._do_sign = lambda manual=False: {"ok": True, "already": True, "balance": 12}
            self.p._wheel_run = lambda: (_ for _ in ()).throw(AssertionError("关闭转盘不可抽奖"))
            self.p._sign_job(manual=True)
            self.assertNotIn("转盘", self.p.notices[-1]["text"])

    def test_form_only_one_new_switch_default_off_and_config_preserved(self):
        with patch.dict(sys.modules, modules):
            form, defaults = self.p.get_form()
        def walk(node):
            if isinstance(node, list):
                for x in node: yield from walk(x)
            elif isinstance(node, dict):
                if node.get("props", {}).get("model") == "maximum_wheel": yield node
                yield from walk(node.get("content", []))
        self.assertEqual(len(list(walk(form))), 1)
        self.assertFalse(defaults["maximum_wheel"])
        self.p._Dian115Sign__update_config()
        self.assertTrue(self.p.config["maximum_wheel"])

    def test_remaining_bound_even_when_site_counter_stale(self):
        calls = self.mock_site(stale=True)
        out = self.p._wheel_run()
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(len([x for x in calls if x[0] == "POST"]), 2)
        self.assertEqual(len(self.p._wheel_records()), 2)

    def test_missing_limit_or_cost_or_insufficient_points_never_posts(self):
        for kw in ({"maximum": 0}, {"cost": None}, {"points": 8}, {"maximum": None}):
            p = Sign()
            p._maximum_wheel = True
            p._wheel_request = lambda method, path: (_ for _ in ()).throw(AssertionError("POST")) if method == "POST" else (
                {"items": {"daily_wheel": {"used_today": 0, "max_plays": kw.get("maximum", 20), "cost": kw.get("cost", 5)}}}
                if path.endswith("status") else {"user": {"points": kw.get("points", 100)}})
            self.assertEqual(p._wheel_run()["results"], [])

    def test_unknown_post_stops_without_retry_or_record(self):
        calls = self.mock_site(fail="抽奖结果不明，请核对站点积分和次数，勿重试")
        out = self.p._wheel_run()
        self.assertEqual(out["results"], [])
        self.assertEqual(len([x for x in calls if x[0] == "POST"]), 1)
        self.assertIn("勿重试", out["error"])
        self.assertTrue(self.p.get_data("dian115_wheel_last")["uncertain"])
        self.p._wheel_request = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不得再次请求站点"))
        blocked = self.p._wheel_run()
        self.assertEqual(blocked["results"], [])
        self.assertIn("今日不再自动抽奖", blocked["error"])

    def test_success_clears_uncertain_guard(self):
        self.mock_site(used=19, maximum=20)
        self.assertEqual(len(self.p._wheel_run()["results"]), 1)
        self.assertFalse(self.p.get_data("dian115_wheel_last")["uncertain"])

    def test_sign_failed_skips_wheel_and_success_and_already_run(self):
        self.p._ensure_token = lambda: True
        self.p._wheel_run = lambda: {"status": "完成", "results": [{"label": "3 积分", "cost": 5}], "error": ""}
        self.p._fetch_me = lambda: {"points": 123}
        for ok, already, expected in ((False, False, 0), (True, False, 1), (True, True, 1)):
            count = []
            self.p._wheel_run = lambda: (count.append(1) or {"status": "完成", "results": [{"label": "3 积分", "cost": 5}], "error": ""})
            self.p._do_sign = lambda manual=False: {"ok": ok, "already": already, "msg": "ok", "balance": 999, "date": "2026-11-10"}
            self.p._sign_job(manual=True)
            self.assertEqual(len(count), expected)
            if expected:
                text = self.p.notices[-1]["text"]
                self.assertIn("当前积分：123", text)
                self.assertNotIn("当前余额：999", text)
                self.assertIn("转盘：完成", text)
        self.p._notify = False
        self.p._sign_job(manual=True)
        self.assertEqual(len(self.p.notices), 3)

    def test_failed_final_balance_is_not_stale(self):
        self.p._ensure_token = lambda: True
        self.p._do_sign = lambda manual=False: {"ok": True, "already": True, "msg": "ok", "balance": 999}
        self.p._wheel_run = lambda: {"status": "未抽奖", "results": [], "error": "次数已满"}
        self.p._fetch_me = lambda: None
        self.p._sign_job()
        text = self.p.notices[0]["text"]
        self.assertIn("当前积分：获取失败", text)
        self.assertNotIn("当前余额：999", text)
        self.assertIn("次数已满", text)

    def test_bootstrap_and_paid_request_never_retry(self):
        class Response:
            status_code = 200
            def __init__(self, payload): self.payload = payload
            def json(self): return self.payload
        class HTTP:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append(("GET", url))
                return Response({"code": "ok", "proof": "test-proof", "ttl": 600})
            def post(self, url, **kwargs):
                self.calls.append(("POST", url))
                return Response({"code": "ok", "session_id": "test-session", "signature_version": 2, "ttl": 1800})
            def request(self, method, url, **kwargs):
                self.calls.append((method, url))
                return Response({"code": "browser_proof_invalid"})
        http = HTTP()
        self.p._http = lambda: http
        with self.assertRaisesRegex(RuntimeError, "结果不明"):
            self.p._wheel_request("POST", self.p.API_BASE + "/lottery/wheel")
        paid = [call for call in http.calls if call[1].endswith("/lottery/wheel")]
        self.assertEqual(len(paid), 1)
        self.assertEqual(http.calls[0][0], "GET")
        self.assertTrue(http.calls[0][1].endswith("/auth/browser-challenge"))
        self.assertTrue(http.calls[1][1].endswith("/auth/browser-session"))

    def test_page_balance_failure_never_uses_stored_balance(self):
        self.p._fetch_me = lambda: None
        self.p.save_data("dian115_wheel_records", [{"date": mod.datetime.now().isoformat(), "label": "甲", "balance": 999}])
        page = self.p.get_page()
        self.assertIn("获取失败", str(page))
        self.assertNotIn("'text': '999'", str(page))

    def test_pairs_of_prizes_and_page_status(self):
        rows = [{"label": s, "cost": 5} for s in ("甲", "乙", "丙")]
        text = "\n".join(self.p._wheel_summary({"status": "提前结束", "results": rows, "error": "积分不足"}))
        self.assertIn("甲  ｜  乙\n丙", text)
        self.p.save_data("dian115_wheel_records", [{"date": mod.datetime.now().isoformat(), "label": "甲"}])
        self.p.save_data("dian115_wheel_last", {"date": mod.datetime.now().strftime("%Y-%m-%d"), "status": "提前结束", "error": "积分不足"})
        self.p._fetch_me = lambda: {"points": 123, "nickname": "test"}
        page = self.p.get_page()
        self.assertIn("今日转盘", str(page))
        self.assertIn("积分不足", str(page))
        self.assertIn("123", str(page))

    def test_v2_signature_raw_and_authenticated_headers(self):
        self.p._wheel_key = ec.generate_private_key(ec.SECP256R1())
        self.p._wheel_session_id = "test-session"
        self.p._wheel_signature_version = 2
        self.p._wheel_proof = "test-proof"
        headers = self.p._wheel_headers("POST", self.p.API_BASE + "/lottery/wheel")
        self.assertEqual(headers["X-Portal-Browser-Version"], "2")
        self.assertEqual(headers["Cookie"], "__Host-portal_token=fake-test-token")
        raw = base64.urlsafe_b64decode(headers["X-Portal-Browser-Sig"] + "==")
        self.assertEqual(len(raw), 64)
        msg = f"portal-browser-request/v2\ntest-session\nPOST\n{self.p.API_BASE}/lottery/wheel\n{headers['X-Portal-Browser-TS']}\n{headers['X-Portal-Browser-Nonce']}".encode()
        sig = encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'))
        self.p._wheel_key.public_key().verify(sig, msg, ec.ECDSA(hashes.SHA256()))

if __name__ == "__main__":
    unittest.main()
