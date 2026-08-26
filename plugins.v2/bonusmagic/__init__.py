"""
魔力值自动兑换插件 - MoviePilot V2/V3 兼容

核心调度（任务/并发/限速/策略/日志/安全）与站点架构解耦。
首期适配器：NexusPHP。后续新架构只加 adapters/ 下独立文件。
不模拟点击，由适配器发出 HTTP 请求并解析结果。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .adapters import detect_adapter
from .parser import format_size
from .strategy import pick_item, plan_counts


def _load_site_oper():
    for module_path in ("app.db.site_oper", "app.db.siteoper", "app.db.oper.site"):
        try:
            module = __import__(module_path, fromlist=["SiteOper"])
            oper = getattr(module, "SiteOper", None)
            if oper:
                return oper
        except Exception:
            continue
    return None


def _number(value, default, cast=float):
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _parse_overrides(text: str) -> dict:
    """每行: 域名=architecture:nexusphp;catalog_path:mybonus.php"""
    mapping = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, rest = line.split("=", 1)
        domain = key.strip().lower()
        overrides = {}
        for part in rest.split(";"):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                k, v = part.split(":", 1)
                overrides[k.strip()] = v.strip()
            else:
                overrides["architecture"] = part
        if domain:
            mapping[domain] = overrides
    return mapping


class BonusMagic(_PluginBase):
    plugin_name = "NexusPHP 魔力兑换"
    plugin_desc = "读取 MoviePilot 站点，按架构适配器自动解析魔力商店并兑换上传/下载量；自适应限速、多站并发、安全熔断"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/bonusmagic.png"
    plugin_version = "1.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "bonusmagic_"
    plugin_order = 0
    auth_level = 2

    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._notify = True
        self._use_system_proxy = True
        self._cron = "0 10 * * *"
        self._site_filter = ""
        self._enable_upload = True
        self._enable_download = False
        self._item_prefer = "cheap"
        self._strategy = "keep"
        self._priority = "auto"
        self._keep_bonus = 10000.0
        self._ratio_threshold = 1.0
        self._max_spend = 0.0
        self._fixed_upload = 0
        self._fixed_download = 0
        self._max_upload = 10
        self._max_download = 0
        self._safety_buffer = 1.5
        self._retry = 3
        self._concurrency = 3
        self._architecture = "auto"
        self._site_overrides = ""
        self._onlyonce = False
        self._running = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled") or False)
            self._notify = True if config.get("notify") is None else bool(config.get("notify"))
            self._use_system_proxy = True if config.get("use_system_proxy") is None else bool(config.get("use_system_proxy"))
            self._cron = config.get("cron") or "0 10 * * *"
            self._site_filter = (config.get("site_filter") or "").strip()
            self._enable_upload = True if config.get("enable_upload") is None else bool(config.get("enable_upload"))
            self._enable_download = bool(config.get("enable_download") or False)
            self._item_prefer = config.get("item_prefer") or "cheap"
            self._strategy = config.get("strategy") or "keep"
            self._priority = config.get("priority") or "auto"
            self._keep_bonus = _number(config.get("keep_bonus"), 10000.0, float)
            self._ratio_threshold = _number(config.get("ratio_threshold"), 1.0, float)
            self._max_spend = _number(config.get("max_spend"), 0.0, float)
            self._fixed_upload = _number(config.get("fixed_upload"), 0, int)
            self._fixed_download = _number(config.get("fixed_download"), 0, int)
            self._max_upload = _number(config.get("max_upload"), 10, int)
            self._max_download = _number(config.get("max_download"), 0, int)
            self._safety_buffer = _number(config.get("safety_buffer"), 1.5, float)
            self._retry = _number(config.get("retry"), 3, int)
            self._concurrency = max(1, _number(config.get("concurrency"), 3, int))
            self._architecture = config.get("architecture") or "auto"
            self._site_overrides = (config.get("site_overrides") or "").strip()
            self._onlyonce = bool(config.get("onlyonce") or False)

        if self._onlyonce:
            scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("魔力兑换：立即运行一次")
            scheduler.add_job(
                func=self._run_job,
                trigger="date",
                run_date=datetime.now() + timedelta(seconds=3),
                name="魔力兑换立即运行",
                kwargs={"manual": True},
            )
            scheduler.start()
            self._onlyonce = False
            self.__update_config()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "use_system_proxy": self._use_system_proxy,
            "cron": self._cron,
            "site_filter": self._site_filter,
            "enable_upload": self._enable_upload,
            "enable_download": self._enable_download,
            "item_prefer": self._item_prefer,
            "strategy": self._strategy,
            "priority": self._priority,
            "keep_bonus": self._keep_bonus,
            "ratio_threshold": self._ratio_threshold,
            "max_spend": self._max_spend,
            "fixed_upload": self._fixed_upload,
            "fixed_download": self._fixed_download,
            "max_upload": self._max_upload,
            "max_download": self._max_download,
            "safety_buffer": self._safety_buffer,
            "retry": self._retry,
            "concurrency": self._concurrency,
            "architecture": self._architecture,
            "site_overrides": self._site_overrides,
            "onlyonce": self._onlyonce,
        })

    def get_state(self) -> bool:
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        return [{
            "cmd": "/bonusmagic",
            "event": EventType.PluginAction,
            "desc": "立即执行魔力兑换",
            "category": "站点",
            "data": {"action": "bonusmagic_run"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "summary": "立即执行兑换",
                "description": "手动触发一轮魔力兑换",
                "endpoint": self._api_run,
                "methods": ["POST"],
                "auth": "bear",
            },
            {
                "path": "/records",
                "summary": "获取兑换记录",
                "description": "最近兑换任务记录",
                "endpoint": self._api_records,
                "methods": ["GET"],
                "auth": "bear",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        from .config_form import build_form
        return build_form()

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "bonusmagic",
                "name": "魔力兑换",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._run_job,
                "kwargs": {"manual": False},
            }]
        return []

    def stop_service(self):
        return

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        if not event or not getattr(event, "event_data", None):
            return
        if event.event_data.get("action") != "bonusmagic_run":
            return
        logger.info("收到 /bonusmagic 命令，开始执行魔力兑换")
        self._run_job(manual=True)

    def _api_run(self) -> dict:
        try:
            summary = self._run_job(manual=True)
            return {"success": True, "message": "兑换任务已执行", "data": summary}
        except Exception as e:
            logger.error(f"魔力兑换 API 失败：{e}")
            return {"success": False, "message": str(e)}

    def _api_records(self) -> dict:
        return {"success": True, "data": self._load_records()}

    # ======================== 站点与请求 ========================

    @staticmethod
    def _system_proxy() -> str:
        try:
            proxy_host = getattr(getattr(settings, "PROXY", None), "host", None)
            if proxy_host:
                return str(proxy_host).strip()
        except Exception:
            pass
        return (os.environ.get("PROXY_HOST") or os.environ.get("https_proxy") or
                os.environ.get("HTTPS_PROXY") or "").split(",")[0].strip() or ""

    def _proxies(self, site_proxy: Any = None) -> Optional[dict]:
        # 站点表 proxy 是 0/1，不是 URL。系统代理优先。
        use_site_proxy = site_proxy in (1, True, "1")
        if self._use_system_proxy or use_site_proxy:
            sys_proxy = self._system_proxy()
            if sys_proxy:
                return {"http": sys_proxy, "https": sys_proxy}
        if isinstance(site_proxy, str) and site_proxy.startswith("http"):
            return {"http": site_proxy, "https": site_proxy}
        return None

    def _list_sites(self) -> List[dict]:
        oper_cls = _load_site_oper()
        if not oper_cls:
            logger.error("找不到站点管理模块 SiteOper，无法读取 MoviePilot 站点")
            return []
        try:
            oper = oper_cls()
            sites = []
            if hasattr(oper, "list_active"):
                sites = oper.list_active() or []
            elif hasattr(oper, "list"):
                sites = [s for s in (oper.list() or []) if getattr(s, "is_active", True)]
        except Exception as e:
            logger.error(f"读取站点列表失败：{e}")
            return []

        filters = [x.strip().lower() for x in self._site_filter.split(",") if x.strip()]
        result = []
        for site in sites:
            name = getattr(site, "name", "") or ""
            domain = getattr(site, "domain", "") or ""
            url = (getattr(site, "url", "") or "").strip()
            if not url and domain:
                url = f"https://{domain}"
            cookie = (getattr(site, "cookie", "") or "").strip()
            if not url or not cookie:
                continue
            if filters:
                blob = f"{name} {domain} {url}".lower()
                if not any(f in blob for f in filters):
                    continue
            domain_key = (domain or (urlparse(url).hostname or "")).lower()
            overrides = self._site_override_map().get(domain_key) or {}
            result.append({
                "id": getattr(site, "id", None),
                "name": name or domain or url,
                "domain": domain or (urlparse(url).hostname or ""),
                "url": url if "://" in url else f"https://{url}",
                "cookie": cookie,
                "ua": getattr(site, "ua", None) or self.DEFAULT_UA,
                "proxy": getattr(site, "proxy", None),
                "timeout": getattr(site, "timeout", None) or 20,
                "overrides": overrides,
            })
        return result

    def _site_override_map(self) -> dict:
        return _parse_overrides(self._site_overrides)

    def _client(self, site: dict, adapter=None) -> RequestUtils:
        origin = f"{urlparse(site['url']).scheme}://{urlparse(site['url']).netloc}"
        referer_path = adapter.referer_path(site) if adapter else "index.php"
        headers = {
            "User-Agent": site.get("ua") or self.DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": origin,
            "Referer": urljoin(site["url"].rstrip("/") + "/", referer_path),
        }
        return RequestUtils(
            headers=headers,
            cookies=site["cookie"],
            proxies=self._proxies(site.get("proxy")),
            timeout=int(site.get("timeout") or 20),
            referer=headers["Referer"],
        )

    def _get(self, site: dict, path: str, adapter=None) -> Tuple[int, str, dict]:
        url = urljoin(site["url"].rstrip("/") + "/", path)
        ru = self._client(site, adapter)
        resp = ru.get_res(url=url)
        if resp is None:
            return 0, "", {}
        return resp.status_code, resp.text or "", dict(resp.headers or {})

    def _post(self, site: dict, url: str, data: dict, adapter=None) -> Tuple[int, str, dict]:
        ru = self._client(site, adapter)
        resp = ru.post_res(url=url, data=data)
        if resp is None:
            return 0, "", {}
        return resp.status_code, resp.text or "", dict(resp.headers or {})

    # ======================== 限速学习 ========================

    def _load_limits(self) -> dict:
        return self.get_data("site_limits") or {}

    def _save_limits(self, limits: dict):
        self.save_data("site_limits", limits)

    def _learned_wait(self, domain: str) -> float:
        rec = (self._load_limits().get(domain) or {})
        return float(rec.get("interval") or 0)

    def _remember_wait(self, domain: str, seconds: float):
        if seconds is None or seconds < 0:
            return
        limits = self._load_limits()
        old = float((limits.get(domain) or {}).get("interval") or 0)
        # 取历史与本次较大值，避免偶发短间隔把站点打爆
        interval = max(old, float(seconds))
        limits[domain] = {
            "interval": interval,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_limits(limits)

    def _sleep_for_site(self, domain: str, wait: Optional[float], reason: str = ""):
        learned = self._learned_wait(domain)
        need = 0.0
        if wait is not None:
            need = max(need, float(wait))
            self._remember_wait(domain, float(wait))
        if learned:
            need = max(need, learned)
        if need > 0:
            delay = need + max(0.0, self._safety_buffer)
            logger.info(f"{domain} 等待 {delay:.1f}s（限制 {need:.1f}s + 缓冲 {self._safety_buffer:.1f}s）{reason}")
            time.sleep(delay)
        elif self._safety_buffer > 0:
            # 无限制站点不强制 10 秒，只做一个很小的缓冲
            tiny = min(self._safety_buffer, 0.5)
            if tiny > 0:
                time.sleep(tiny)

    # ======================== 核心任务 ========================

    def _run_job(self, manual: bool = False) -> dict:
        if not manual and not self._enabled:
            return {"ok": False, "message": "插件未启用"}
        if self._running:
            logger.warning("魔力兑换任务正在运行，跳过重复触发")
            return {"ok": False, "message": "任务进行中"}
        self._running = True
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"魔力兑换开始（{'手动' if manual else '定时'}）")
        try:
            sites = self._list_sites()
            if not sites:
                msg = "没有可用站点（需在 MoviePilot 站点管理中启用并配置 Cookie）"
                logger.warning(msg)
                summary = {
                    "time": started,
                    "ok": False,
                    "message": msg,
                    "sites": [],
                    "exchanges": 0,
                    "spent": 0,
                    "got_upload": 0,
                    "got_download": 0,
                    "success": 0,
                    "fail": 0,
                }
                self._save_round(summary)
                return summary

            results = []
            workers = min(self._concurrency, len(sites))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._run_site, site): site for site in sites}
                for fut in as_completed(futs):
                    site = futs[fut]
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        logger.error(f"{site.get('name')} 兑换异常：{e}")
                        results.append({
                            "name": site.get("name"),
                            "domain": site.get("domain"),
                            "status": "failed",
                            "message": str(e),
                            "exchanges": 0,
                            "spent": 0,
                            "got_upload": 0,
                            "got_download": 0,
                            "success": 0,
                            "fail": 1,
                            "logs": [str(e)],
                        })

            summary = self._summarize(started, results)
            self._save_round(summary)
            self._notify_summary(summary, manual)
            logger.info(
                f"魔力兑换结束：站点 {len(results)}，成功 {summary['success']}，失败 {summary['fail']}，"
                f"消耗 {summary['spent']}，获得上传 {format_size(summary['got_upload'])} / 下载 {format_size(summary['got_download'])}"
            )
            return summary
        finally:
            self._running = False

    def _run_site(self, site: dict) -> dict:
        name = site["name"]
        domain = site["domain"]
        rec = {
            "name": name,
            "domain": domain,
            "status": "running",
            "message": "",
            "bonus": None,
            "upload": None,
            "download": None,
            "ratio": None,
            "plan_upload": 0,
            "plan_download": 0,
            "exchanges": 0,
            "spent": 0.0,
            "got_upload": 0,
            "got_download": 0,
            "success": 0,
            "fail": 0,
            "wait": None,
            "item_upload": "",
            "item_download": "",
            "architecture": "",
            "logs": [],
        }

        def log(msg: str):
            logger.info(f"[{name}] {msg}")
            rec["logs"].append(msg)

        html = ""
        last_err = ""
        adapter = None
        for attempt in range(self._retry + 1):
            try:
                code, html, headers = self._get(site, "index.php")
                if code == 0:
                    last_err = "首页请求失败"
                else:
                    forced = "" if self._architecture == "auto" else self._architecture
                    adapter, score, how = detect_adapter(site, html, forced=forced)
                    rec["architecture"] = adapter.name
                    wait = adapter.parse_wait(html, headers)
                    if wait:
                        rec["wait"] = wait
                        self._sleep_for_site(domain, wait, "首页")
                    stats = adapter.parse_user_stats(html)
                    if not stats.get("logged_in"):
                        rec["status"] = "login"
                        rec["message"] = "登录失效，已停止该站"
                        log(rec["message"])
                        rec["fail"] += 1
                        return rec
                    rec["bonus"] = stats.get("bonus")
                    rec["upload"] = stats.get("upload")
                    rec["download"] = stats.get("download")
                    rec["ratio"] = stats.get("ratio")
                    log(
                        f"架构 {adapter.label}（{how}:{score:.2f}）· 当前魔力 {rec['bonus']} · "
                        f"上传 {format_size(rec['upload'])} · 下载 {format_size(rec['download'])} · 分享率 {rec['ratio']}"
                    )
                    break
            except Exception as e:
                last_err = str(e)
            if attempt < self._retry:
                time.sleep(1 + attempt)
        else:
            rec["status"] = "failed"
            rec["message"] = last_err or "首页请求失败"
            rec["fail"] += 1
            log(rec["message"])
            return rec

        if adapter is None:
            rec["status"] = "failed"
            rec["message"] = "未识别站点架构，禁止兑换"
            rec["fail"] += 1
            log(rec["message"])
            return rec

        catalog_html = ""
        catalog_path = adapter.catalog_path(site)
        for attempt in range(self._retry + 1):
            try:
                code, catalog_html, headers = self._get(site, catalog_path, adapter)
                if code == 0:
                    last_err = "魔力页请求失败"
                else:
                    wait = adapter.parse_wait(catalog_html, headers)
                    if wait:
                        rec["wait"] = wait
                        self._sleep_for_site(domain, wait, "魔力页")
                    stats2 = adapter.parse_user_stats(catalog_html)
                    if stats2.get("bonus") is not None:
                        rec["bonus"] = stats2["bonus"]
                    if not stats2.get("logged_in"):
                        rec["status"] = "login"
                        rec["message"] = "魔力页显示未登录，已停止该站"
                        rec["fail"] += 1
                        log(rec["message"])
                        return rec
                    break
            except Exception as e:
                last_err = str(e)
            if attempt < self._retry:
                time.sleep(1 + attempt)
        else:
            rec["status"] = "failed"
            rec["message"] = last_err or "魔力页请求失败"
            rec["fail"] += 1
            log(rec["message"])
            return rec

        base = site["url"].rstrip("/") + "/"
        items = adapter.parse_catalog(catalog_html, base, site)
        if not items:
            rec["status"] = "parse"
            rec["message"] = "解析不到兑换价格，禁止兑换"
            rec["fail"] += 1
            log(rec["message"])
            return rec

        up_item = pick_item(items, "upload", self._item_prefer) if self._enable_upload else None
        down_item = pick_item(items, "download", self._item_prefer) if self._enable_download else None
        if up_item:
            rec["item_upload"] = f"{up_item['size_text']} / {up_item['cost']} 魔力"
            log(f"上传兑换项：option={up_item.get('option')} 价格={up_item['cost']} 获得={up_item['size_text']}")
        if down_item:
            rec["item_download"] = f"{down_item['size_text']} / {down_item['cost']} 魔力"
            log(f"下载兑换项：option={down_item.get('option')} 价格={down_item['cost']} 获得={down_item['size_text']}")

        up_n, down_n, reason = plan_counts(
            bonus=rec["bonus"],
            ratio=rec["ratio"],
            upload_item=up_item,
            download_item=down_item,
            enable_upload=self._enable_upload,
            enable_download=self._enable_download,
            strategy=self._strategy,
            fixed_upload=self._fixed_upload,
            fixed_download=self._fixed_download,
            max_upload=self._max_upload,
            max_download=self._max_download,
            keep_bonus=self._keep_bonus,
            ratio_threshold=self._ratio_threshold,
            priority=self._priority,
            max_spend=self._max_spend,
        )
        rec["plan_upload"] = up_n
        rec["plan_download"] = down_n
        log(f"计划：上传 {up_n} 次 / 下载 {down_n} 次 · {reason}")
        if up_n <= 0 and down_n <= 0:
            rec["status"] = "skipped"
            rec["message"] = reason
            return rec

        queue: List[dict] = []
        if up_item:
            queue.extend([up_item] * up_n)
        if down_item:
            queue.extend([down_item] * down_n)

        spent_cap = self._max_spend if self._max_spend > 0 else None
        for item in queue:
            if rec["bonus"] is not None and rec["bonus"] - item["cost"] < self._keep_bonus:
                log("达到最低保留魔力，停止该站")
                rec["message"] = "达到最低保留魔力"
                break
            if spent_cap is not None and rec["spent"] + item["cost"] > spent_cap:
                log("达到单次任务消耗上限，停止该站")
                rec["message"] = "达到单次消耗上限"
                break

            ok, result = self._exchange_once(site, item, rec, adapter)
            rec["exchanges"] += 1
            rec["wait"] = result.get("wait_seconds")
            if result.get("code") == "login":
                rec["status"] = "login"
                rec["message"] = "登录失效，已停止该站"
                rec["fail"] += 1
                log(rec["message"])
                return rec
            if result.get("code") == "no_bonus":
                rec["status"] = "no_bonus"
                rec["message"] = "魔力值不足，已停止该站"
                rec["fail"] += 1
                log(rec["message"])
                return rec
            if ok:
                rec["success"] += 1
                rec["spent"] += item["cost"]
                if rec["bonus"] is not None:
                    rec["bonus"] -= item["cost"]
                if item["kind"] == "upload":
                    rec["got_upload"] += item["size_bytes"]
                else:
                    rec["got_download"] += item["size_bytes"]
                log(f"兑换成功 {item['kind']} {item['size_text']} 消耗 {item['cost']}")
            else:
                rec["fail"] += 1
                log(f"兑换失败：{result.get('message')}")
                if result.get("code") in ("parse", "unknown") and rec["fail"] >= 2:
                    rec["status"] = "failed"
                    rec["message"] = "连续失败，停止该站以免误兑换"
                    return rec
            self._sleep_for_site(domain, result.get("wait_seconds"), "兑换后")

        rec["status"] = "done" if rec["success"] else ("failed" if rec["fail"] else "skipped")
        rec["message"] = rec["message"] or f"实际兑换 {rec['success']} 次，失败 {rec['fail']} 次，消耗 {rec['spent']}"
        log(rec["message"])
        return rec

    def _exchange_once(self, site: dict, item: dict, rec: dict, adapter) -> Tuple[bool, dict]:
        last = {"success": False, "code": "http", "message": "请求失败", "wait_seconds": None}
        _method, url, data = adapter.build_exchange(site, item)
        for attempt in range(self._retry + 1):
            try:
                code, html, headers = self._post(site, url, data, adapter)
                wait = adapter.parse_wait(html, headers)
                result = adapter.classify_result(html, code)
                if wait is not None:
                    result["wait_seconds"] = wait
                last = result
                if result.get("code") == "rate_limit":
                    self._sleep_for_site(site["domain"], result.get("wait_seconds") or 5, "重试前")
                    continue
                if code == 0:
                    if attempt < self._retry:
                        time.sleep(1 + attempt)
                        continue
                return bool(result.get("success")), result
            except Exception as e:
                last = {"success": False, "code": "http", "message": str(e), "wait_seconds": None}
                if attempt < self._retry:
                    time.sleep(1 + attempt)
        return False, last

    def _summarize(self, started: str, results: List[dict]) -> dict:
        return {
            "time": started,
            "ok": any(r.get("status") in ("done", "skipped") for r in results),
            "message": "",
            "sites": results,
            "exchanges": sum(int(r.get("exchanges") or 0) for r in results),
            "spent": sum(float(r.get("spent") or 0) for r in results),
            "got_upload": sum(int(r.get("got_upload") or 0) for r in results),
            "got_download": sum(int(r.get("got_download") or 0) for r in results),
            "success": sum(int(r.get("success") or 0) for r in results),
            "fail": sum(int(r.get("fail") or 0) for r in results),
        }

    def _save_round(self, summary: dict):
        slim_sites = []
        for r in summary.get("sites") or []:
            slim_sites.append({k: r.get(k) for k in (
                "name", "domain", "status", "message", "bonus", "ratio",
                "plan_upload", "plan_download", "exchanges", "spent",
                "got_upload", "got_download", "success", "fail", "wait",
                "item_upload", "item_download", "architecture",
            )})
        record = {**summary, "sites": slim_sites}
        records = self.get_data("records") or []
        records.append(record)
        self.save_data("records", records[-30:])
        self.save_data("last", record)

        totals = self.get_data("totals") or {
            "runs": 0, "exchanges": 0, "spent": 0, "got_upload": 0, "got_download": 0, "success": 0, "fail": 0,
        }
        totals["runs"] = int(totals.get("runs") or 0) + 1
        for key in ("exchanges", "spent", "got_upload", "got_download", "success", "fail"):
            totals[key] = (totals.get(key) or 0) + (summary.get(key) or 0)
        self.save_data("totals", totals)

    def _load_records(self) -> list:
        return self.get_data("records") or []

    def _notify_summary(self, summary: dict, manual: bool):
        if not self._notify:
            return
        lines = [
            f"{'手动' if manual else '定时'}任务完成",
            f"站点 {len(summary.get('sites') or [])} · 成功 {summary.get('success', 0)} · 失败 {summary.get('fail', 0)}",
            f"消耗魔力 {summary.get('spent', 0)}",
            f"获得上传 {format_size(summary.get('got_upload'))} · 下载 {format_size(summary.get('got_download'))}",
        ]
        for r in (summary.get("sites") or [])[:8]:
            lines.append(f"{r.get('name')}: {r.get('status')} {r.get('message')}")
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="魔力兑换",
                text="\n".join(lines),
            )
        except Exception as e:
            logger.error(f"发送通知失败：{e}")

    # ======================== 页面 ========================

    def get_page(self) -> List[dict]:
        last = self.get_data("last") or {}
        totals = self.get_data("totals") or {}
        records = self._load_records()
        sites = last.get("sites") or []

        def kpi_card(icon: str, label: str, value: str, value_color: str = "", note: str = "") -> dict:
            value_cls = f"text-h6 font-weight-bold text-{value_color}" if value_color else "text-h6 font-weight-bold"
            children = [
                {"component": "div", "props": {"class": "text-h5 mb-1"}, "text": icon},
                {"component": "span", "props": {
                    "class": value_cls,
                    "style": "display:block; font-size: clamp(0.8rem, 3.8vw, 1.25rem); white-space: normal; overflow-wrap: anywhere; word-break: break-all; line-height: 1.2;",
                }, "text": value},
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis d-block mt-1"}, "text": label},
            ]
            if note:
                children.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis", "style": "white-space: normal; overflow-wrap: anywhere;"}, "text": note})
            return {
                "component": "VCard",
                "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                "content": [{"component": "VCardText", "props": {"class": "text-center pa-3"}, "content": children}],
            }

        def run_tr(cells: List[tuple], head: bool = False) -> dict:
            return {
                "component": "tr",
                "content": [
                    {"component": "th" if head else "td", "props": {"class": cls}, "text": text}
                    for text, cls in cells
                ],
            }

        last_spent = last.get("spent") or 0
        last_up = format_size(last.get("got_upload"))
        last_down = format_size(last.get("got_download"))
        total_spent = totals.get("spent") or 0

        site_table = {
            "component": "VTable",
            "props": {"hover": True, "density": "compact", "class": "run-records-table", "style": "min-width: 720px;"},
            "content": [
                {"component": "thead", "content": [run_tr([
                    ("站点", "text-body-2 text-start ps-3 text-no-wrap"),
                    ("状态", "text-body-2 text-center text-no-wrap"),
                    ("魔力", "text-body-2 text-center text-no-wrap"),
                    ("计划", "text-body-2 text-center text-no-wrap"),
                    ("实际", "text-body-2 text-center text-no-wrap"),
                    ("消耗", "text-body-2 text-center text-no-wrap"),
                    ("获得", "text-body-2 text-start text-no-wrap"),
                    ("说明", "text-body-2 text-start"),
                ], head=True)]},
                {"component": "tbody", "content": []},
            ],
        }
        for r in sites:
            status = r.get("status") or "—"
            color = {
                "done": "text-success",
                "skipped": "text-info",
                "login": "text-error",
                "failed": "text-error",
                "parse": "text-error",
                "no_bonus": "text-warning",
            }.get(status, "")
            got = []
            if r.get("got_upload"):
                got.append(f"↑{format_size(r.get('got_upload'))}")
            if r.get("got_download"):
                got.append(f"↓{format_size(r.get('got_download'))}")
            site_table["content"][1]["content"].append(run_tr([
                (str(r.get("name") or "—"), "text-body-2 text-start ps-3 text-no-wrap font-weight-bold"),
                (str(status), f"text-body-2 font-weight-bold text-center text-no-wrap {color}"),
                (str(r.get("bonus") if r.get("bonus") is not None else "—"), "text-body-2 text-center text-no-wrap"),
                (f"↑{r.get('plan_upload', 0)} ↓{r.get('plan_download', 0)}", "text-body-2 text-center text-no-wrap"),
                (f"{r.get('success', 0)}/{r.get('exchanges', 0)}", "text-body-2 text-center text-no-wrap"),
                (str(r.get("spent") or 0), "text-body-2 text-center text-no-wrap"),
                (" ".join(got) or "—", "text-body-2 text-start text-no-wrap"),
                (str(r.get("message") or "—"), "text-body-2 text-start"),
            ]))

        hist_table = {
            "component": "VTable",
            "props": {"hover": True, "density": "compact", "class": "run-records-table", "style": "min-width: 640px;"},
            "content": [
                {"component": "thead", "content": [run_tr([
                    ("时间", "text-body-2 text-start ps-3 text-no-wrap"),
                    ("成功", "text-body-2 text-center text-no-wrap"),
                    ("失败", "text-body-2 text-center text-no-wrap"),
                    ("消耗魔力", "text-body-2 text-center text-no-wrap"),
                    ("获得上传", "text-body-2 text-center text-no-wrap"),
                    ("获得下载", "text-body-2 text-center text-no-wrap"),
                ], head=True)]},
                {"component": "tbody", "content": []},
            ],
        }
        for r in reversed(records[-12:]):
            hist_table["content"][1]["content"].append(run_tr([
                (str(r.get("time") or "—"), "text-body-2 text-start ps-3 text-no-wrap"),
                (str(r.get("success") or 0), "text-body-2 text-center text-no-wrap text-success"),
                (str(r.get("fail") or 0), "text-body-2 text-center text-no-wrap text-error"),
                (str(r.get("spent") or 0), "text-body-2 text-center text-no-wrap"),
                (format_size(r.get("got_upload")), "text-body-2 text-center text-no-wrap"),
                (format_size(r.get("got_download")), "text-body-2 text-center text-no-wrap"),
            ]))

        return [
            {
                "component": "VRow",
                "content": [{
                    "component": "VCol", "props": {"cols": 12}, "content": [{
                        "component": "VCard", "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                        "content": [
                            {"component": "VCardTitle", "text": "💎 兑换概况"},
                            {"component": "VCardSubtitle", "text": last.get("time") or "尚未执行"},
                            {"component": "VCardText", "props": {"class": "pa-2"}, "content": [
                                {"component": "VRow", "props": {"dense": True}, "content": [
                                    {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                        kpi_card("🔁", "累计次数", f"{int(totals.get('exchanges') or 0)}", "info", f"任务 {int(totals.get('runs') or 0)} 轮"),
                                    ]},
                                    {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                        kpi_card("💎", "累计消耗", f"{total_spent}", "warning", f"最近 {last_spent}"),
                                    ]},
                                    {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                        kpi_card("⬆️", "累计上传", format_size(totals.get("got_upload")), "success", f"最近 {last_up}"),
                                    ]},
                                    {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                                        kpi_card("⬇️", "累计下载", format_size(totals.get("got_download")), "error", f"最近 {last_down}"),
                                    ]},
                                ]},
                            ]},
                        ],
                    }],
                }],
            },
            {
                "component": "VRow",
                "content": [{
                    "component": "VCol", "props": {"cols": 12}, "content": [{
                        "component": "VCard", "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                        "content": [
                            {"component": "VCardTitle", "text": "📡 最近一轮 · 各站点"},
                            {"component": "VCardSubtitle", "text": "同一站点串行，多站点并发；解析不到价格不会兑换"},
                            {"component": "VCardText", "props": {"class": "pa-2", "style": "overflow-x:auto;"}, "content": [site_table if sites else {"component": "div", "props": {"class": "text-medium-emphasis pa-3"}, "text": "还没有执行记录"}]},
                        ],
                    }],
                }],
            },
            {
                "component": "VRow",
                "content": [{
                    "component": "VCol", "props": {"cols": 12}, "content": [{
                        "component": "VCard", "props": {"variant": "flat", "elevation": 2, "class": "h-100 rounded-lg"},
                        "content": [
                            {"component": "VCardTitle", "text": "📋 任务记录"},
                            {"component": "VCardSubtitle", "text": "最近 12 轮"},
                            {"component": "VCardText", "props": {"class": "pa-2", "style": "overflow-x:auto;"}, "content": [hist_table if records else {"component": "div", "props": {"class": "text-medium-emphasis pa-3"}, "text": "暂无记录"}]},
                        ],
                    }],
                }],
            },
        ]
