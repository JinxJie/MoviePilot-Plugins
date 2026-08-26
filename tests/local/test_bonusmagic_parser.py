"""bonusmagic 解析与策略冒烟测试。不依赖 MoviePilot 宿主。"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[2] / "plugins.v2"
sys.path.insert(0, str(HERE))

# stub host so `import bonusmagic` (package __init__) does not require MoviePilot
logging.basicConfig(level=logging.WARNING)
app = types.ModuleType("app")
app_core = types.ModuleType("app.core")
app_config = types.ModuleType("app.core.config")
app_config.settings = types.SimpleNamespace(PROXY="", TZ="Asia/Shanghai")
app_core.config = app_config
app.core = app_core
app_event = types.ModuleType("app.core.event")
app_event.Event = type("Event", (), {})
app_event.eventmanager = types.SimpleNamespace(register=lambda *_a, **_k: (lambda fn: fn))
app_core.event = app_event
app_log = types.ModuleType("app.log")
app_log.logger = logging.getLogger("bonusmagic_parser")
app.log = app_log
app_schemas = types.ModuleType("app.schemas")
app_types = types.ModuleType("app.schemas.types")
app_types.EventType = types.SimpleNamespace(PluginAction="PluginAction")
app_types.NotificationType = types.SimpleNamespace(Plugin="Plugin")
app_schemas.types = app_types
app.schemas = app_schemas
app_plugins = types.ModuleType("app.plugins")
app_plugins._PluginBase = type("_PluginBase", (), {"__init__": lambda self: None})
app.plugins = app_plugins
app_http = types.ModuleType("app.utils.http")
app_http.RequestUtils = type("RequestUtils", (), {})
app_utils = types.ModuleType("app.utils")
app_utils.http = app_http
app.utils = app_utils
aps = types.ModuleType("apscheduler")
aps_cron = types.ModuleType("apscheduler.triggers.cron")
aps_cron.CronTrigger = type("CronTrigger", (), {"from_crontab": staticmethod(lambda x: x)})
aps_bg = types.ModuleType("apscheduler.schedulers.background")
aps_bg.BackgroundScheduler = type("BackgroundScheduler", (), {})
fastapi = types.ModuleType("fastapi")
fastapi.Body = lambda default=None, **_k: default
for name, mod in {
    "app": app, "app.core": app_core, "app.core.config": app_config, "app.core.event": app_event,
    "app.log": app_log, "app.schemas": app_schemas, "app.schemas.types": app_types,
    "app.plugins": app_plugins, "app.utils": app_utils, "app.utils.http": app_http,
    "apscheduler": aps, "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
    "apscheduler.triggers.cron": aps_cron,
    "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
    "apscheduler.schedulers.background": aps_bg,
    "fastapi": fastapi,
}.items():
    sys.modules[name] = mod

from bonusmagic.parser import (  # noqa: E402
    classify_result,
    format_size,
    parse_exchange_items,
    parse_user_stats,
    parse_wait_seconds,
)
from bonusmagic.strategy import pick_item, plan_counts  # noqa: E402
from bonusmagic.adapters.registry import detect_adapter, list_adapters  # noqa: E402
from bonusmagic.adapters.nexusphp import NexusPHPAdapter  # noqa: E402


SAMPLE_INDEX = """
<html><body>
<a href="logout.php">退出</a>
<a href="mybonus.php">魔力值: 25,000</a>
<span>上传量: 1.50 TB</span>
<span>下载量: 500.00 GB</span>
<span>分享率: 3.072</span>
</body></html>
"""

SAMPLE_BONUS = """
<html><body>
<a href="logout.php">退出</a>
当前魔力值: 25000
<form action="mybonus.php?action=exchange" method="post">
  <input type="hidden" name="option" value="1">
  <input type="hidden" name="other" value="">
  兑换 1 GB 上传量，需要 1000 魔力值
  <input type="submit" name="submit" value="交换">
</form>
<form action="mybonus.php?action=exchange" method="post">
  <input type="hidden" name="option" value="2">
  兑换 10 GB 上传量，需要 9000 魔力值
  <input type="submit" name="submit" value="交换">
</form>
<form action="mybonus.php?action=exchange" method="post">
  <input type="hidden" name="option" value="11">
  兑换 1 GB 下载量，需要 800 魔力值
  <input type="submit" name="submit" value="交换">
