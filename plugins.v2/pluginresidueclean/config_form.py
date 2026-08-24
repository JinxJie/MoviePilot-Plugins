"""
插件残留清理插件配置表单
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
                # 第一行：开关组（定时扫描 / 通知）
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
                                        "model": "scheduled_scan",
                                        "label": "定时扫描",
                                        "hint": "开启后按下方时间自动扫描，发现残留仅发通知，不自动清理",
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
                                        "hint": "扫描发现残留 / 清理完成均通过此开关控制",
                                    },
                                }
                            ],
                        },
                    ],
                },
                # 第二行：扫描时间
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "扫描时间",
                                        "placeholder": "0 3 * * *",
                                        "hint": "默认每天 03:00 扫描一次",
                                    },
                                }
                            ],
                        },
                    ],
                },
                # 第三行：排除列表
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
                                        "label": "排除插件 ID（可选）",
                                        "placeholder": "多个 ID 用英文逗号分隔，例如：hhlottery,nodeseek",
                                        "hint": "这些插件即使有残留也不会被清理；插件自身始终受保护",
                                    },
                                }
                            ],
                        },
                    ],
                },
            ],
        },
    ], {
        "scheduled_scan": False,
        "cron": "0 3 * * *",
        "notify": True,
        "exclude_pids": "",
    }
