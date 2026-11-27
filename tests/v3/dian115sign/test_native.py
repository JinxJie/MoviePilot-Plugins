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


def page_text(nodes):
    if isinstance(nodes, list):
        return '\n'.join(page_text(node) for node in nodes)
    if not isinstance(nodes, dict):
        return ''
    return '\n'.join([str(nodes.get('text', '')), page_text(nodes.get('content', []))]).strip()


def test_homepage_shows_archived_unknown_without_pausing_new_day():
    from datetime import datetime, timedelta
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._fetch_me = lambda: {'points': 1501}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20}}}
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    plugin.save_data('dian115_wheel_pending_history', [{'date': yesterday, 'uncertain': True,
                                                         'status': '结果待核对'}])
    plugin.save_data('dian115_wheel_last', {'date': datetime.now().strftime('%Y-%m-%d'),
                                           'status': '完成', 'uncertain': False})
    text = page_text(plugin.get_page())
    assert '站点今日已用：0/20' in text
    assert f'历史待核对（{yesterday}）' in text
    assert '自动抽奖已暂停' not in text


def test_homepage_today_wheel_uses_site_quota_and_local_confirmed_count():
    mod = load()
    plugin = mod.Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._fetch_me = lambda: {'points': 100}
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append((method, path))
        return {'items': {'daily_wheel': {'used_today': 17, 'max_plays': 20}}}
    plugin._wheel_request = request
    text = page_text(plugin.get_page())
    assert '站点今日已用：17/20' in text
    assert '本地已确认：0 次' in text
    assert calls == [('GET', plugin.API_BASE + '/games/status')]


def test_homepage_today_wheel_read_failure_is_explicit_and_pending_visible():
    import copy
    mod = load()
    plugin = mod.Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._fetch_me = lambda: {'points': 100}
    plugin.save_data('dian115_wheel_last', {'date': __import__('datetime').date.today().isoformat(),
                                           'status': '结果待核对', 'uncertain': True})
    before = copy.deepcopy(plugin.data)
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append((method, path))
        raise RuntimeError('GET failed')
    plugin._wheel_request = request
    text = page_text(plugin.get_page())
    assert '站点今日已用：获取失败' in text
    assert '本地已确认：0 次' in text
    assert '结果待核对' in text
    assert calls == [('GET', plugin.API_BASE + '/games/status')]
    assert plugin.data == before


def test_homepage_invalid_pending_date_is_read_only_and_does_not_crash():
    import copy
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._fetch_me = lambda: {'points': 100}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20}}}
    plugin.save_data('dian115_wheel_last', {'date': 123, 'uncertain': True})
    before = copy.deepcopy(plugin.data)
    assert '结果待核对' in page_text(plugin.get_page())
    assert plugin.data == before


def test_homepage_cross_day_pending_survives_when_wheel_disabled():
    import copy
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = False
    plugin._fetch_me = lambda: {'points': 100}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20}}}
    marker = {'date': '2020-01-01', 'status': '结果待核对', 'uncertain': True}
    plugin.save_data('dian115_wheel_last', marker)
    before = copy.deepcopy(plugin.data)
    text = page_text(plugin.get_page())
    assert '状态：结果待核对（2020-01-01）' in text
    assert '历史结果待核对，不影响今日额度' in text
    assert '自动抽奖已暂停' not in text
    assert '状态：未开启' not in text
    assert plugin.data == before


def test_homepage_today_rewards_group_in_first_seen_order():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._fetch_me = lambda: {'points': 12345678901234567890}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': '17', 'max_plays': '20'}}}
    today = __import__('datetime').date.today().isoformat()
    plugin.save_data('dian115_wheel_records', [
        {'date': today + 'T09:30:01', 'label': '+5 积分', 'points': 5, 'cost': 5},
        {'date': '2020-01-01T09:30:01', 'label': '+99 积分', 'points': 99, 'cost': 5},
        {'date': today + 'T09:30:02', 'label': '-3 积分', 'points': -3, 'cost': 5},
        {'date': today + 'T09:30:03', 'label': '+5 积分', 'points': 5, 'cost': 5},
    ])
    text = page_text(plugin.get_page())
    assert '站点今日已用：17/20' in text and '本地已确认：3 次' in text
    assert '今日消耗：15 积分' in text and '积分奖励：+7' in text
    assert '• +5 积分 ×2\n• -3 积分 ×1' in text
    assert '+99 积分' not in text
    assert '12,345,678,901,234,567,890' in text


def test_homepage_no_token_makes_no_site_calls():
    plugin = load().Dian115Sign()
    def fail(*a, **k):
        raise AssertionError('no network without credentials')
    plugin._fetch_me = fail
    plugin._wheel_request = fail
    text = page_text(plugin.get_page())
    assert '站点今日已用：未配置 Token' in text
    assert '本地已确认：0 次' in text


