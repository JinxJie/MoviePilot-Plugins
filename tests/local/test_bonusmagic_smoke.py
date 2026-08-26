"""bonusmagic 插件契约冒烟。stub MoviePilot 宿主，不发真实请求。"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")

app = types.ModuleType("app")
app_core = types.ModuleType("app.core")
app_config = types.ModuleType("app.core.config")


class _Settings:
    PROXY = ""
    TZ = "Asia/Shanghai"
    API_TOKEN = "test-token"


app_config.settings = _Settings()
app_core.config = app_config
app.core = app_core

app_event = types.ModuleType("app.core.event")


class Event:
    def __init__(self, event_data=None):
        self.event_data = event_data or {}


class _EventManager:
    def register(self, *_a, **_k):
        def deco(fn):
            return fn
        return deco


app_event.Event = Event
app_event.eventmanager = _EventManager()
app_core.event = app_event

app_log = types.ModuleType("app.log")
app_log.logger = logging.getLogger("bonusmagic_smoke")
app.log = app_log

app_schemas = types.ModuleType("app.schemas")
app_types = types.ModuleType("app.schemas.types")


class _EventType:
    PluginAction = "PluginAction"


class _NotificationType:
    Plugin = "Plugin"


app_types.EventType = _EventType
app_types.NotificationType = _NotificationType
app_schemas.types = app_types
app.schemas = app_schemas

app_plugins = types.ModuleType("app.plugins")


class _PluginBase:
    def __init__(self):
        self._data = {}

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def update_service(self, services):
        pass

    def update_config(self, config):
        self._config = config

    def post_message(self, mtype=None, title="", text=""):
        print(f"  [通知] {title}: {(text or '').splitlines()[0]}")


app_plugins._PluginBase = _PluginBase
app.plugins = app_plugins

app_utils = types.ModuleType("app.utils")
app_http = types.ModuleType("app.utils.http")


class RequestUtils:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_res(self, *a, **k):
        return None

    def post_res(self, *a, **k):
        return None


app_http.RequestUtils = RequestUtils
app_utils.http = app_http
app.utils = app_utils

for name, mod in {
    "app": app,
    "app.core": app_core,
    "app.core.config": app_config,
    "app.core.event": app_event,
    "app.log": app_log,
    "app.schemas": app_schemas,
    "app.schemas.types": app_types,
    "app.plugins": app_plugins,
    "app.utils": app_utils,
    "app.utils.http": app_http,
}.items():
    sys.modules[name] = mod

fastapi = types.ModuleType("fastapi")


def Body(default=None, **_k):
    return default


fastapi.Body = Body
sys.modules["fastapi"] = fastapi

aps = types.ModuleType("apscheduler")
aps_triggers = types.ModuleType("apscheduler.triggers")
aps_cron = types.ModuleType("apscheduler.triggers.cron")


class _CronTrigger:
    @staticmethod
    def from_crontab(expr):
        return f"cron:{expr}"


aps_cron.CronTrigger = _CronTrigger
aps_triggers.cron = aps_cron
aps.triggers = aps_triggers
aps_schedulers = types.ModuleType("apscheduler.schedulers")
aps_bg = types.ModuleType("apscheduler.schedulers.background")


class _BackgroundScheduler:
    def __init__(self, **kwargs):
        self.jobs = []

    def add_job(self, **kwargs):
        self.jobs.append(kwargs)

    def start(self):
        pass


aps_bg.BackgroundScheduler = _BackgroundScheduler
aps_schedulers.background = aps_bg
aps.schedulers = aps_schedulers
for name, mod in {
    "apscheduler": aps,
    "apscheduler.triggers": aps_triggers,
    "apscheduler.triggers.cron": aps_cron,
    "apscheduler.schedulers": aps_schedulers,
    "apscheduler.schedulers.background": aps_bg,
}.items():
    sys.modules[name] = mod

PLUGIN_DIR = str(Path(__file__).resolve().parents[2] / "plugins.v2")
sys.path.insert(0, PLUGIN_DIR)
mod = __import__("bonusmagic", fromlist=["BonusMagic"])
PluginCls = getattr(mod, "BonusMagic")

p = PluginCls()
CONFIG = {
    "enabled": True,
    "onlyonce": False,
    "notify": True,
    "use_system_proxy": True,
    "cron": "0 10 * * *",
    "architecture": "auto",
}
p.init_plugin(CONFIG)

form, defaults = p.get_form()
assert isinstance(form, list) and form, "get_form must return a non-empty field list"
print(f"get_form: {len(form)} fields")

form_models = []


def _collect(items):
    for it in items or []:
        if isinstance(it, dict):
            if "model" in it.get("props", {}):
                form_models.append(it["props"]["model"])
            _collect(it.get("content"))


_collect(form)
for required in ("enabled", "onlyonce", "notify", "use_system_proxy", "architecture"):
    assert required in form_models, f"form missing: {required}"
print("switch trio + architecture present")

banned = []


def _scan(items):
    for it in items or []:
        if isinstance(it, dict):
            name = it.get("component")
            if name in ("VAvatar", "VProgressLinear", "VApexChart"):
                banned.append(name)
            _scan(it.get("content"))


_scan(form)
page = p.get_page()
_scan(page)
assert not banned, banned
print(f"get_page: {len(page)} blocks, no Lite-banned components")

page_dump = str(page)
assert "站点操作中心" in page_dump
for label in ("站点名称", "当前魔力", "当前上传", "当前下载", "当前分享率", "可兑换上传", "可兑换下载", "单次兑换消耗", "当前兑换限制", "推荐方案", "当前任务", "最近结果"):
    pass
# 无站点时仍有操作中心和空态
assert "没有可用站点" in page_dump or "已配置" in page_dump
assert "VCheckbox" not in page_dump
assert "VAvatar" not in page_dump
assert "/refresh" in page_dump and "/select" in page_dump and "/run" in page_dump
assert "智能推荐" in page_dump and "一键智能兑换" in page_dump and "批量兑换" in page_dump
print("homepage is site operation center")

# 注入两个假站点，核对卡片字段
p._list_sites = lambda: [  # type: ignore
    {"name": "站A", "domain": "a.example", "url": "https://a.example", "cookie": "c=1", "ua": "ua", "proxy": 0, "timeout": 20, "overrides": {}},
    {"name": "站B", "domain": "b.example", "url": "https://b.example", "cookie": "c=1", "ua": "ua", "proxy": 0, "timeout": 20, "overrides": {}},
]
p.save_data("dashboard", {
    "a.example": {
        "status": "ready", "bonus": 12345, "upload": 1024 ** 3, "download": 512 * 1024 ** 2,
        "ratio": 2.0, "item_upload": "1 GB / 1000 魔力", "item_download": "",
        "afford_upload": 12, "afford_download": 0, "plan_upload": 3, "plan_download": 0,
        "plan_reason": "保留余额优先兑换上传", "message": "保留余额优先兑换上传",
    }
})
page2 = p.get_page()
dump2 = str(page2)
assert "站A" in dump2 and "站B" in dump2
for needle in ("当前魔力", "当前上传", "当前下载", "当前分享率", "可兑换上传", "可兑换下载", "单次兑换消耗", "当前兑换限制", "推荐方案", "当前任务", "最近结果", "选择本站", "刷新", "立即兑换", "仅选可兑换", "仅选有推荐", "全选", "取消全选"):
    assert needle in dump2, needle
assert "12,345" in dump2
assert "VCheckbox" not in dump2
print("site cards render required fields")

# 勾选：单站 / 全选 / 取消全选 / 仅选可兑换 / 仅选有推荐 / 多站
p.save_data("dashboard", {
    "a.example": {
        "status": "ready", "bonus": 125000, "upload": 1024 ** 3, "download": 512 * 1024 ** 2,
        "ratio": 2.0, "item_upload": "1 GB / 1000 魔力", "item_download": "",
        "afford_upload": 12, "afford_download": 0, "plan_upload": 10, "plan_download": 0,
        "plan_reason": "保留余额优先兑换上传", "message": "保留余额优先兑换上传",
    },
    "b.example": {
        "status": "idle", "bonus": 100, "upload": 0, "download": 0, "ratio": 0,
        "item_upload": "", "item_download": "", "afford_upload": 0, "afford_download": 0,
        "plan_upload": 0, "plan_download": 0, "plan_reason": "魔力不足",
    },
})
assert p._api_select(domain="a.example", selected="1")["data"] == ["a.example"]
assert set(p._api_select(domains="a.example,b.example", selected="1")["data"]) == {"a.example", "b.example"}
assert p._api_select(scope="none")["data"] == []
assert p._api_select(scope="all", selected="1")["data"] == ["a.example", "b.example"]
assert p._api_select(scope="ready")["data"] == ["a.example"]
assert p._api_select(scope="recommended")["data"] == ["a.example"]
assert p._api_select(scope="none")["data"] == []
page3 = str(p.get_page())
assert "☐ 未选" in page3
print("select modes ok")

# 单站手动执行面板：只勾一站时出现魔力/推荐/预计消耗/立即兑换
assert p._api_select(domain="a.example", selected="1")["data"] == ["a.example"]
page4 = str(p.get_page())
assert "① 单站点操作" in page4
assert "☑ 站A" in page4
assert "当前魔力值：125,000" in page4
assert "上传 ×10" in page4
assert "预计消耗：10,000" in page4
assert "立即兑换" in page4
assert "仅处理 站A，不影响其他站点" in page4
assert "plugin/BonusMagic/run?apikey=test-token&domain=a.example" in page4
# 勾两站时显示批量面板，不显示单站面板
assert set(p._api_select(domains="a.example,b.example", selected="1")["data"]) == {"a.example", "b.example"}
page5 = str(p.get_page())
assert "① 单站点操作" not in page5
assert "② 批量操作" in page5
assert "批量兑换" in page5
# domain 参数只跑目标站
called = {}
def _fake_run(manual=False, targets=None):
    called["targets"] = targets
    return {"ok": True}
p._run_job = _fake_run  # type: ignore
assert p._api_run(domain="a.example")["success"] is True
assert called["targets"] == ["a.example"]
# 批量兑换只跑勾选站
called.clear()
assert p._api_run(scope="selected")["success"] is True
assert set(called["targets"]) == {"a.example", "b.example"}
print("single/batch exec panels ok")

# 智能推荐 / 一键智能兑换
def _fake_refresh(domain=None):
    rows = [
        {
            "name": "HHClub", "domain": "hh.example", "status": "ready", "bonus": 125000, "ratio": 7.92,
            "item_upload": "1 GB / 1000 魔力", "item_download": "", "plan_upload": 10, "plan_download": 0,
            "plan_reason": "保留余额优先兑换上传", "message": "保留余额优先兑换上传",
            "afford_upload": 10, "afford_download": 0,
        },
        {
            "name": "馒头", "domain": "pt.example", "status": "ready", "bonus": 86000, "ratio": 0.72,
            "item_upload": "1 GB / 2000 魔力", "item_download": "", "plan_upload": 20, "plan_download": 0,
            "plan_reason": "保留余额优先兑换上传", "message": "保留余额优先兑换上传",
            "afford_upload": 20, "afford_download": 0,
        },
        {
            "name": "站点C", "domain": "c.example", "status": "no_bonus", "bonus": 5000, "ratio": 12.4,
            "item_upload": "1 GB / 1000 魔力", "item_download": "", "plan_upload": 0, "plan_download": 0,
            "plan_reason": "暂不兑换", "message": "暂不兑换",
            "afford_upload": 0, "afford_download": 0,
        },
    ]
    p._merge_dashboard(rows)
    return rows
p._refresh_sites = _fake_refresh  # type: ignore
# dashboard rows 来自 list_sites；补齐三站
p._list_sites = lambda: [  # type: ignore
    {"name": "HHClub", "domain": "hh.example", "url": "https://hh.example", "cookie": "c=1", "ua": "ua", "proxy": 0, "timeout": 20, "overrides": {}},
    {"name": "馒头", "domain": "pt.example", "url": "https://pt.example", "cookie": "c=1", "ua": "ua", "proxy": 0, "timeout": 20, "overrides": {}},
    {"name": "站点C", "domain": "c.example", "url": "https://c.example", "cookie": "c=1", "ua": "ua", "proxy": 0, "timeout": 20, "overrides": {}},
]
rec = p._api_recommend()
assert rec["success"] is True
assert rec["data"]["actionable"] == 2
assert set(rec["data"]["domains"]) == {"hh.example", "pt.example"}
assert set(p._load_selected()) == {"hh.example", "pt.example"}
page6 = str(p.get_page())
assert "③ 智能推荐结果" in page6
assert "☑ HHClub" in page6 and "☐ 站点C" in page6
assert "魔力值：125,000" in page6
assert "分享率：7.92" in page6
assert "推荐：上传 ×10" in page6
assert "消耗：10,000" in page6
assert "执行智能推荐" in page6
assert "一键智能兑换" in page6
called.clear()
smart = p._api_smart()
assert smart["success"] is True
assert set(called["targets"]) == {"hh.example", "pt.example"}
print("smart recommend / one-click ok")

cmd = p.get_command()
assert cmd and all("cmd" in c and "event" in c for c in cmd)
api = p.get_api()
assert api and all("endpoint" in a and "methods" in a for a in api)
paths = {a["path"] for a in api}
assert {"/run", "/refresh", "/select", "/recommend", "/smart", "/records"} <= paths, paths
svc = p.get_service()
assert svc, "enabled plugin should register cron service"
print(f"get_command({len(cmd)}) / get_api({len(api)}) / get_service({len(svc)})")

# 核心调度不应直接绑定 mybonus.php
src = Path(PLUGIN_DIR, "bonusmagic", "__init__.py").read_text()
assert "self._get(site, \"mybonus.php\")" not in src
assert "from .adapters import detect_adapter" in src
print("core scheduler is adapter-decoupled")

print("OK plugin contract smoke")
