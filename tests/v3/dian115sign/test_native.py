"""Offline native V3 migration tests; no live site or paid requests."""
import base64
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import unittest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins.v3/dian115sign"


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


def load(proxy=None):
    modules = {name: types.ModuleType(name) for name in (
        "app", "app.plugins", "app.sdk", "app.sdk.config", "app.sdk.logging",
        "app.schemas", "app.schemas.types", "apscheduler", "apscheduler.triggers",
        "apscheduler.triggers.cron")}
    modules["app.plugins"]._PluginBase = Base
    modules["app.sdk.config"].settings = types.SimpleNamespace(PROXY=proxy)
    modules["app.sdk.logging"].logger = types.SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None, debug=lambda *a: None)
    modules["app.schemas.types"].EventType = types.SimpleNamespace(PluginAction="plugin")
    modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Plugin="plugin")
    modules["apscheduler.triggers.cron"].CronTrigger = types.SimpleNamespace(from_crontab=lambda x: x)
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("dian115sign_v3", PLUGIN / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mod._test_modules = modules
        return mod


def test_native_lifecycle_and_existing_form():
    mod = load()
    plugin = mod.Dian115Sign()
    plugin.init_plugin({"token": "fake", "maximum_wheel": False})
    with patch.dict(sys.modules, {**mod._test_modules, mod.__name__: mod}):
        form, defaults = plugin.get_form()
    assert form and defaults["maximum_wheel"] is False
    assert plugin.plugin_version == "2.0.0"
    assert plugin.get_state() is False
    assert plugin.get_service() == []
    assert plugin.get_command()[0]["event"] == "plugin"
    assert {api["path"] for api in plugin.get_api()} == {"/signin", "/account"}
    assert next(api for api in plugin.get_api() if api["path"] == "/signin")["methods"] == ["POST"]
    assert all(not (api["endpoint"].__code__.co_flags & 0x08) for api in plugin.get_api())
    assert plugin.get_page()
    plugin.stop_service()


def test_system_proxy_shapes():
    for proxy, expected in [({"https": "http://proxy:1", "http": "http://other:2"}, "http://proxy:1"),
                            ({"http": "http://other:2"}, "http://other:2"),
                            (types.SimpleNamespace(host="http://proxy:3"), "http://proxy:3")]:
        mod = load(proxy)
        assert mod.Dian115Sign._system_proxy() == expected


def test_paid_post_ambiguous_stops_and_persists_guard():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True):
        calls.append(method)
        if path.endswith("/games/status"):
            return {"items": {"daily_wheel": {"used_today": 19, "max_plays": 20, "cost": 5}}}
        if path.endswith("/me"):
            return {"user": {"points": 100}}
        raise RuntimeError("抽奖结果不明，请核对站点积分和次数，勿重试")
    plugin._wheel_request = request
    assert plugin._wheel_run()["results"] == []
    assert calls.count("POST") == 1
    assert plugin.get_data("dian115_wheel_last")["uncertain"]
    plugin._wheel_run()
    assert calls.count("POST") == 1


def test_sign_v1_signature_is_raw_ecdsa_and_verifies():
    plugin = load().Dian115Sign()
    plugin._token = "fake"
    plugin._priv_key = ec.generate_private_key(ec.SECP256R1())
    path = plugin.API_BASE + "/signin"
    headers = plugin._signed_headers("POST", path)
    raw = base64.urlsafe_b64decode(headers["X-Portal-Browser-Sig"] + "==")
    assert len(raw) == 64
    message = f"portal-browser-request/v1\nPOST\n{path}\n{headers['X-Portal-Browser-TS']}\n{headers['X-Portal-Browser-Nonce']}".encode()
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    plugin._priv_key.public_key().verify(der, message, ec.ECDSA(hashes.SHA256()))


def test_missing_marker_persistence_never_sends_paid_post():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True):
        calls.append(method)
        if path.endswith("/games/status"):
            return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 1, "cost": 5}}}
        if path.endswith("/me"):
            return {"user": {"points": 100}}
        raise AssertionError("paid POST must never execute")
    plugin._wheel_request = request
    original_save = plugin.save_data
    def save(key, value):
        if key == "dian115_wheel_last":
            raise RuntimeError("storage unavailable")
        return original_save(key, value)
    plugin.save_data = save
    outcome = plugin._wheel_run()
    assert "POST" not in calls
    assert "storage unavailable" in outcome["error"]


def test_successful_post_record_storage_failure_keeps_guard():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True):
        calls.append(method)
        if path.endswith("/games/status"):
            return {"items": {"daily_wheel": {"used_today": 0, "max_plays": 2, "cost": 5}}}
        if path.endswith("/me"):
            return {"user": {"points": 100}}
        return {"prize": {"label": "3 积分", "points": 3}}
    plugin._wheel_request = request
    original_save = plugin.save_data
    def save(key, value):
        if key == "dian115_wheel_records":
            raise RuntimeError("records unavailable")
        return original_save(key, value)
    plugin.save_data = save
    result = plugin._wheel_run()
    assert calls.count("POST") == 1
    assert result["error"] == "records unavailable"
    assert plugin.get_data("dian115_wheel_last")["uncertain"] is True
    plugin._wheel_run()
    assert calls.count("POST") == 1


def test_sign_success_and_already_signed_offline():
    plugin = load().Dian115Sign()
    plugin._lucky_mode = False
    plugin._fetch_me = lambda: {"last_signin_date": "", "points": 20, "consecutive_signin": 2}
    plugin._signed_headers = lambda *args: {}
    class Response:
        status_code = 200
        text = ""
        def json(self):
            return {"code": "ok", "award": 5, "new_balance": 25}
    class HTTP:
        def post(self, *args, **kwargs):
            return Response()
    plugin._http = lambda: HTTP()
    assert plugin._do_sign()["award"] == 5
    assert len(plugin.get_data("dian115_records")) == 1
    plugin._fetch_me = lambda: {"last_signin_date": plugin._records[0]["date"], "points": 25, "consecutive_signin": 3}
    assert plugin._do_sign()["already"] is True


def test_system_proxy_applied_to_http_session_for_both_shapes():
    for proxy in ({"https": "http://dict-proxy:1"}, types.SimpleNamespace(host="http://object-proxy:2")):
        mod = load(proxy)
        plugin = mod.Dian115Sign()
        plugin._use_system_proxy = True
        class Session:
            def __init__(self, impersonate):
                self.proxies = {}
        with patch.object(mod, "curl_requests", types.SimpleNamespace(Session=Session)), patch.object(mod, "HAS_CURL_CFFI", True):
            session = plugin._http()
        expected = mod.Dian115Sign._system_proxy()
        assert session.proxies == {"http": expected, "https": expected}