def test_homepage_missing_or_invalid_quota_never_falls_back_to_local_count():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._fetch_me = lambda: {'points': 100}
    today = __import__('datetime').date.today().isoformat()
    plugin.save_data('dian115_wheel_records', [
        {'date': today + 'T09:30:01', 'label': '+5 积分', 'points': 5, 'cost': 5}])
    for quota in ({}, {'used_today': True, 'max_plays': 20},
                  {'used_today': 1.5, 'max_plays': 20}, {'used_today': 1, 'max_plays': 'invalid'},
                  {'used_today': -1, 'max_plays': 20}):
        plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': quota}}
        text = page_text(plugin.get_page())
        assert '站点今日已用：获取失败' in text
        assert '本地已确认：1 次' in text


def test_homepage_invalid_local_totals_are_unknown_and_do_not_crash():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._fetch_me = lambda: {'points': 100}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 17, 'max_plays': 20}}}
    today = __import__('datetime').date.today().isoformat()
    plugin.save_data('dian115_wheel_records', [None, 'legacy',
        {'date': today + 'T09:30:01', 'label': '+5 积分', 'cost': 'bad', 'points': 5},
        {'date': today + 'T09:30:02', 'label': 'VIP 1 天', 'cost': 5},
    ])
    text = page_text(plugin.get_page())
    assert '站点今日已用：17/20' in text
    assert '本地已确认：2 次' in text
    assert '今日消耗：获取失败' in text and '积分奖励：获取失败' in text


def test_homepage_local_storage_failure_does_not_display_zero_confirmed():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._fetch_me = lambda: {'points': 100}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 17, 'max_plays': 20}}}
    original = plugin.get_data
    def get(key):
        if key in ('dian115_wheel_records', 'dian115_wheel_last'):
            raise RuntimeError('storage unavailable')
        return original(key)
    plugin.get_data = get
    text = page_text(plugin.get_page())
    assert '站点今日已用：17/20' in text
    assert '本地已确认：获取失败' in text
    assert '状态：获取失败' in text
    assert '本地已确认：0 次' not in text


def test_homepage_midnight_query_labels_local_snapshot_date():
    from datetime import datetime, timedelta
    mod = load()
    plugin = mod.Dian115Sign()
    plugin._token = 'fake'
    plugin._fetch_me = lambda: {'points': 100}
    day = datetime.now()
    switched = [False]
    plugin.save_data('dian115_wheel_records', [
        {'date': day.isoformat(timespec='seconds'), 'label': '+3 积分', 'cost': 5, 'points': 3}])
    def request(*a, **k):
        switched[0] = True
        return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20}}}
    plugin._wheel_request = request
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day + (timedelta(days=1) if switched[0] else timedelta())
    with patch.object(mod, 'datetime', Clock):
        text = page_text(plugin.get_page())
    assert f'🎡 转盘记录（{day:%Y-%m-%d}）' in text
    assert '查询期间日期已切换，请刷新查看今日数据' in text
    assert '站点今日已用：获取失败' in text
    assert '本地已确认：1 次' in text


def test_midnight_during_wheel_bootstrap_never_sends_paid_request():
    from datetime import datetime, timedelta
    mod = load()
    plugin = mod.Dian115Sign()
    day = datetime.now()
    switched = [False]
    sent = []
    def bootstrap(force=False):
        switched[0] = True
    plugin._wheel_bootstrap = bootstrap
    plugin._wheel_headers = lambda *a, **k: {}
    plugin._http = lambda: types.SimpleNamespace(request=lambda *a, **k: sent.append((a, k)))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day + (timedelta(days=1) if switched[0] else timedelta())
    with patch.object(mod, 'datetime', Clock):
        try:
            plugin._wheel_request('POST', plugin.API_BASE + '/lottery/wheel', retry=False)
        except RuntimeError as exc:
            assert '日期已切换' in str(exc)
        else:
            raise AssertionError('bootstrap rollover must stop paid POST')
    assert sent == []


def test_final_notification_midnight_query_discards_other_day_quota():
    from datetime import datetime, timedelta
    mod = load()
    plugin = mod.Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._notify = True
    plugin._retry = 0
    day = datetime.now()
    switched = [False]
    plugin._do_sign = lambda manual=False: {'ok': True, 'date': day.strftime('%Y-%m-%d'), 'award': 4}
    plugin._wheel_run = lambda: {'status': '完成', 'date': day.strftime('%Y-%m-%d'),
                                 'results': [{'label': '+3 积分', 'points': 3, 'cost': 5}],
                                 'planned': 1, 'used_today': 1, 'max_plays': 20, 'error': ''}
    plugin._fetch_me = lambda: {'points': 98}
    def request(*a, **k):
        switched[0] = True
        return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20}}}
    plugin._wheel_request = request
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day + (timedelta(days=1) if switched[0] else timedelta())
    with patch.object(plugin, '_wheel_pace'), patch.object(mod, 'datetime', Clock):
        plugin._sign_job(manual=True)
    text = plugin.notices[0]['text']
    assert '今日已用：获取失败' in text
    assert '💰 当前余额：获取失败' in text


