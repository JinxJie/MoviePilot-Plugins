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
    plugin_version = "1.0.0"
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
            status = self.STATUS_INSTALLED if lp in installed else self.STATUS_LEFT
            items.append({
                "pid": pid,
                "status": status,
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
        try:
            installed = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
            folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
            plugins_dir = settings.ROOT_PATH / "app" / "plugins"
        except Exception as e:
            logger.error(f"插件残留清理初始化失败：{e}")
            return {"code": 0, "success": False, "message": f"初始化失败：{e}", "data": []}

        pm = PluginManager()
        targets = [it for it in self._scan().get("leftovers", []) if it["pid"]]
        results = []
        for it in targets:
            pid = it["pid"]
            lp = pid.lower()
            steps = []
            ok = True
            try:
                # 1. 删除目录
                for d in (plugins_dir / pid, plugins_dir / lp):
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)
                        steps.append("目录")
                # 2. 删除数据
                try:
                    PluginDataOper().del_data(lp)
                    steps.append("数据")
                except Exception:
                    pass
                # 3. 删除配置
                try:
                    SystemConfigOper().delete(f"plugin.{lp}")
                    steps.append("配置")
                except Exception:
                    pass
                # 4. 清除模块缓存
                try:
                    pm._clear_plugin_modules(lp)
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
                    steps.append("文件夹配置")
            except Exception as e:
                ok = False
                steps.append(f"失败:{str(e)[:100]}")
                logger.error(f"插件残留清理 {pid} 失败：{e}")

            results.append({"pid": pid, "success": ok, "steps": steps})

        # 保存文件夹配置变更
        try:
            SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        except Exception as e:
            logger.error(f"保存文件夹配置失败：{e}")

        cleaned = sum(1 for r in results if r["success"])
        failed = len(results) - cleaned
        message = f"清理完成：成功 {cleaned} 个，失败 {failed} 个"
        logger.info(f"插件残留清理：{message}")

        if self._notify and results:
            self._notify_clean(results, cleaned, failed)

        return {"code": 1 if failed == 0 else 0, "success": failed == 0, "message": message, "data": results}

    # ======================== 通知 ========================

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

    def api_clean(self, scope: str = "all", data: Optional[Dict[str, Any]] = Body(default=None)) -> dict:
        """
        API：一键清理卸载残留。

        MoviePilot 页面按钮把 ``params`` 作为 POST body 发送，同时兼容
        URL 查询参数；``scope`` 从查询参数注入，``data`` 为请求体。
        清理目标始终是「已卸载但有残留」的插件，不受请求参数影响。
        """
        logger.info("插件残留清理：收到清理请求 scope=%s", scope)
        try:
            return self._clean()
        except Exception as e:
            logger.error(f"插件残留清理执行失败：{e}")
            return {"success": False, "code": 0, "message": f"清理失败：{e}", "data": []}

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

        api_base = f"plugin/{self.__class__.__name__}?apikey={settings.API_TOKEN}"
        scan_api = f"plugin/{self.__class__.__name__}/scan?apikey={settings.API_TOKEN}"
        clean_api = f"plugin/{self.__class__.__name__}/clean?apikey={settings.API_TOKEN}&scope=all"

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
                    {"component": "th", "props": {"class": "text-body-2 text-center text-no-wrap"}, "text": "状态"},
                ]}]},
                {"component": "tbody", "content": []},
            ],
        }

        for it in items:
            if it["status"] == self.STATUS_LEFT:
                status = ("✅ 可清理", "text-success")
            else:
                status = ("ℹ️ 已安装", "text-medium-emphasis")
            table["content"][1]["content"].append({"component": "tr", "content": [
                cell(it["pid"], "text-body-2 text-start ps-3 text-no-wrap font-weight-bold"),
                cell("、".join(it["leftovers"]), "text-body-2 text-start text-medium-emphasis"),
                cell(status[0], f"text-body-2 font-weight-bold text-center text-no-wrap {status[1]}"),
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