</form>
<form action="invite.php" method="post">
  <input type="hidden" name="option" value="99">
  邀请名额，需要 50000 魔力值
  <input type="submit" name="submit" value="交换">
</form>
</body></html>
"""

LOGIN_PAGE = """
<html><body>
<form action="login.php"><input type="password" name="password"></form>
</body></html>
"""

NO_PRICE = """
<html><body>
<a href="logout.php">退出</a>
<form action="mybonus.php?action=exchange" method="post">
  <input type="hidden" name="option" value="1">
  神秘礼物
  <input type="submit" name="submit" value="交换">
</form>
</body></html>
"""

SAMPLE_RADIO = """
<html><body>
<a href="logout.php">退出</a>
当前魔力值: 12000
<form action="mybonus.php?action=exchange" method="post">
<table>
<tr><td><input type="radio" name="option" value="1"></td>
    <td>兑换 1.00GB 上传量，需要 1000 魔力值</td></tr>
<tr><td><input type="radio" name="option" value="2"></td>
    <td>兑换 10.00GB 上传量，需要 9000 魔力值</td></tr>
<tr><td><input type="radio" name="option" value="11"></td>
    <td>兑换 1.00GB 下载量，需要 800 魔力值</td></tr>
<tr><td><input type="radio" name="option" value="99"></td>
    <td>邀请名额，需要 50000 魔力值</td></tr>