def test_paid_request_preserves_preflight_date_and_pending_guard():
    from datetime import datetime, timedelta
    mod = load()
    plugin = mod.Dian115Sign()
    original_request = plugin._wheel_request
    day = datetime.now()
    sent = []
    plugin._wheel_bootstrap = lambda *a, **k: None
    plugin._wheel_headers = lambda *a, **k: {}
    plugin._http = lambda: types.SimpleNamespace(request=lambda *a, **k: sent.append((a, k)))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day + timedelta(days=1)
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 1, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        assert kwargs['expected_date'] == day.strftime('%Y-%m-%d')
        with patch.object(mod, 'datetime', Clock):
            return original_request(method, path, retry=retry, **kwargs)
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert '日期已切换' in result['error']
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is True
    assert sent == []


def test_native_lifecycle_and_existing_form():
    mod = load()
    plugin = mod.Dian115Sign()
    plugin.init_plugin({"token": "fake", "maximum_wheel": False})
    with patch.dict(sys.modules, {**mod._test_modules, mod.__name__: mod}):
        form, defaults = plugin.get_form()
    assert form and defaults["maximum_wheel"] is False
    assert plugin.plugin_version == "3.0.2"
    assert plugin.get_state() is False
    assert plugin.get_service() == []
    assert plugin.get_command()[0]["event"] == "plugin"
    assert {api["path"] for api in plugin.get_api()} == {"/signin", "/account"}
    assert next(api for api in plugin.get_api() if api["path"] == "/signin")["methods"] == ["POST"]
    assert all(not (api["endpoint"].__code__.co_flags & 0x08) for api in plugin.get_api())
    plugin._fetch_me = lambda: {'points': 100}
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20}}}
    assert plugin.get_page()
    plugin.stop_service()


def test_system_proxy_shapes():
    for proxy, expected in [({"https": "http://proxy:1", "http": "http://other:2"}, "http://proxy:1"),
                            ({"http": "http://other:2"}, "http://other:2"),
                            (types.SimpleNamespace(host="http://proxy:3"), "http://proxy:3")]:
        mod = load(proxy)
        assert mod.Dian115Sign._system_proxy() == expected


def test_last_unknown_slot_is_reserved_and_never_replayed():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith("/games/status"):
            return {"items": {"daily_wheel": {"used_today": 19, "max_plays": 20, "cost": 5}}}
        if path.endswith("/me"):
            return {"user": {"points": 100}}
        raise RuntimeError("抽奖结果不明，请核对站点积分和次数，勿重试")
    plugin._wheel_request = request
    assert plugin._wheel_run()["results"] == []
    assert calls.count("POST") == 1
    assert plugin.get_data("dian115_wheel_pending_history")[0]["uncertain"]
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
    def request(method, path, retry=True, **kwargs):
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
    def request(method, path, retry=True, **kwargs):
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


def test_rate_limit_stops_and_keeps_guard():
    plugin = load().Dian115Sign()
    calls = []
    used = [0]
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': used[0], 'max_plays': 20, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        if used[0] == 0:
            used[0] = 1
            return {'prize': {'label': '+3 积分', 'points': 3}}
        raise RuntimeError('站点明确拒绝本次抽奖；站点接口失败：HTTP 429，代码 rate_limited')
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace') as pace:
        result = plugin._wheel_run()
    assert pace.call_count >= 2
    assert calls.count('POST') == 2
    assert len(result['results']) == 1
    assert result['used_today'] is None and 'rate_limited' in result['error']
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is True
    with patch.object(plugin, '_wheel_pace'):
        plugin._wheel_run()
    # A subsequent explicit run may resume after read-only rejection reconciliation;
    # the current run itself never retries a 429.
    assert calls.count('POST') == 3


