"""
插件诊所 - MoviePilot V2 插件

功能：
- 打开插件页面自动扫描所有插件状态
- 定位无法加载的插件（缺依赖 / 代码错误 / 目录缺失）与卸载残留（目录 / 数据 / 配置 / 模块缓存）
- 逐项确认清理，一键清理干净，让重新安装一路顺畅
- 清理记录留痕，可回溯
- 可选定时扫描：发现问题只发送通知，不自动清理

说明：
- 打开插件页面会自动扫描
- 定时任务只负责扫描和告警，清理始终由用户手动确认
- 自动排除插件自身与排除列表中的插件
"""

import importlib
import shutil
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.plugindata_oper import PluginDataOper
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas.types import EventType, NotificationType, SystemConfigKey
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase


class PluginClinic(_PluginBase):
    """
    插件诊所：扫描并清理无法加载的插件与卸载残留
    """

    # 插件元信息
    plugin_name = "插件诊所"
    plugin_desc = "扫描无法加载的插件与卸载残留，逐项确认或一键清理，让重新安装一路顺畅"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/pluginclinic.png"
    plugin_version = "1.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "pluginclinic_"
    plugin_order = 0
    auth_level = 2

    # 自身 ID（清理时自动排除）
    SELF_PID = "pluginclinic"

    # 状态定义
    STATUS_OK = "ok"                    # 正常
    STATUS_LOAD_FAILED = "load_failed"  # 加载失败
    STATUS_GHOST = "ghost"              # 已安装但目录缺失（记录残留）
    STATUS_DIR_LEFT = "dir_leftover"    # 目录残留
    STATUS_DATA_LEFT = "data_leftover"  # 数据/配置/模块残留

    STATUS_LABEL = {
        STATUS_OK: "✅ 正常",
        STATUS_LOAD_FAILED: "❌ 加载失败",
        STATUS_GHOST: "🕳️ 记录残留",
        STATUS_DIR_LEFT: "🗑️ 目录残留",
        STATUS_DATA_LEFT: "💾 数据残留",
    }

    STATUS_COLOR = {
        STATUS_OK: "success",
        STATUS_LOAD_FAILED: "error",
        STATUS_GHOST: "warning",
        STATUS_DIR_LEFT: "info",
        STATUS_DATA_LEFT: "info",
    }

    # 需要清理的异常状态（一键清理范围）
    ABNORMAL_STATUSES = (STATUS_LOAD_FAILED, STATUS_GHOST, STATUS_DIR_LEFT, STATUS_DATA_LEFT)

    def __init__(self):
        super().__init__()
        self._scheduled_scan = False
        self._cron = "0 2 * * *"
        self._notify = True
        self._exclude_pids = ""
        self._clean_feedback = ""
        self._clean_feedback_color = ""
    def init_plugin(self, config: dict = None):
        """
        初始化插件配置
        """
        if config:
            self._scheduled_scan = config.get("scheduled_scan", False)
            self._cron = config.get("cron") or "0 2 * * *"
            self._notify = config.get("notify", True)
            self._exclude_pids = (config.get("exclude_pids") or "").strip()

    def get_state(self) -> bool:
        """仅在开启定时扫描时视为运行中；页面手动扫描/清理无需常驻运行。"""
        return self._scheduled_scan

    def get_command(self) -> List[Dict[str, Any]]:
        """注册手动扫描命令。"""
        return [
            {
                "cmd": "/pluginclinic",
                "event": EventType.PluginAction,
                "desc": "插件诊所：扫描并汇报插件异常",
                "category": "系统",
                "data": {
                    "action": "pluginclinic_scan"
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册 API 路由"""
        return [
            {
                "path": "/scan",
                "summary": "扫描插件状态",
                "description": "扫描所有插件的加载状态与残留情况",
                "endpoint": self._api_scan,
                "methods": ["POST"],
            },
            {
                "path": "/clean",
                "summary": "清理插件",
                "description": "API 路由由 MoviePilot 自动加上 `/pluginclinic` 前缀，body 支持 `pids` 与 `scope`",
                "endpoint": self._api_clean,
                "methods": ["POST"],
            },
            {
                "path": "/records",
                "summary": "获取清理记录",
                "description": "获取最近的清理记录",
                "endpoint": self._api_records,
                "methods": ["GET"],
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单"""
        from .config_form import build_form
        return build_form()

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册可选定时扫描服务。

        这里只扫描并发送告警，绝不调用清理逻辑；清理必须由用户手动执行。
        """
        if self._scheduled_scan and self._cron:
            return [
                {
                    "id": "pluginclinic_scan",
                    "name": "插件诊所定时扫描",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._scheduled_scan_job,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """停止插件服务；定时服务由 MoviePilot 调度器统一移除。"""
        pass

    def _scheduled_scan_job(self):
        """定时扫描：发现异常只通知，不执行清理。"""
        scan = self._scan()
        counts = scan.get("counts", {})
        abnormal_items = [
            item for item in scan.get("items", [])
            if item.get("status") in self.ABNORMAL_STATUSES
        ]
        if abnormal_items:
            logger.warning(
                "插件诊所定时扫描发现 %d 个异常/残留插件，未执行自动清理",
                len(abnormal_items),
            )
            if self._notify:
                self._notify_scan(scan)
        else:
            logger.info("插件诊所定时扫描完成：未发现异常或残留")

    def get_page(self) -> List[dict]:
        """
        插件页面：打开即自动扫描，展示扫描结果与清理记录
        """
        scan = self._scan()
        items = scan.get("items", [])
        counts = scan.get("counts", {})

        # 清理记录
        records = self._load_records()

        # ---------- KPI 卡 ----------
        def kpi_card(icon: str, label: str, value: str, value_color: str = "", note: str = "") -> dict:
            value_cls = f"text-h6 font-weight-bold text-{value_color}" if value_color else "text-h6 font-weight-bold"
            value_props = {
                "class": value_cls,
                "style": "font-size: clamp(0.8rem, 3.8vw, 1.25rem); white-space: nowrap; line-height: 1.2;",
            }
            right = [
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                {"component": "div", "props": {"class": "d-flex align-center flex-wrap"}, "content": [
                    {"component": "span", "props": value_props, "text": value},
                ]},
            ]
            if note:
                right.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": note})
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [
                    {"component": "VCardText", "props": {"class": "d-flex align-center"}, "content": [
                        {"component": "VAvatar", "props": {"rounded": True, "variant": "tonal", "color": "primary", "size": "x-large", "class": "me-3 flex-shrink-0"}, "content": [
                            {"component": "span", "props": {"style": "font-size: 2.25rem; line-height: 1;"}, "text": icon},
                        ]},
                        {"component": "div", "props": {"class": "flex-grow-1", "style": "min-width: 0;"}, "content": right},
                    ]},
                ],
            }

        # ---------- 扫描结果与手动清理清单 ----------
        # 页面操作使用 MoviePilot 已支持的 VBtn API 事件。每行一个清理按钮，
        # 用户先阅读具体问题，再决定是否对该项执行，不依赖前端临时勾选状态。
        api_base = f"plugin/{self.__class__.__name__}/clean?apikey={settings.API_TOKEN}"

        def clean_api(pid: str = "", scope: str = "selected") -> str:
            """把参数同时放入 URL 和 params，兼容不同 MP 前端版本。"""
            if pid:
                return f"{api_base}&pid={quote(pid)}&scope={scope}"
            return f"{api_base}&scope={scope}"

        def cell(text: Any, cls: str) -> dict:
            return {"component": "td", "props": {"class": cls}, "text": str(text or "—")}

        def run_tr(cells: List[tuple], head: bool = False) -> dict:
            return {
                "component": "tr",
                "content": [
                    {
                        "component": "th" if head else "td",
                        "props": {"class": cls},
                        "text": str(text or "—"),
                    }
                    for text, cls in cells
                ],
            }

        def clean_button(pid: str) -> dict:
            return {
                "component": "VBtn",
                "props": {
                    "color": "error",
                    "size": "small",
                    "variant": "tonal",
                    "prepend-icon": "mdi-delete-outline",
                },
                "text": "清理此项",
                "events": {
                    "click": {
                        "api": clean_api(pid, "selected"),
                        "method": "post",
                    },
                },
            }

        scan_table = {
            "component": "VTable",
            "props": {"hover": True, "density": "compact", "class": "clinic-scan-table"},
            "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "props": {"class": "text-body-2 text-start ps-3 text-no-wrap"}, "text": "插件"},
                    {"component": "th", "props": {"class": "text-body-2 text-center text-no-wrap"}, "text": "状态"},
                    {"component": "th", "props": {"class": "text-body-2 text-start"}, "text": "问题详情"},
                    {"component": "th", "props": {"class": "text-body-2 text-start"}, "text": "残留项"},
                    {"component": "th", "props": {"class": "text-body-2 text-center text-no-wrap"}, "text": "操作"},
                ]}]},
                {"component": "tbody", "content": []},
            ],
        }

        cleanable_items = []
        for it in items:
            st = it.get("status", "")
            label = self.STATUS_LABEL.get(st, st)
            color = self.STATUS_COLOR.get(st, "")
            issue = it.get("issue") or "—"
            if st == self.STATUS_OK:
                issue = "运行正常"
            leftovers = it.get("leftovers") or []
            can_clean = (
                st in self.ABNORMAL_STATUSES
                and not it.get("is_self")
                and not it.get("excluded")
            )
            if can_clean:
                cleanable_items.append(it)
            elif it.get("is_self"):
                action = "自身受保护"
            elif it.get("excluded"):
                action = "排除列表保护"
            else:
                action = "无需清理"
            row_cells = [
                cell(it.get("pid", "—"), "text-body-2 text-start ps-3 text-no-wrap font-weight-bold"),
                cell(label, f"text-body-2 font-weight-bold text-center text-no-wrap text-{color}"),
                cell(issue[:120], "text-body-2 text-start text-medium-emphasis"),
                cell("、".join(leftovers) if leftovers else "—", "text-body-2 text-start text-medium-emphasis"),
            ]
            if can_clean:
                row_cells.append({"component": "td", "props": {"class": "text-center text-no-wrap"}, "content": [clean_button(it["pid"])]})
            else:
                row_cells.append(cell(action, "text-body-2 text-center text-medium-emphasis text-no-wrap"))
            scan_table["content"][1]["content"].append({"component": "tr", "content": row_cells})

        # ---------- 清理记录表 ----------
        rec_table = {
            "component": "VTable",
            "props": {"hover": True, "density": "compact", "class": "clinic-records-table"},
            "content": [
                {"component": "thead", "content": [run_tr([
                    ("时间", "text-body-2 text-start ps-3 text-no-wrap"),
                    ("插件", "text-body-2 text-start text-no-wrap"),
                    ("清理项", "text-body-2 text-start"),
                    ("结果", "text-body-2 text-center text-no-wrap"),
                ], head=True)]},
                {"component": "tbody", "content": []},
            ],
        }
        for r in reversed(records[-20:]):
            ok = r.get("success")
            rec_table["content"][1]["content"].append(run_tr([
                (str(r.get("time", "—")), "text-body-2 text-start ps-3 text-no-wrap"),
                (str(r.get("pid", "—")), "text-body-2 text-start text-no-wrap font-weight-bold"),
                ("、".join(r.get("steps", []) or ["—"]), "text-body-2 text-start text-medium-emphasis"),
                ("✅ 成功" if ok else "❌ 失败", f"text-body-2 font-weight-bold text-center text-no-wrap {'text-success' if ok else 'text-error'}"),
            ]))

        page = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            kpi_card("🧩", "插件总数", f"{counts.get('total', 0)}", "", "扫描到的全部插件"),
                        ]
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            kpi_card("❌", "异常插件", f"{counts.get('abnormal', 0)}", "error" if counts.get("abnormal") else "", "加载失败 + 记录残留"),
                        ]
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            kpi_card("🗑️", "残留插件", f"{counts.get('leftover', 0)}", "info" if counts.get("leftover") else "", "目录 / 数据 / 配置残留"),
                        ]
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            kpi_card("🧹", "累计清理", f"{counts.get('cleaned', 0)}", "success" if counts.get("cleaned") else "", "清理成功次数"),
                        ]
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12}, "content": [
                            {
                                "component": "VCard", "props": {"variant": "tonal", "color": "warning"}, "content": [
                                    {"component": "VCardTitle", "props": {"class": "d-flex align-center justify-space-between flex-wrap ga-2"}, "content": [
                                        {"component": "span", "text": f"🔍 待清理清单（自动扫描：{len(cleanable_items)} 项可处理）"},
                                        {
                                            "component": "VBtn",
                                            "props": {
                                                "color": "error",
                                                "size": "small",
                                                "variant": "tonal",
                                                "prepend-icon": "mdi-delete-sweep-outline",
                                                "disabled": not bool(cleanable_items),
                                            },
                                            "text": "一键清理全部异常",
                                            "events": {"click": {
                                                "api": clean_api("", "all"),
                                                "method": "post",
                                                "params": {"scope": "all"},
                                            }},
                                        },
                                    ]},
                                    {"component": "VCardSubtitle", "props": {"class": "px-4 pb-2"}, "text": "每项均展示问题详情；点击「清理此项」即代表你已确认执行不可逆清理。受保护或排除项不会显示清理按钮。"},
                                    {"component": "VCardText", "props": {"class": "pa-2"}, "content": [scan_table]},
                                ]
                            }
                        ]
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12}, "content": [
                            {
                                "component": "VCard", "props": {"variant": "tonal", "color": "info"}, "content": [
                                    {"component": "VCardTitle", "text": "🧹 手动清理说明"},
                                    {"component": "VCardText", "content": [
                                        {"component": "div", "props": {"class": "text-body-2"}, "content": [
                                            {"component": "p", "props": {"class": "mb-1"},
                                             "text": "• 每个异常/残留插件右侧都有「清理此项」按钮：适合逐项确认后处理。"},
                                            {"component": "p", "props": {"class": "mb-1"},
                                             "text": "• 「一键清理全部异常」只处理待清理清单中的异常与残留；正常、插件诊所自身和排除列表中的插件始终不会被清理。"},
                                            {"component": "p", "props": {"class": "mb-1"},
                                             "text": "• 清理会删除插件目录、数据、配置和安装记录，操作不可逆；执行后刷新或重新打开页面查看结果。"},
                                            {"component": "p", "props": {"class": "mb-1"},
                                             "text": "• 打开插件页面会自动扫描；只有开启「定时扫描」后才会注册后台扫描任务。"},
                                        ]},
                                    ]},
                                ]
                            }
                        ]
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12}, "content": [
                            {
                                "component": "VCard", "props": {"variant": "tonal", "color": "primary"}, "content": [
                                    {"component": "VCardTitle", "text": "📋 清理记录（最近 20 条）"},
                                    {"component": "VCardText", "props": {"class": "pa-2"}, "content": [rec_table]},
                                ]
                            }
                        ]
                    },
                ],
            },
        ]
        return page

    # ======================== 扫描 ========================

    def _scan(self) -> dict:
        """
        扫描所有插件状态
        返回: {"items": [...], "counts": {...}}
        """
        try:
            installed = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
            plugins_dir = settings.ROOT_PATH / "app" / "plugins"
            exclude = self._get_exclude()
        except Exception as e:
            logger.error(f"插件诊所扫描初始化失败：{e}")
            return {"items": [], "counts": {}}

        # 收集候选插件 ID（已安装列表 + 目录 + 配置 + 模块缓存）
        pids = set()
        for i in installed:
            pids.add(i)
        if plugins_dir.exists():
            for d in plugins_dir.iterdir():
                if d.is_dir() and not d.name.startswith("_"):
                    pids.add(d.name)
        try:
            confs = SystemConfigOper().all() or {}
            for k in confs.keys():
                if str(k).startswith("plugin."):
                    pids.add(str(k)[len("plugin."):])
        except Exception:
            pass
        for m in list(sys.modules.keys()):
            if m.startswith("app.plugins.") and m.count(".") >= 2:
                pids.add(m.split(".")[2])

        items = []
        for pid in sorted(pids, key=str.lower):
            item = self._analyze(pid, installed, plugins_dir, exclude)
            if item:
                items.append(item)

        total = len(items)
        abnormal = sum(1 for it in items if it["status"] in (self.STATUS_LOAD_FAILED, self.STATUS_GHOST))
        leftover = sum(1 for it in items if it["status"] in (self.STATUS_DIR_LEFT, self.STATUS_DATA_LEFT))
        cleaned = len([r for r in self._load_records() if r.get("success")])

        return {
            "items": items,
            "counts": {"total": total, "abnormal": abnormal, "leftover": leftover, "cleaned": cleaned},
        }

    def _analyze(self, pid: str, installed: List[str], plugins_dir, exclude: set) -> Optional[dict]:
        """
        分析单个插件状态
        """
        lp = pid.lower()
        dir_path = plugins_dir / lp if plugins_dir.exists() else None
        dir_exists = bool(dir_path and dir_path.is_dir())
        in_installed = pid in installed
        is_self = lp == self.SELF_PID

        # 残留检查
        has_data = bool(PluginDataOper().get_data(pid))
        has_config = SystemConfigOper().get("plugin.%s" % pid) is not None
        has_module = any(
            m == f"app.plugins.{lp}" or m.startswith(f"app.plugins.{lp}.")
            for m in sys.modules
        )
        leftovers = []
        if has_data:
            leftovers.append("数据")
        if has_config:
            leftovers.append("配置")
        if has_module:
            leftovers.append("模块缓存")
        if dir_exists and not in_installed:
            leftovers.append("目录")

        # 无关插件（无任何痕迹）
        if not in_installed and not dir_exists and not has_data and not has_config and not has_module:
            return None

        # 尝试加载（仅对已安装且目录存在的）
        import_ok = False
        issue = ""
        if in_installed and dir_exists:
            try:
                importlib.import_module(f"app.plugins.{lp}")
                import_ok = True
            except Exception as e:
                msg = str(e) or type(e).__name__
                issue = msg[:200]
                if not issue:
                    issue = type(e).__name__

        # 分类
        if in_installed and dir_exists and import_ok:
            status = self.STATUS_OK
        elif in_installed and dir_exists and not import_ok:
            status = self.STATUS_LOAD_FAILED
            if not issue:
                issue = "插件导入失败（未知错误）"
        elif in_installed and not dir_exists:
            status = self.STATUS_GHOST
            if not issue:
                issue = "已安装记录存在但插件目录缺失，重装可能被卡"
        elif not in_installed and dir_exists:
            status = self.STATUS_DIR_LEFT
            if not issue:
                issue = "插件已卸载但目录仍残留"
        else:
            status = self.STATUS_DATA_LEFT
            if not issue:
                issue = "插件已卸载但数据/配置/模块缓存残留"

        return {
            "pid": pid,
            "status": status,
            "issue": issue,
            "leftovers": leftovers,
            "in_installed": in_installed,
            "dir_exists": dir_exists,
            "is_self": is_self,
            "excluded": lp in exclude,
        }

    def _get_exclude(self) -> set:
        """解析排除列表"""
        exclude = set()
        for p in (self._exclude_pids or "").split(","):
            p = p.strip().lower()
            if p:
                exclude.add(p)
        exclude.add(self.SELF_PID)
        return exclude

    # ======================== 清理 ========================

    def _clean(self, pids: List[str], scope: str = "selected") -> dict:
        """
        清理插件
        :param pids: 指定插件 ID 列表（scope=selected 时使用）
        :param scope: selected=仅清理指定插件；all=清理全部异常与残留插件
        """
        try:
            installed = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
            folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
            plugins_dir = settings.ROOT_PATH / "app" / "plugins"
        except Exception as e:
            logger.error(f"插件诊所清理初始化失败：{e}")
            return {"code": 0, "message": f"初始化失败：{e}", "data": []}

        exclude = self._get_exclude()
        pm = PluginManager()

        # 确定清理目标
        targets = []
        if scope == "all":
            scan = self._scan()
            targets = [it["pid"] for it in scan.get("items", []) if it["status"] in self.ABNORMAL_STATUSES]
        else:
            targets = list(pids or [])

        results = []
        for pid in targets:
            lp = pid.lower()
            # 安全护栏：排除自身与排除列表
            if lp in exclude:
                results.append({"pid": pid, "success": False, "steps": [], "reason": "已排除（自身或排除列表）"})
                continue

            steps = []
            ok = True
            try:
                # 1. 停止插件（未加载时安全跳过）
                pm.stop(pid)
                steps.append("停止插件")

                # 2. 清除模块缓存
                pm._clear_plugin_modules(pid)
                steps.append("清模块缓存")

                # 3. 移除定时任务
                try:
                    Scheduler().remove_plugin_job(pid)
                    steps.append("移除定时任务")
                except Exception:
                    pass

                # 4. 删除插件目录
                d = plugins_dir / lp
                if d.exists() and d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    steps.append("删除目录")

                # 5. 删除插件数据
                try:
                    PluginDataOper().del_data(pid)
                    steps.append("删除数据")
                except Exception:
                    pass

                # 6. 删除插件配置
                try:
                    SystemConfigOper().delete("plugin.%s" % pid)
                    steps.append("删除配置")
                except Exception:
                    pass

                # 7. 移出已安装列表
                if pid in installed:
                    installed.remove(pid)
                    steps.append("移出安装列表")

                # 8. 移出插件文件夹
                folder_changed = False
                for fname, fdata in (folders or {}).items():
                    if isinstance(fdata, dict) and "plugins" in fdata:
                        plist = fdata["plugins"]
                        if pid in plist:
                            plist.remove(pid)
                            folder_changed = True
                if folder_changed:
                    SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
                    steps.append("移出插件文件夹")

                # 再次清理模块缓存（目录删除后兜底）
                pm._clear_plugin_modules(pid)

            except Exception as e:
                ok = False
                steps.append(f"异常:{str(e)[:100]}")
                logger.error(f"插件诊所清理 {pid} 失败：{e}")

            results.append({"pid": pid, "success": ok, "steps": steps})
            self._save_record(pid, steps, ok)

        # 保存已安装列表变更
        try:
            SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, installed)
        except Exception as e:
            logger.error(f"保存已安装列表失败：{e}")

        cleaned = sum(1 for r in results if r["success"])
        failed = len(results) - cleaned
        message = f"清理完成：成功 {cleaned} 个，失败 {failed} 个"
        logger.info(f"插件诊所：{message}")

        # 通知
        if self._notify and results:
            self._notify_clean(results, cleaned, failed)

        return {"code": 1 if failed == 0 else 0, "message": message, "data": results}

    # ======================== 记录 ========================

    def _load_records(self) -> List[dict]:
        """加载清理记录"""
        try:
            data = self.get_data("pluginclinic_data") or {}
            return data.get("records", []) or []
        except Exception:
            return []

    def _save_record(self, pid: str, steps: List[str], success: bool):
        """保存一条清理记录（最多 200 条）"""
        try:
            data = self.get_data("pluginclinic_data") or {}
            records = data.get("records", []) or []
            records.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "pid": pid,
                "steps": steps,
                "success": success,
            })
            data["records"] = records[-200:]
            self.save_data("pluginclinic_data", data)
        except Exception as e:
            logger.error(f"保存清理记录失败：{e}")

    # ======================== 通知 ========================

    def _notify_clean(self, results: List[dict], cleaned: int, failed: int):
        """清理完成通知"""
        lines = [
            f"🧹 插件诊所清理完成",
            "",
            f"✅ 成功：{cleaned} 个　❌ 失败：{failed} 个",
        ]
        for r in results:
            mark = "✅" if r.get("success") else "❌"
            reason = f"（{r.get('reason')}）" if r.get("reason") else ""
            lines.append(f"{mark} {r.get('pid')}{reason}")
        self._send_notification(self._format_notification("插件诊所", "\n".join(lines)))

    def _notify_scan(self, scan: dict):
        """扫描结果通知"""
        counts = scan.get("counts", {})
        lines = [
            f"🔍 插件诊所扫描完成",
            "",
            f"🧩 插件总数：{counts.get('total', 0)}",
            f"❌ 异常插件：{counts.get('abnormal', 0)}",
            f"🗑️ 残留插件：{counts.get('leftover', 0)}",
        ]
        items = scan.get("items", [])
        bad = [it for it in items if it["status"] != self.STATUS_OK][:10]
        if bad:
            lines.append("")
            lines.append("异常清单：")
            for it in bad:
                lines.append(f"· {it['pid']}：{it.get('issue', '')[:50]}")
        else:
            lines.append("")
            lines.append("🎉 一切正常，无需清理")
        self._send_notification(self._format_notification("插件诊所", "\n".join(lines)))

    def _format_notification(self, title: str, body: str) -> str:
        """统一通知格式"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (body or "").strip()
        if body:
            return f"🕒 {ts}\n{title}\n\n{body}"
        return f"🕒 {ts}\n{title}"

    def _send_notification(self, message: str):
        """发送通知"""
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="插件诊所",
                text=message,
            )
        except Exception as e:
            logger.error(f"发送通知失败：{e}")

    # ======================== API 端点 ========================

    def _api_scan(self, *args, **kwargs) -> dict:
        """API：手动扫描插件状态。"""
        scan = self._scan()
        if self._notify:
            self._notify_scan(scan)
        return {
            "code": 1,
            "message": "扫描完成",
            "data": scan,
        }

    def _api_clean(
        self,
        pid: str = "",
        scope: str = "selected",
        pids: str = "",
        data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> dict:
        """
        API：执行已由用户点击确认的清理。

        MoviePilot 页面按钮的 ``params`` 会作为查询参数传入，因此这里必须
        使用显式参数签名；仅使用 ``*args, **kwargs`` 时 FastAPI 不会把查询
        参数注入 kwargs，表现就是点击后没有实际清理。
        同时兼容 JSON body 和逗号分隔的 pids 查询参数。
        """
        body = data if isinstance(data, dict) else {}
        pid = pid or body.get("pid") or kwargs.get("pid", "")
        scope = scope or body.get("scope") or kwargs.get("scope", "selected")
        raw_pids = pids or body.get("pids") or kwargs.get("pids", [])
        if pid:
            selected = [pid]
        elif isinstance(raw_pids, str):
            selected = [item.strip() for item in raw_pids.split(",") if item.strip()]
        else:
            selected = list(raw_pids or [])
        if scope not in ("selected", "all"):
            return {"code": 0, "message": "无效清理范围", "data": []}
        if scope == "selected" and not selected:
            return {"code": 0, "message": "请先选择要清理的插件", "data": []}
        result = self._clean(pids=selected, scope=scope)
        result["success"] = result.get("code") == 1
        return result

    def _api_records(self, *args, **kwargs) -> dict:
        """API：获取清理记录"""
        records = self._load_records()
        return {
            "code": 1,
            "message": "success",
            "data": list(reversed(records[-50:])),
        }
