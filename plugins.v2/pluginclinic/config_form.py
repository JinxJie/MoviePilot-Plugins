"""
插件诊所配置表单
"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """配置表单定义。"""
    return [
        {
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VSwitch",
                                "props": {
                                    "model": "enabled",
                                    "label": "启用插件",
                                    "hint": "开启后可使用扫描、清理、命令和 API",
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VSwitch",
                                "props": {
                                    "model": "scheduled_scan",
                                    "label": "定时扫描",
                                    "hint": "只扫描并通知，绝不自动清理",
                                },
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VSwitch",
                                "props": {
                                    "model": "notify",
                                    "label": "发送通知",
                                    "hint": "定时扫描发现问题时发送通知",
                                },
                            }],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VCronField",
                            "props": {
                                "model": "cron",
                                "label": "扫描时间",
                                "placeholder": "0 2 * * *",
                                "hint": "默认每天 02:00；仅在开启「定时扫描」后生效",
                            },
                        }],
                    }],
                },
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VTextField",
                            "props": {
                                "model": "exclude_pids",
                                "label": "排除插件 ID（逗号分隔）",
                                "placeholder": "如 NodeSeek, HHLottery",
                                "hint": "这些插件不会被清理；插件诊所自身始终自动排除",
                                "clearable": True,
                            },
                        }],
                    }],
                },
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "text": "打开插件页面即自动扫描。定时扫描只负责发现问题并通知，不执行任何清理；清理必须由你在扫描结果中确认后手动执行。",
                            },
                        }],
                    }],
                },
            ],
        },
    ], {
        "enabled": False,
        "scheduled_scan": False,
        "cron": "0 2 * * *",
        "notify": True,
        "exclude_pids": "",
    }