def test_previous_day_pending_does_not_block_new_daily_quota():
    from datetime import datetime, timedelta
    import copy
    plugin = load().Dian115Sign()
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    marker = {'date': yesterday, 'status': '结果待核对', 'uncertain': True,
              'used_before': 17, 'points_before': 1497}
    plugin.save_data('dian115_wheel_last', copy.deepcopy(marker))
    plugin.save_data('dian115_wheel_progress', {'date': yesterday, 'baseline': 0, 'count': 17})
    used, points, calls = [0], [1501], []
    def request(method, path, retry=True, **kwargs):
        calls.append((method, path))
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': used[0], 'max_plays': 20, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': points[0]}}
        assert method == 'POST' and kwargs['expected_date'] == today
        used[0] += 1
        points[0] -= 2
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
        assert result['planned'] == 20 and len(result['results']) == 20
        assert result['error'] == ''
        assert plugin._wheel_run()['results'] == []
    assert sum(method == 'POST' for method, _ in calls) == 20
    assert plugin.get_data('dian115_wheel_pending_history') == [marker]
    assert plugin.get_data('dian115_wheel_progress') == {'date': today, 'baseline': 0, 'count': 20,
                                                        'points_limit': points[0]}
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is False


def test_success_response_crossing_midnight_keeps_original_draw_date():
    from datetime import datetime as RealDate
    mod = load()
    moved = [False]
    class Clock(RealDate):
        @classmethod
        def now(cls, tz=None):
            return RealDate(2026, 11, 28, 0, 0, 1) if moved[0] else RealDate(2026, 11, 27, 23, 59, 59)
    plugin = mod.Dian115Sign()
    posts = []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 3, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        posts.append(method)
        moved[0] = True
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(mod, 'datetime', Clock), patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert posts == ['POST'] and '日期已切换' in result['error']
    assert plugin._wheel_records()[0]['date'].startswith('2026-11-27T')


def test_confirmed_draw_budget_survives_stale_balance_and_restart():
    plugin = load().Dian115Sign()
    state = {'used': 0, 'balance': 10}
    posts = []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': state['used'], 'max_plays': 3, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 10}}
        posts.append(method)
        state['used'] += 1
        state['balance'] -= 5
        return {'prize': {'label': '未中奖', 'points': 0}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        first = plugin._wheel_run()
        second = plugin._wheel_run()
    assert posts == ['POST']
    assert state['balance'] == 5
    assert '积分不足' in first['error'] and '积分不足' in second['error']
    assert plugin.get_data('dian115_wheel_progress')['points_limit'] == 5


def test_real_request_wrapper_unknown_responses_continue_without_retry():
    for failure in ('timeout', 'non_json', 'http_500', 'missing_prize', 'bad_prize'):
        plugin = load().Dian115Sign()
        plugin._wheel_bootstrap = lambda *a, **k: None
        plugin._wheel_headers = lambda *a, **k: {}
        paid = []
        class Response:
            def __init__(self, data, status=200):
                self.data = data
                self.status_code = status
            def json(self):
                if isinstance(self.data, Exception):
                    raise self.data
                return self.data
        def request(method, url, **kwargs):
            if url.endswith('/games/status'):
                return Response({'items': {'daily_wheel': {'used_today': len(paid), 'max_plays': 2, 'cost': 5}}})
            if url.endswith('/me'):
                return Response({'user': {'points': 100}})
            paid.append(method)
            if len(paid) == 1:
                if failure == 'timeout':
                    raise TimeoutError('synthetic timeout')
                if failure == 'non_json':
                    return Response(ValueError('synthetic invalid JSON'))
                if failure == 'http_500':
                    return Response({'code': 'server_error'}, 500)
                if failure == 'bad_prize':
                    return Response({'prize': {'label': '+3', 'points': 'invalid'}})
                return Response({'code': 'ok'})
            return Response({'prize': {'label': '+3 积分', 'points': 3}})
        plugin._http = lambda: types.SimpleNamespace(request=request)
        with patch.object(plugin, '_wheel_pace'):
            result = plugin._wheel_run()
        assert paid == ['POST', 'POST'], failure
        assert len(result['results']) == 1 and result['pending_count'] == 1, failure
        assert result['error'] == '', failure


def test_all_unknown_sequences_are_capped_across_restart():
    from itertools import product
    for sequence in product((True, False), repeat=4):
        plugin = load().Dian115Sign()
        posts = []
        def request(method, path, retry=True, **kwargs):
            if path.endswith('/games/status'):
                return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 4, 'cost': 5}}}
            if path.endswith('/me'):
                return {'user': {'points': 100}}
            index = len(posts)
            posts.append(method)
            assert index < 4
            if sequence[index]:
                raise RuntimeError('POST 请求结果不明：TimeoutError')
            return {'prize': {'label': '+3 积分', 'points': 3}}
        plugin._wheel_request = request
        with patch.object(plugin, '_wheel_pace'):
            first = plugin._wheel_run()
            second = plugin._wheel_run()
        assert posts == ['POST'] * 4, sequence
        assert len(first['results']) == sequence.count(False), sequence
        assert first['status'] == '完成', sequence
        assert first['pending_count'] == sequence.count(True), sequence
        assert second['results'] == [], sequence


