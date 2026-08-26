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

cmd = p.get_command()
assert cmd and all("cmd" in c and "event" in c for c in cmd)
api = p.get_api()
assert api and all("endpoint" in a and "methods" in a for a in api)
svc = p.get_service()
assert svc, "enabled plugin should register cron service"
print(f"get_command({len(cmd)}) / get_api({len(api)}) / get_service({len(svc)})")

# 核心调度不应直接绑定 mybonus.php
src = Path(PLUGIN_DIR, "bonusmagic", "__init__.py").read_text()
assert "self._get(site, \"mybonus.php\")" not in src
assert "from .adapters import detect_adapter" in src
print("core scheduler is adapter-decoupled")

print("OK plugin contract smoke")
