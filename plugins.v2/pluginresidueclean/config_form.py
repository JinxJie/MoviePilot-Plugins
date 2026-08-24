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
                                        "hint": "开启后按下方时间自动扫描；只检查卸载残留并发送通知，绝不自动删除任何内容",
                                        "persistent-hint": True,
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
                                        "hint": "控制插件通知总开关：定时扫描发现残留、手动清理完成和清理失败都会通知",
                                        "persistent-hint": True,
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
                                        "hint": "每天按此 Cron 规则执行只读扫描；扫描不会删除目录、数据或配置",
                                        "persistent-hint": True,
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
                                        "hint": "填写插件目录 ID，多个 ID 用英文逗号分隔；排除项只展示、不删除，插件自身始终受保护",
                                        "persistent-hint": True,
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