def test_history_read_failure_does_not_escape_run_result():
    plugin = load().Dian115Sign()
    original = plugin.get_data
    def get(key):
        if key == 'dian115_wheel_pending_history':
            raise RuntimeError('history unavailable')
        return original(key)
    plugin.get_data = get
    plugin._wheel_request = lambda *a, **k: {'items': {'daily_wheel': {'used_today': 20, 'max_plays': 20}}}
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert result['results'] == [] and result['pending_count'] is None


def test_rate_recovery_after_unknown_uses_actual_account_snapshot():
    plugin = load().Dian115Sign()
    used, posts = [0], []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': used[0], 'max_plays': 3, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        posts.append(method)
        if len(posts) == 1:
            used[0] += 1
            raise RuntimeError('POST 请求结果不明：TimeoutError')
        if len(posts) == 2:
            raise RuntimeError('站点明确拒绝本次抽奖；站点接口失败：HTTP 429，代码 rate_limited')
        used[0] += 1
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        first = plugin._wheel_run()
        second = plugin._wheel_run()
    assert 'rate_limited' in first['error']
    assert second['error'] == '' and len(second['results']) == 2
    assert len(posts) == 4


def test_unknown_round_failed_final_reads_never_reuse_old_balance():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._notify = True
    plugin._retry = 0
    day = __import__('datetime').date.today().isoformat()
    plugin._do_sign = lambda manual=False: {'ok': True, 'date': day, 'award': 4, 'balance': 999}
    final, posts = [False], []
    def request(method, path, retry=True, **kwargs):
        if final[0]:
            raise RuntimeError('synthetic final GET failure')
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': len(posts), 'max_plays': 2, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        posts.append(method)
        if len(posts) == 1:
            raise RuntimeError('POST 请求结果不明：TimeoutError')
        return {'prize': {'label': '+3 积分', 'points': 3}}
    def fetch():
        final[0] = True
        return None
    plugin._wheel_request = request
    plugin._fetch_me = fetch
    with patch.object(plugin, '_wheel_pace'):
        plugin._sign_job(manual=True)
    text = plugin.notices[0]['text']
    assert posts == ['POST', 'POST']
    assert '今日已用：获取失败' in text and '💰 当前余额：获取失败' in text
    assert '当前余额：999' not in text
    assert plugin.get_data('dian115_wheel_last_check') is None


def test_unknown_round_final_notification_uses_fresh_site_reads():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._notify = True
    plugin._retry = 0
    today = __import__('datetime').date.today().isoformat()
    plugin._do_sign = lambda manual=False: {'ok': True, 'date': today, 'award': 4, 'balance': 999}
    calls, used = [], [0]
    def request(method, path, retry=True, **kwargs):
        calls.append((method, path))
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': used[0], 'max_plays': 3, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        used[0] += 1
        if used[0] == 1:
            raise RuntimeError('POST 请求结果不明：TimeoutError')
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    plugin._fetch_me = lambda: {'points': 91}
    with patch.object(plugin, '_wheel_pace'):
        plugin._sign_job(manual=True)
    text = plugin.notices[0]['text']
    assert sum(method == 'POST' for method, _ in calls) == 3
    assert '转盘计划 3 次，实际 3 次' in text
    assert '已确认 2 次，结果待核对 1 次' in text
    assert '今日已用：3/3' in text and '💰 当前余额：91' in text
    assert plugin.get_data('dian115_wheel_last_check') == {'date': today, 'used_today': 3, 'max_plays': 3, 'points': 91}
    assert plugin.get_data('dian115_wheel_pending_history')[0]['uncertain'] is True


def test_unknown_result_budget_reserve_survives_stale_account_and_restart():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 10}}
        calls.append(method)
        raise RuntimeError('POST 请求结果不明：TimeoutError')
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        first = plugin._wheel_run()
        second = plugin._wheel_run()
    assert calls == ['POST']
    assert '积分不足' in first['error'] and '积分不足' in second['error']


def test_unknown_archive_failure_stops_remaining_paid_requests():
    plugin = load().Dian115Sign()
    original = plugin.save_data
    def save(key, value):
        if key == 'dian115_wheel_pending_history':
            return
        original(key, value)
    plugin.save_data = save
    calls = []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 20, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        calls.append(method)
        raise RuntimeError('POST 请求结果不明：TimeoutError')
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert calls == ['POST'] and '保存失败' in result['error']
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is True


def test_unknown_paid_result_continues_remaining_slots_without_replay():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 3, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        calls.append(method)
        if len(calls) == 1:
            raise RuntimeError('POST 请求结果不明：TimeoutError')
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
        again = plugin._wheel_run()
    assert calls == ['POST'] * 3
    assert len(result['results']) == 2 and not result['error']
    assert result['attempted'] == 3 and result['pending_count'] == 1
    assert again['results'] == []
    assert len(plugin.get_data('dian115_wheel_pending_history')) == 1


