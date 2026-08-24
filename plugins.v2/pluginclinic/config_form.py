"""
插件诊所配置表单
"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """
    配置表单定义
    """
    return [
        {
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enabled",
                                        "label": "启用插件",
                                        "hint": "开启后可响应命令与 API",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "notify",
                                        "label": "发送通知",
                                        "hint": "扫描结果 / 清理完成通知",
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "exclude_pids",
                                        "label": "排除插件 ID（逗号分隔）",
                                        "placeholder": "如 NodeSeek, HHLottery",
                                        "hint": "这些插件不会被清理；插件诊所自身始终自动排除",
                                        "clearable": True,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "打开插件页面即自动扫描。发现异常/残留插件后，在页面勾选并点击「清理所选」，或调用 API / 命令触发清理。清理为不可逆操作，请谨慎选择。",
                                    },
                                }
                            ],
                        },
                    ],
                },
            ],
        },
    ], {
        "enabled": False,
        "notify": True,
        "exclude_pids": "",
    }