</table>
<input type="submit" name="submit" value="交换">
</form>
</body></html>
"""

# LongPT / NexusPHP 官方标准结构：四列表头（项目|简介|价格|交换）+ 行内空 form
SAMPLE_LONGPT = """
<html><head><title>LongPT :: test的魔力值 - Powered by NexusPHP</title></head>
<body>
<a href="logout.php">[退出]</a>
<font class="color_bonus">魔力值 </font>[<a href="mybonus.php">使用</a>]: 98,663.2
<font class="color_ratio">分享率:</font> 1.441
<font class="color_uploaded">上传量:</font> 73.34 GB
<font class="color_downloaded">下载量:</font> 50.90 GB
<table align="center" width="940" border="1" cellspacing="0" cellpadding="3">
<tr><td class="colhead" colspan="4" align="center"><font class="big">LongPT魔力值系统</font></td></tr>
<tr><td class="text" align="center" colspan="4">用你的魔力值（当前98,663.2）换东东！</td></tr>
<tr><td class="colhead" align="center">项目</td><td class="colhead" align="left">简介</td><td class="colhead" align="center">价格</td><td class="colhead" align="center">交换</td></tr>
<tr>
<form action="?action=exchange" method="post"></form>
<td class="rowhead_center"><input type="hidden" name="option" value="3"><b>3</b></td>
<td class="rowfollow" align="left"><h1>10.0 GB上传量</h1>如果有足够的魔力值，你可以用它来换取上传量。</td>
<td class="rowfollow" align="center">13,000</td>
<td class="rowfollow" align="center"><input type="submit" name="submit" value="交换"></td>
</tr>
<tr>
<form action="?action=exchange" method="post"></form>
<td class="rowhead_center"><input type="hidden" name="option" value="11"><b>11</b></td>
<td class="rowfollow" align="left"><h1>10.0 GB下载量</h1>如果有足够的魔力值，你可以用它来换取下载量。</td>
<td class="rowfollow" align="center">10,000</td>
<td class="rowfollow" align="center"><input type="submit" name="submit" value="需要更多魔力值" disabled="disabled"></td>
</tr>
<tr>
<form action="?action=exchange" method="post"></form>
<td class="rowhead_center"><input type="hidden" name="option" value="9"><b>9</b></td>
<td class="rowfollow" align="left"><h1>慈善捐赠</h1>你可以将你的魔力值通过慈善捐赠送与有需要的用户群体。</td>
<td class="rowfollow nowrap" align="center">最少1,000<br>最多50,000</td>
<td class="rowfollow" align="center"><input type="submit" name="submit" value="慈善捐赠"></td>
</tr>
</table>
</body></html>
"""

# 兑换后站点直接回渲染魔力页（无成功/失败文案）——应视为已受理
SAMPLE_ACCEPTED = """
<html><head><title>LongPT :: test的魔力值 - Powered by NexusPHP</title></head>
<body>
<a href="logout.php">[退出]</a>
<font class="color_bonus">魔力值 </font>[<a href="mybonus.php">使用</a>]: 88,663.2
<table>
<tr><form action="?action=exchange" method="post"></form>
<td><input type="hidden" name="option" value="4"></td>
<td>10.0 GB 下载量</td><td>10,000</td>
<td><input type="submit" name="submit" value="交换"></td></tr>
</table>
</body></html>
"""


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: {actual!r} != {expected!r}")


def main():
    stats = parse_user_stats(SAMPLE_INDEX)
    assert_eq(stats["logged_in"], True, "logged_in")
    assert_eq(stats["bonus"], 25000.0, "bonus")
    assert_eq(stats["upload"], int(1.5 * 1024 ** 4), "upload")
    assert_eq(stats["download"], int(500 * 1024 ** 3), "download")
    assert stats["ratio"] and abs(stats["ratio"] - 3.072) < 0.01, stats["ratio"]

    items = parse_exchange_items(SAMPLE_BONUS, "https://pt.example/")
    radio_items = parse_exchange_items(SAMPLE_RADIO, "https://pt.example/")
    assert {(it["option"], it["kind"]) for it in radio_items} == {(it["option"], it["kind"]) for it in items}
    kinds = {(it["option"], it["kind"], it["cost"], it["size_bytes"]) for it in items}
    assert ("1", "upload", 1000.0, 1024 ** 3) in kinds, items
    assert ("2", "upload", 9000.0, 10 * 1024 ** 3) in kinds, items
    assert ("11", "download", 800.0, 1024 ** 3) in kinds, items
    assert all(it["option"] != "99" for it in items), "invite should be skipped"
    assert all(it["action"].startswith("https://pt.example/") for it in items)

    cheap = pick_item(items, "upload", "cheap")
    biggest = pick_item(items, "upload", "max")
    assert cheap and cheap["option"] == "1"
    assert biggest and biggest["option"] == "2"

    up, down, reason = plan_counts(
        bonus=25000,
        ratio=0.5,
        upload_item=cheap,
        download_item=pick_item(items, "download", "cheap"),
        enable_upload=True,
        enable_download=True,
        strategy="keep",
        fixed_upload=0,
        fixed_download=0,
        max_upload=10,
        max_download=10,
        keep_bonus=10000,
        ratio_threshold=1.0,
        priority="auto",
        max_spend=0,
    )
    # usable 15000, cheap upload 1000, prefer upload because ratio 0.5 < 1.0, max_upload 10
    # 上传 10 次用掉 10000，剩余 5000 继续兑下载 6 次
    assert_eq(up, 10, "keep upload times")
    assert_eq(down, 6, "leftover bonus goes to download")
    assert "优先 upload" in reason

    up2, down2, _ = plan_counts(
        bonus=25000,
        ratio=2.0,
        upload_item=cheap,
        download_item=pick_item(items, "download", "cheap"),
        enable_upload=True,
        enable_download=True,
        strategy="fixed",
        fixed_upload=5,
        fixed_download=5,
        max_upload=10,
        max_download=10,
        keep_bonus=10000,
        ratio_threshold=1.0,
        priority="auto",
        max_spend=0,
    )
    # remaining 15000, prefer download first: 5*800=4000, then upload 5*1000=5000
    assert_eq(up2, 5, "fixed upload")
    assert_eq(down2, 5, "fixed download")

    up3, down3, _ = plan_counts(
        bonus=25000,
        ratio=2.0,
        upload_item=None,
        download_item=pick_item(items, "download", "cheap"),
        enable_upload=True,
        enable_download=True,
        strategy="max",
        fixed_upload=0,
        fixed_download=0,
        max_upload=99,
        max_download=99,
        keep_bonus=0,
        ratio_threshold=1.0,
        priority="upload",
        max_spend=0,
    )
    assert_eq(up3, 0, "no upload item parsed")
    assert down3 > 0, "download still available"

    empty = parse_exchange_items(NO_PRICE, "https://pt.example/")
    assert_eq(empty, [], "no price => no items")

    # LongPT / 官方四列表格：空 form + 价格列 + [使用] 余额
    lp_stats = parse_user_stats(SAMPLE_LONGPT)
    assert_eq(lp_stats["bonus"], 98663.2, "LongPT bonus with [使用]")
    assert_eq(lp_stats["ratio"], 1.441, "LongPT ratio")
    lp = parse_exchange_items(SAMPLE_LONGPT, "https://longpt.org/mybonus.php")
    lp_kinds = {(it["option"], it["kind"], it["cost"]) for it in lp}
    assert ("3", "upload", 13000.0) in lp_kinds, lp_kinds
    assert ("11", "download", 10000.0) in lp_kinds, lp_kinds
    assert all(opt != "9" for opt, _, _ in lp_kinds), f"charity row must be excluded: {lp_kinds}"
    # 置灰按钮（需要更多魔力值）必须标记 disabled，供执行层跳过
    dis = {it["option"]: it.get("disabled") for it in lp}
    assert dis.get("11") is True and dis.get("3") is False, dis
    # 价格不能是体积数字（10 GB 的 10）
    for it in lp:
        assert it["cost"] >= 100, f"cost too small, likely size leak: {it}"
    # 相对 action 正确拼到 mybonus.php
    assert all(it["action"].startswith("https://longpt.org/mybonus.php") for it in lp), [it["action"] for it in lp]
    # pick_item 应跳过 disabled 行（11 是唯一下载项且置灰 => 无可用下载项）
    lp_down = pick_item(lp, "download", "cheap")
    assert lp_down is None, f"disabled row must not be picked: {lp_down}"

    login_stats = parse_user_stats(LOGIN_PAGE)
    assert_eq(login_stats["logged_in"], False, "login page")

    wait = parse_wait_seconds("请等待 12 秒后再试")
    assert_eq(wait, 12.0, "wait seconds")
    wait_h = parse_wait_seconds("<html></html>", {"Retry-After": "8"})
    assert_eq(wait_h, 8.0, "retry-after")

    ok = classify_result("兑换成功，上传量增加 1 GB", 200)
    assert ok["success"] and ok["code"] == "ok"
    nob = classify_result("魔力值不足，无法兑换", 200)
    assert nob["code"] == "no_bonus"
    rl = classify_result("请等待 7 秒", 200)
    assert rl["code"] == "rate_limit" and rl["wait_seconds"] == 7.0
    lg = classify_result(LOGIN_PAGE, 200)
    assert lg["code"] == "login"

    # 兑换后回渲染魔力页且无错误 => 已受理成功（借鉴 HTTP 兑换脚本判定）
    acc = classify_result(SAMPLE_ACCEPTED, 200)
    assert acc["success"] and acc["code"] == "ok", acc
    # 有明确失败文案时仍按失败
    fail_page = SAMPLE_ACCEPTED.replace("88,663.2", "88,663.2 魔力值不足")
    fb = classify_result(fail_page, 200)
    assert not fb["success"] and fb["code"] == "no_bonus", fb

    assert format_size(1024 ** 3) == "1.00 GB"

    assert "nexusphp" in list_adapters()
    adapter, score, how = detect_adapter({"url": "https://pt.example/"}, SAMPLE_INDEX)
    assert adapter.name == "nexusphp" and how == "auto" and score >= 0.5, (adapter, score, how)
    pinned, pscore, phow = detect_adapter(
        {"url": "https://pt.example/", "overrides": {"architecture": "nexusphp"}},
        "<html></html>",
    )
    assert pinned.name == "nexusphp" and phow == "pinned" and pscore == 1.0
    nphp = NexusPHPAdapter()
    catalog = nphp.parse_catalog(SAMPLE_BONUS, "https://pt.example/", {"url": "https://pt.example/"})
    assert catalog and catalog[0]["kind"] == "upload"
    method, url, data = nphp.build_exchange({"url": "https://pt.example/"}, catalog[0])
    assert method == "post" and "option" in data and "mybonus" in url
    custom = nphp.catalog_path({"overrides": {"catalog_path": "bonus.php"}})
    assert custom == "bonus.php"
    fallback, fscore, fhow = detect_adapter({"url": "https://unknown.example/"}, "<html><body>hello</body></html>")
    assert fallback.name == "nexusphp" and fhow == "fallback" and fscore == 0.0
    print("OK parser+strategy+adapter smoke")


if __name__ == "__main__":
    main()