def test_same_day_unknown_consumes_reserved_slot_not_entire_round():
    plugin = load().Dian115Sign()
    today = __import__('datetime').date.today().isoformat()
    marker = {'date': today, 'uncertain': True, 'used_before': 1, 'points_before': 100, 'cost': 5}
    plugin.save_data('dian115_wheel_last', marker)
    calls = []
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 1, 'max_plays': 3, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        calls.append(method)
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert calls == ['POST'] and len(result['results']) == 1
    assert plugin.get_data('dian115_wheel_pending_history') == [marker]


def test_old_unknown_pending_without_snapshot_rolls_into_history():
    from datetime import datetime, timedelta
    plugin = load().Dian115Sign()
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    marker = {'date': yesterday, 'uncertain': True, 'status': '结果待核对'}
    plugin.save_data('dian115_wheel_last', marker)
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 20, 'max_plays': 20, 'cost': 5}}}
        raise AssertionError('quota exhausted: no account GET or paid POST')
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert result['error'] == '今日次数已用完'
    assert calls == ['GET']
    assert plugin.get_data('dian115_wheel_pending_history') == [marker]


def test_old_pending_archive_failure_never_sends_paid_post():
    from datetime import datetime, timedelta
    plugin = load().Dian115Sign()
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    marker = {'date': yesterday, 'uncertain': True, 'rejected': 'rate_limited'}
    plugin.save_data('dian115_wheel_last', marker)
    original = plugin.save_data
    def save(key, value):
        if key == 'dian115_wheel_pending_history':
            return
        original(key, value)
    plugin.save_data = save
    calls = []
    plugin._wheel_request = lambda *a, **k: calls.append((a, k))
    result = plugin._wheel_run()
    assert '保存失败' in result['error']
    assert calls == []
    assert plugin.get_data('dian115_wheel_last') == marker


def test_old_pending_is_replaced_only_after_archival_readback():
    from datetime import datetime, timedelta
    plugin = load().Dian115Sign()
    marker = {'date': (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'),
              'uncertain': True, 'status': '结果待核对'}
    plugin.save_data('dian115_wheel_last', marker)
    original = plugin.save_data
    def save(key, value):
        if key == 'dian115_wheel_last':
            return
        original(key, value)
    plugin.save_data = save
    calls = []
    plugin._wheel_request = lambda *a, **k: calls.append((a, k))
    result = plugin._wheel_run()
    assert '切换失败' in result['error']
    assert calls == []
    assert plugin.get_data('dian115_wheel_last') == marker
    assert plugin.get_data('dian115_wheel_pending_history') == [marker]


def test_unknown_paid_result_from_old_day_is_archived_without_replaying_it():
    from datetime import datetime, timedelta
    plugin = load().Dian115Sign()
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    marker = {'date': yesterday, 'uncertain': True, 'used_before': 1, 'points_before': 100}
    plugin.save_data('dian115_wheel_last', marker)
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 20, 'max_plays': 20, 'cost': 5}}}
        raise AssertionError('no paid request when daily quota is exhausted')
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert result['results'] == [] and '今日次数已用完' in result['error']
    assert calls == ['GET']
    assert plugin.get_data('dian115_wheel_pending_history') == [marker]
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is False


def test_http_rate_limit_parser_distinguishes_known_rejection_from_unknown():
    plugin = load().Dian115Sign()
    plugin._wheel_bootstrap = lambda *a, **k: None
    plugin._wheel_headers = lambda *a, **k: {}
    class Response:
        status_code = 429
        def json(self):
            return {'code': 'rate_limited'}
    plugin._http = lambda: types.SimpleNamespace(request=lambda *a, **k: Response())
    try:
        plugin._wheel_request('POST', '/api/portal/lottery/wheel')
        assert False, 'rate limit must raise'
    except RuntimeError as exc:
        assert str(exc).startswith('站点明确拒绝本次抽奖；')
    Response.json = lambda self: {'code': 'server_error'}
    try:
        plugin._wheel_request('POST', '/api/portal/lottery/wheel')
        assert False, 'unknown failure must raise'
    except RuntimeError as exc:
        assert str(exc).startswith('抽奖 POST 结果不明')


