"""
插件残留清理（PluginResidueClean）

一键清理卸载后的插件残留（目录 / 数据 / 配置 / 模块缓存 / 文件夹配置），
支持手动扫描与定时扫描通知；定时扫描只发通知，绝不自动清理。

- 打开插件页面即自动扫描
- 「一键清理残留」一次性清掉所有已卸载插件的残留，排除自身与排除列表
- 定时扫描发现残留只通知，清理始终由用户手动触发
"""

import shutil
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.plugindata_oper import PluginDataOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import EventType, NotificationType, SystemConfigKey
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase


class PluginResidueClean(_PluginBase):
    """
    插件残留清理：扫描并一键清理卸载后的插件残留
    """

    # 插件元信息
    plugin_name = "插件残留清理"
    plugin_desc = "一键清理卸载后的插件残留（目录/数据/配置/模块缓存），支持定时扫描通知，不自动清理"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/pluginresidueclean.png"
    plugin_version = "1.0.1"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "pluginresidueclean_"
    plugin_order = 0
    auth_level = 2

    # 自身 ID（清理时自动排除）
    SELF_PID = "pluginresidueclean"

    # 卸载残留（可清理）
    STATUS_LEFT = "leftover"
    # 已安装但有残留（只展示，不清理）
    STATUS_INSTALLED = "installed_leftover"

    def __init__(self):
        super().__init__()
        self._scheduled_scan = False
        self._cron = "0 3 * * *"
        self._notify = True
        self._exclude_pids = ""
        self._progress = {"running": False, "done": 0, "total": 0, "current": "", "message": "尚未开始", "started": "", "finished": ""}
        self._progress_lock = threading.Lock()
        self._clean_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置
        """
        if config:
            self._scheduled_scan = config.get("scheduled_scan", False)
            self._cron = config.get("cron") or "0 3 * * *"
            self._notify = config.get("notify", True)
            self._exclude_pids = (config.get("exclude_pids") or "").strip()

    def get_state(self) -> bool:
        """仅在开启定时扫描时视为运行中；页面手动扫描/清理无需常驻运行。"""
        return self._scheduled_scan

    def get_command(self) -> List[Dict[str, Any]]:
        """注册手动扫描命令。"""
        return [
            {
                "cmd": "/pluginresidueclean",
                "event": EventType.PluginAction,
                "desc": "插件残留清理：扫描插件残留并汇报",
                "category": "系统",
                "data": {
                    "action": "pluginresidueclean_scan"
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册 API 路由（MoviePilot 自动拼接 /api/v1/plugin/PluginResidueClean 前缀）"""
        return [
            {
                "path": "/scan",
                "summary": "扫描插件残留",
                "description": "扫描卸载后仍有残留的插件（目录/数据/配置/模块缓存/文件夹配置）",
                "endpoint": self.api_scan,
                "methods": ["POST"],
            },
            {
                "path": "/clean_one",
                "summary": "清理单个插件残留",
                "description": "仅清理指定的已卸载插件残留，自动保护自身、已安装插件和排除列表",
                "endpoint": self.api_clean_one,
                "methods": ["POST"],
            },
            {
                "path": "/progress",
                "summary": "获取清理进度",
                "description": "获取当前清理任务进度",
                "endpoint": self.api_progress,
                "methods": ["GET"],
            },
            {
                "path": "/clean",
                "summary": "一键清理插件残留",
                "description": "清理所有已卸载但仍有残留的插件，自动排除自身与排除列表",
                "endpoint": self.api_clean,
                "methods": ["POST"],
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单"""
        from .config_form import build_form
        return build_form()

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册定时扫描服务：发现残留只通知，绝不调用清理。
        """
        if self._scheduled_scan and self._cron:
            return [
                {
                    "id": "pluginresidueclean_scan",
                    "name": "插件残留定时扫描",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._scheduled_scan_job,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """停止插件服务；定时服务由 MoviePilot 调度器统一移除。"""
        pass

    # ======================== 定时扫描 ========================

    def _scheduled_scan_job(self):
        """定时扫描：发现卸载残留只通知，不执行清理。"""
        try:
            scan = self._scan()
        except Exception as e:
            logger.error(f"插件残留清理定时扫描异常：{e}")
            return
        leftovers = scan.get("leftovers", [])
        if leftovers:
            logger.warning("插件残留清理定时扫描发现 %d 个卸载残留，未自动清理", len(leftovers))
            if self._notify:
                self._notify_scan(leftovers)
        else:
            logger.info("插件残留清理定时扫描：未发现卸载残留")

    # ======================== 扫描 ========================

    def _get_exclude(self) -> set:
        """解析排除列表（自身始终排除）"""
        exclude = set()
        for p in (self._exclude_pids or "").split(","):
            p = p.strip().lower()
            if p:
                exclude.add(p)
        exclude.add(self.SELF_PID)
        return exclude

    def _scan(self) -> Dict[str, Any]:
        """
        扫描卸载后的插件残留。

        候选范围：插件目录名 ∪ 已安装列表 ∪ 文件夹配置中的插件名。
        卸载残留（不在已安装列表但有残留）→ 可清理；
        已安装但有残留（如目录缺失）→ 只展示提示，不清理。
        """
        installed = set(str(p).lower() for p in (SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []))
        folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
        plugins_dir = settings.ROOT_PATH / "app" / "plugins"
        exclude = self._get_exclude()

        # 候选插件名
        candidates = set()
        if plugins_dir.exists():
            for d in plugins_dir.iterdir():
                if d.is_dir() and not d.name.startswith((".", "_")):
                    candidates.add(d.name)
        candidates |= installed
        for fdata in folders.values():
            if isinstance(fdata, dict):
                for p in fdata.get("plugins", []) or []:
                    if isinstance(p, str):
                        candidates.add(p)

        items = []
        for pid in sorted(candidates, key=str.lower):
            lp = pid.lower()
            if lp in exclude:
                continue
            leftovers = []
            risk = "low"
            risk_score = 0
            # 目录残留
            if (plugins_dir / pid).exists() or (plugins_dir / lp).exists():
                leftovers.append("目录")
            # 数据残留
            try:
                if PluginDataOper().get_data(lp):
                    leftovers.append("数据")
            except Exception:
                pass
            # 配置残留
            try:
                if SystemConfigOper().get(f"plugin.{lp}"):
                    leftovers.append("配置")
            except Exception:
                pass
            # 模块缓存残留
            if f"app.plugins.{lp}" in sys.modules:
                leftovers.append("模块缓存")
            # 文件夹配置残留
            if any(
                lp in (fdata.get("plugins", []) or [])
                for fdata in folders.values()
                if isinstance(fdata, dict)
            ):
                leftovers.append("文件夹配置")

            if not leftovers:
                continue
            risk_score = sum({"目录": 3, "数据": 2, "配置": 2, "模块缓存": 1, "文件夹配置": 1}.get(x, 1) for x in leftovers)
            risk = "high" if risk_score >= 3 else ("medium" if risk_score >= 2 else "low")
            status = self.STATUS_INSTALLED if lp in installed else self.STATUS_LEFT
            items.append({
                "pid": pid,
                "status": status,
                "risk": risk,
                "risk_score": risk_score,
                "leftovers": leftovers,
            })

        cleanable = [it for it in items if it["status"] == self.STATUS_LEFT]
        return {
            "items": items,
            "leftovers": cleanable,
            "count": len(cleanable),
        }

    # ======================== 清理 ========================

    def _clean(self) -> dict:
        """
        一键清理所有卸载残留（排除自身与排除列表）。
        """
        logger.info("插件残留清理：开始执行清理任务")
        if not self._clean_lock.acquire(blocking=False):
            logger.warning("插件残留清理：已有清理任务正在运行，拒绝重复启动")
            return {"code": 0, "success": False, "message": "已有清理任务正在运行", "data": []}
        try:
            return self._clean_locked()
        finally:
            self._clean_lock.release()

    def _clean_locked(self, targets: Optional[List[dict]] = None) -> dict:
        """实际清理实现；由调用方持有清理锁。"""
        try:
            installed = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
            folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
            plugins_dir = settings.ROOT_PATH / "app" / "plugins"
        except Exception as e:
            logger.error(f"插件残留清理初始化失败：{e}")
            return {"code": 0, "success": False, "message": f"初始化失败：{e}", "data": []}

        pm = PluginManager()
        targets = targets if targets is not None else [it for it in self._scan().get("leftovers", []) if it["pid"]]
        total = len(targets)
        self._set_progress(True, 0, total, "准备开始", "")
        logger.info("插件残留清理：共发现 %d 个可清理残留", total)
        results = []
        for index, it in enumerate(targets, 1):
            pid = it["pid"]
            lp = pid.lower()
            logger.info("插件残留清理：开始处理 [%d/%d] %s，残留项=%s", index, total, pid, "、".join(it.get("leftovers", [])))
            self._set_progress(True, index - 1, total, f"正在处理 {pid}", pid)
            steps = []
            ok = True
            try:
                # 1. 删除目录
                for d in (plugins_dir / pid, plugins_dir / lp):
                    if d.exists():
                        logger.info("插件残留清理：[%s] 删除目录 %s", pid, d)
                        shutil.rmtree(d, ignore_errors=True)
                        steps.append("目录")
                # 2. 删除数据
                try:
                    PluginDataOper().del_data(lp)
                    logger.info("插件残留清理：[%s] 删除插件数据", pid)
                    steps.append("数据")
                except Exception:
                    pass
                # 3. 删除配置
                try:
                    SystemConfigOper().delete(f"plugin.{lp}")
                    logger.info("插件残留清理：[%s] 删除插件配置 plugin.%s", pid, lp)
                    steps.append("配置")
                except Exception:
                    pass
                # 4. 清除模块缓存
                try:
                    pm._clear_plugin_modules(lp)
                    logger.info("插件残留清理：[%s] 清除模块缓存 app.plugins.%s", pid, lp)
                    steps.append("模块缓存")
                except Exception:
                    pass
                # 5. 移出文件夹配置
                changed = False
                for fdata in folders.values():
                    if isinstance(fdata, dict) and lp in (fdata.get("plugins", []) or []):
                        fdata["plugins"].remove(lp)
                        changed = True
                if changed:
                    SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
                    logger.info("插件残留清理：[%s] 移除文件夹配置引用", pid)
                    steps.append("文件夹配置")
            except Exception as e:
                ok = False
                steps.append(f"失败:{str(e)[:100]}")
                logger.error(f"插件残留清理 {pid} 失败：{e}")

            results.append({"pid": pid, "success": ok, "steps": steps})
            logger.info("插件残留清理：完成 [%d/%d] %s，结果=%s，步骤=%s", index, total, pid, "成功" if ok else "失败", "、".join(steps) or "无")
            self._set_progress(True, index, total, f"已完成 {pid}", pid)

        # 保存文件夹配置变更
        try:
            SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        except Exception as e:
            logger.error(f"保存文件夹配置失败：{e}")

        cleaned = sum(1 for r in results if r["success"])
        failed = len(results) - cleaned
        post_scan = self._scan()
        remaining = len(post_scan.get("leftovers", []))
        message = f"清理完成：成功 {cleaned} 个，失败 {failed} 个，复扫后仍有 {remaining} 个残留"
        logger.info(f"插件残留清理：{message}")
        self._set_progress(False, total, total, message, "")

        if self._notify and results:
            self._notify_clean(results, cleaned, failed)

        return {"code": 1 if failed == 0 else 0, "success": failed == 0, "message": message, "data": results}

    # ======================== 通知 ========================

    def api_progress(self) -> dict:
        """API：获取清理进度"""
        with self._progress_lock:
            progress = dict(self._progress)
        total = progress.get("total", 0)
        done = progress.get("done", 0)
        progress["percent"] = int(done * 100 / total) if total else (100 if not progress.get("running") else 0)
        logger.debug("插件残留清理：查询进度 %s", progress)
        return {"success": True, "code": 1, "message": progress.get("message", ""), "data": progress}

    def _set_progress(self, running: bool, done: int, total: int, message: str, current: str):
        """更新内存进度，供页面轮询。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._progress_lock:
            self._progress.update({"running": running, "done": done, "total": total, "current": current, "message": message})
            if running and not self._progress.get("started"):
                self._progress["started"] = now
            if not running:
                self._progress["finished"] = now

    def _notify_scan(self, leftovers: List[dict]):
        """定时扫描发现残留通知（不自动清理）"""
        lines = [f"发现 {len(leftovers)} 个插件残留（仅通知，未自动清理）："]
        for it in leftovers[:20]:
            lines.append(f"• {it['pid']}：{'、'.join(it['leftovers'])}")
        if len(leftovers) > 20:
            lines.append(f"… 其余 {len(leftovers) - 20} 个略")
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="🧹 插件残留清理",
                text="\n".join(lines),
            )
        except Exception as e:
            logger.error(f"插件残留清理通知发送失败：{e}")

    def _notify_clean(self, results: List[dict], cleaned: int, failed: int):
        """清理完成通知"""
        lines = [f"共处理 {len(results)} 个插件残留："]
        for r in results:
            mark = "✅" if r["success"] else "❌"
            lines.append(f"{mark} {r['pid']}：{'、'.join(r['steps']) or '无操作'}")
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"🧹 插件残留清理：成功 {cleaned} / 失败 {failed}",
                text="\n".join(lines),
            )
        except Exception as e:
            logger.error(f"插件残留清理通知发送失败：{e}")

    # ======================== API ========================

    def api_scan(self) -> dict:
        """API：扫描插件残留"""
        logger.info("插件残留清理：收到扫描请求")
        try:
            scan = self._scan()
            self._save_last({"action": "扫描", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "message": f"发现 {scan['count']} 个卸载残留"})
            return {
                "success": True,
                "code": 1,
                "message": f"扫描完成：发现 {scan['count']} 个卸载残留",
                "data": scan,
            }
        except Exception as e:
            logger.error(f"插件残留清理扫描失败：{e}")
            return {"success": False, "code": 0, "message": f"扫描失败：{e}", "data": {}}

    def api_clean_one(self, pid: str = "", data: Optional[Dict[str, Any]] = Body(default=None)) -> dict:
        """API：清理单个已卸载插件；PID 必须来自当前扫描结果。"""
        body = data if isinstance(data, dict) else {}
        target = (pid or body.get("pid") or "").strip()
        if not target:
            return {"success": False, "code": 0, "message": "缺少插件 ID", "data": {}}
        scan = self._scan()
        item = next((it for it in scan.get("leftovers", []) if it.get("pid", "").lower() == target.lower()), None)
        if not item:
            return {"success": False, "code": 0, "message": "目标不存在、已安装或受到保护", "data": {"pid": target}}
        if self._clean_lock.locked():
            return {"success": False, "code": 0, "message": "已有清理任务正在运行", "data": {}}
        # 复用统一清理逻辑的筛选入口，避免单项清理绕过安全判断。
        threading.Thread(target=self._clean_targets, args=([item],), name="pluginresidueclean-one", daemon=True).start()
        return {"success": True, "code": 1, "message": f"已启动 {target} 的清理任务", "data": item}

    def _clean_targets(self, targets: List[dict]) -> dict:
        """清理指定目标并复扫；单项入口与批量入口共用锁。"""
        if not self._clean_lock.acquire(blocking=False):
            return {"success": False, "code": 0, "message": "已有清理任务正在运行", "data": []}
        try:
            # 仅将目标传入通用批量清理实现的轻量包装，当前方法不接受外部任意路径。
            return self._clean_locked(targets=targets)
        finally:
            self._clean_lock.release()

    def api_clean(self, scope: str = "all", data: Optional[Dict[str, Any]] = Body(default=None)) -> dict:
        """
        API：一键清理卸载残留。

        MoviePilot 页面按钮把 ``params`` 作为 POST body 发送，同时兼容
        URL 查询参数；``scope`` 从查询参数注入，``data`` 为请求体。
        清理目标始终是「已卸载但有残留」的插件，不受请求参数影响。
        """
        logger.info("插件残留清理：收到清理请求 scope=%s", scope)
        if self._clean_lock.locked():
            logger.warning("插件残留清理：已有任务运行中，拒绝重复启动")
            return {"success": False, "code": 0, "message": "已有清理任务正在运行", "data": {}}
        threading.Thread(target=self._clean, name="pluginresidueclean", daemon=True).start()
        logger.info("插件残留清理：已启动后台清理线程，页面可通过进度按钮刷新")
        return {"success": True, "code": 1, "message": "清理任务已启动，请点击‘刷新进度’查看", "data": self.api_progress().get("data", {})}

    def _save_last(self, info: dict):
        """保存最近一次操作结果，页面展示用"""
        try:
            data = self.get_data("pluginresidueclean_state") or {}
            data["last"] = info
            self.save_data("pluginresidueclean_state", data)
        except Exception:
            pass

    # ======================== 页面 ========================

    def get_page(self) -> List[dict]:
        """
        页面：扫描概览 + 一键清理。
        打开页面即自动扫描；点击按钮后 MoviePilot 前端会自动重新加载页面，
        展示清理后的最新状态。
        """
        try:
            scan = self._scan()
        except Exception as e:
            logger.error(f"插件残留清理页面扫描失败：{e}")
            scan = {"items": [], "leftovers": [], "count": 0}

        items = scan.get("items", [])
        leftovers = scan.get("leftovers", [])
        count = len(leftovers)
        progress = self.api_progress().get("data", {})
        progress_percent = int(progress.get("percent", 0))
        progress_text = progress.get("message", "尚未开始")
        progress_current = progress.get("current", "")

        api_base = f"plugin/{self.__class__.__name__}?apikey={settings.API_TOKEN}"
        scan_api = f"plugin/{self.__class__.__name__}/scan?apikey={settings.API_TOKEN}"
        progress_api = f"plugin/{self.__class__.__name__}/progress?apikey={settings.API_TOKEN}"
        clean_api = f"plugin/{self.__class__.__name__}/clean?apikey={settings.API_TOKEN}&scope=all"

        clean_one_api = lambda pid: f"plugin/{self.__class__.__name__}/clean_one?apikey={settings.API_TOKEN}&pid={pid}"

        def risk_view(it: dict) -> tuple[str, str]:
            return {"high": ("高", "text-error"), "medium": ("中", "text-warning"), "low": ("低", "text-info")}.get(it.get("risk", "low"), ("低", "text-info"))

        def cell(text: Any, cls: str) -> dict:
            return {"component": "td", "props": {"class": cls}, "text": str(text or "—")}

        # 残留列表
        table = {
            "component": "VTable",
            "props": {"hover": True, "density": "compact", "class": "residue-table"},
            "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "props": {"class": "text-body-2 text-start ps-3 text-no-wrap"}, "text": "插件"},
                    {"component": "th", "props": {"class": "text-body-2 text-start"}, "text": "残留项"},
                    {"component": "th", "props": {"class": "text-body-2 text-center text-no-wrap"}, "text": "风险"},
                    {"component": "th", "props": {"class": "text-body-2 text-center text-no-wrap"}, "text": "状态"},
                    {"component": "th", "props": {"class": "text-body-2 text-center text-no-wrap"}, "text": "操作"},
                ]}]},
                {"component": "tbody", "content": []},
            ],
        }

        for it in items:
            risk_text, risk_cls = risk_view(it)
            status_text, status_cls = (("可清理", "text-success") if it["status"] == self.STATUS_LEFT else ("已安装，仅展示", "text-medium-emphasis"))
            action = {"component": "VBtn", "props": {"size": "x-small", "variant": "text", "color": "error", "disabled": it["status"] != self.STATUS_LEFT}, "text": "清理", "events": {"click": {"api": clean_one_api(it["pid"]), "method": "post", "params": {"pid": it["pid"]}}}}
            table["content"][1]["content"].append({"component": "tr", "content": [
                cell(it["pid"], "text-body-2 text-start ps-3 text-no-wrap font-weight-bold"),
                cell("、".join(it["leftovers"]), "text-body-2 text-start text-medium-emphasis"),
                cell(risk_text, f"text-body-2 font-weight-bold text-center text-no-wrap {risk_cls}"),
                cell(status_text, f"text-body-2 font-weight-bold text-center text-no-wrap {status_cls}"),
                {"component": "td", "props": {"class": "text-center"}, "content": [action]},
            ]})

        page = [
            # 操作卡片
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard", "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "d-flex align-center justify-space-between flex-wrap ga-2"},
                                        "content": [
                                            {"component": "span", "text": f"🧹 卸载残留一键清理（当前 {count} 项）"},
                                            {
                                                "component": "div",
                                                "props": {"class": "d-flex ga-2"},
                                                "content": [
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "primary",
                                                            "size": "small",
                                                            "variant": "tonal",
                                                            "prepend-icon": "mdi-magnify",
                                                        },
                                                        "text": "立即扫描",
                                                        "events": {"click": {"api": scan_api, "method": "post"}},
                                                    },
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "info",
                                                            "size": "small",
                                                            "variant": "tonal",
                                                            "prepend-icon": "mdi-progress-clock",
                                                        },
                                                        "text": "刷新进度",
                                                        "events": {"click": {"api": progress_api, "method": "get"}},
                                                    },
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "error",
                                                            "size": "small",
                                                            "variant": "tonal",
                                                            "prepend-icon": "mdi-delete-sweep-outline",
                                                            "disabled": not bool(count),
                                                        },
                                                        "text": "一键清理残留",
                                                        "events": {"click": {"api": clean_api, "method": "post"}},
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "component": "VCardSubtitle",
                                        "props": {"class": "px-4 pb-2"},
                                        "text": "清理对象为已卸载但仍有残留的插件（目录/数据/配置/模块缓存/文件夹配置）。插件自身与排除列表始终受保护；定时扫描只通知，不自动清理。",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "px-4 pt-0 pb-2"},
                                        "content": [
                                            {"component": "div", "props": {"class": "d-flex justify-space-between text-caption mb-1"}, "content": [
                                                {"component": "span", "text": f"{progress_text}{('：' + progress_current) if progress_current else ''}"},
                                                {"component": "span", "text": f"{progress_percent}%（{progress.get('done', 0)}/{progress.get('total', 0)}）"},
                                            ]},
                                            {"component": "VProgressLinear", "props": {"model-value": progress_percent, "color": "info" if progress.get("running") else "success", "height": 8, "rounded": True}},
                                        ],
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "pa-2"},
                                        "content": [table] if items else [
                                            {
                                                "component": "div",
                                                "props": {"class": "text-body-2 text-medium-emphasis pa-4"},
                                                "text": "🎉 未发现卸载残留，一切干净",
                                            }
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ]
        return page