def test_reconciliation_failure_preserves_marker_without_paid_post():
    plugin = load().Dian115Sign()
    today = __import__('datetime').date.today().isoformat()
    marker = {'date': today, 'uncertain': True, 'rejected': 'rate_limited',
              'used_before': 17, 'points_before': 100}
    plugin.save_data('dian115_wheel_last', marker)
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 18}}}
        return {'user': {'points': 100}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert '不一致' in result['error']
    assert calls == ['GET', 'GET']
    assert plugin.get_data('dian115_wheel_last') == marker


def test_recovered_old_pending_notification_keeps_history_warning():
    from datetime import datetime, timedelta
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._notify = True
    plugin._retry = 0
    day = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    plugin._do_sign = lambda manual=False: {'ok': True, 'date': day, 'award': 4}
    plugin.save_data('dian115_wheel_last', {'date': yesterday, 'uncertain': True,
                                           'status': '结果待核对'})
    used, calls = [0], []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': used[0], 'max_plays': 1, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 1501}}
        used[0] += 1
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    plugin._fetch_me = lambda: {'points': 1499}
    with patch.object(plugin, '_wheel_pace'):
        plugin._sign_job(manual=True)
    text = plugin.notices[0]['text']
    assert calls.count('POST') == 1
    assert '转盘计划 1 次，实际 1 次' in text
    assert '今日已用：1/1' in text
    assert '💰 当前余额：1499' in text
    assert f'先前转盘结果仍待核对（{yesterday}），不影响今日抽奖' in text


def test_rate_limit_notification_format_and_no_balance_get():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._notify = True
    plugin._retry = 0
    plugin._fetch_me = lambda: (_ for _ in ()).throw(AssertionError('GET after limit'))
    plugin._do_sign = lambda manual=False: {'ok': True, 'date': '2026-11-26', 'award': 4}
    plugin._wheel_run = lambda: {'results': [{'label': '+5 积分', 'points': 5, 'cost': 5},
                                              {'label': '+3 积分', 'points': 3, 'cost': 5},
                                              {'label': '+5 积分', 'points': 5, 'cost': 5}],
                                 'planned': 20, 'used_today': None, 'max_plays': None,
                                 'error': '站点限流（rate_limited），本轮停止；不自动重试', 'status': '提前结束'}
    plugin._sign_job(manual=True)
    text = plugin.notices[0]['text']
    assert plugin.notices[0]['title'] == '癫影签到成功（2026-11-26）'
    assert '转盘计划 20 次，实际 3 次' in text
    assert '• +5 积分 ×2\n• +3 积分 ×1' in text
    assert '本次消耗：15 积分\n积分奖励：+13' in text
    assert '今日已用：获取失败\n💰 当前余额：获取失败' in text
    assert text.endswith('⚠️ 提前结束：站点限流（rate_limited），本轮停止；不自动重试')


def test_rejected_same_day_pending_can_continue_after_read_only_reconciliation():
    plugin = load().Dian115Sign()
    today = __import__('datetime').date.today().isoformat()
    marker = {'date': today, 'uncertain': True, 'rejected': 'rate_limited',
              'used_before': 0, 'points_before': 100}
    plugin.save_data('dian115_wheel_last', marker)
    used, points, calls = [0], [100], []
    def request(method, path, retry=True, **kwargs):
        calls.append((method, path))
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': used[0], 'max_plays': 1, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': points[0]}}
        assert method == 'POST'
        used[0] += 1
        points[0] -= 2
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert len(result['results']) == 1 and result['error'] == ''
    assert [method for method, _ in calls].count('POST') == 1
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is False


def test_rejection_recovery_still_requires_fresh_valid_cost():
    plugin = load().Dian115Sign()
    today = __import__('datetime').date.today().isoformat()
    plugin.save_data('dian115_wheel_last', {'date': today, 'uncertain': True, 'rejected': 'rate_limited',
                                           'used_before': 17, 'points_before': 100})
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append((method, path))
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 17, 'max_plays': 20}}}
        return {'user': {'points': 100}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert result['results'] == [] and '成本' in result['error']
    assert len(calls) == 3 and all(method == 'GET' for method, _ in calls)
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is False


def test_stale_site_counter_cannot_exceed_local_daily_progress_across_runs():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 1, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        assert len(plugin._wheel_run()['results']) == 1
        assert plugin._wheel_run()['results'] == []
    assert calls.count('POST') == 1


def test_stale_site_counter_two_successes_still_capped_across_runs():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 2, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        assert len(plugin._wheel_run()['results']) == 2
        assert plugin._wheel_run()['results'] == []
    assert calls.count('POST') == 2


def test_stale_site_counter_is_not_reported_as_confirmed_usage():
    plugin = load().Dian115Sign()
    def request(method, path, retry=True, **kwargs):
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 2, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        return {'prize': {'label': '+3 积分', 'points': 3}}
    plugin._wheel_request = request
    with patch.object(plugin, '_wheel_pace'):
        result = plugin._wheel_run()
    assert len(result['results']) == 2
    assert result['used_today'] == 0
    assert result['confirmed_used'] == 2
    assert '今日已用：0/2' in '\n'.join(plugin._wheel_summary(result))


def test_midnight_after_marker_never_sends_paid_post():
    from datetime import datetime, timedelta
    mod = load()
    plugin = mod.Dian115Sign()
    day = datetime.now()
    switched = [False]
    calls = []
    real_save = plugin.save_data
    def save(key, value):
        real_save(key, value)
        if key == 'dian115_wheel_last' and value.get('uncertain') is True:
            switched[0] = True
    plugin.save_data = save
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 1, 'cost': 5}}}
        if path.endswith('/me'):
            return {'user': {'points': 100}}
        raise AssertionError('paid POST after midnight')
    plugin._wheel_request = request
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day + (timedelta(days=1) if switched[0] else timedelta())
    with patch.object(mod, 'datetime', Clock):
        result = plugin._wheel_run()
    assert '日期已切换' in result['error'] and 'POST' not in calls
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is True
    assert result['used_today'] is None


def test_midnight_during_preflight_stops_before_marker():
    from datetime import datetime, timedelta
    mod = load()
    plugin = mod.Dian115Sign()
    day = datetime.now()
    switched = [False]
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 1, 'cost': 5}}}
        if path.endswith('/me'):
            switched[0] = True
            return {'user': {'points': 100}}
        raise AssertionError('paid POST after midnight')
    plugin._wheel_request = request
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return day + (timedelta(days=1) if switched[0] else timedelta())
    with patch.object(mod, 'datetime', Clock):
        result = plugin._wheel_run()
    assert '日期已切换' in result['error'] and 'POST' not in calls
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is False
    assert result['used_today'] is None


def test_unsent_cooldown_failure_does_not_consume_slot_after_recovery():
    for failure in ('raise', 'drop'):
        plugin = load().Dian115Sign()
        posts, failed = [], [False]
        def request(method, path, retry=True, **kwargs):
            if path.endswith('/games/status'):
                return {'items': {'daily_wheel': {'used_today': len(posts), 'max_plays': 1, 'cost': 5}}}
            if path.endswith('/me'):
                return {'user': {'points': 100}}
            posts.append(method)
            return {'prize': {'label': '未中奖', 'points': 0}}
        plugin._wheel_request = request
        original_save = plugin.save_data
        def save(key, value):
            if key == 'dian115_wheel_paid_at' and not failed[0]:
                failed[0] = True
                if failure == 'raise':
                    raise RuntimeError('synthetic cooldown failure')
                return
            original_save(key, value)
        plugin.save_data = save
        with patch.object(plugin, '_wheel_pace'):
            first = plugin._wheel_run()
            assert posts == [] and first['attempted'] == 0, failure
            second = plugin._wheel_run()
        assert posts == ['POST'], (failure, first, second)
        assert len(second['results']) == 1 and second['error'] == '', failure
        assert not plugin.get_data('dian115_wheel_pending_history'), failure
        assert plugin.get_data('dian115_wheel_progress')['count'] == 1, failure


def test_cooldown_storage_failure_prevents_paid_post():
    plugin = load().Dian115Sign()
    calls = []
    def request(method, path, retry=True, **kwargs):
        calls.append(method)
        if path.endswith('/games/status'):
            return {'items': {'daily_wheel': {'used_today': 0, 'max_plays': 1, 'cost': 5}}}
        return {'user': {'points': 100}}
    plugin._wheel_request = request
    original = plugin.save_data
    def save(key, value):
        if key == 'dian115_wheel_paid_at':
            raise RuntimeError('cooldown storage failure')
        original(key, value)
    plugin.save_data = save
    result = plugin._wheel_run()
    assert 'cooldown storage failure' in result['error']
    assert 'POST' not in calls
    assert plugin.get_data('dian115_wheel_last')['uncertain'] is False


def test_success_notification_final_quota_get_failure_is_not_stale():
    plugin = load().Dian115Sign()
    plugin._token = 'fake'
    plugin._maximum_wheel = True
    plugin._notify = True
    plugin._retry = 0
    plugin._do_sign = lambda manual=False: {'ok': True, 'date': '2026-11-26', 'award': 4}
    plugin._wheel_run = lambda: {'status': '完成', 'results': [{'label': '+3 积分', 'points': 3, 'cost': 5}],
                                 'planned': 1, 'used_today': 1, 'max_plays': 20, 'confirmed_used': 1, 'error': ''}
    plugin._fetch_me = lambda: {'points': 98}
    plugin._wheel_request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('GET failed'))
    with patch.object(plugin, '_wheel_pace'):
        plugin._sign_job(manual=True)
    text = plugin.notices[0]['text']
    assert '今日已用：获取失败' in text and '💰 当前余额：98' in text
