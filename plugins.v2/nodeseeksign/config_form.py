"""
NodeSeek 自动签到插件配置表单
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
                # 第一行：开关组（启用 / 立即运行一次 / 通知 / 代理）
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enabled",
                                        "label": "启用插件",
                                        "hint": "开启后按签到时间自动运行",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "onlyonce",
                                        "label": "立即运行一次",
                                        "hint": "保存后立即签到一次，完成后自动复位",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "notify",
                                        "label": "发送通知",
                                        "hint": "签到成功/失败/Cookie 失效均通过此开关控制",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "use_proxy",
                                        "label": "使用系统代理",
                                        "hint": "通过 MoviePilot 系统代理访问 NodeSeek",
                                    },
                                }
                            ],
                        },
                    ],
                },
                # 第二行：Cookie
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
                                        "model": "cookie",
                                        "label": "Cookie",
                                        "placeholder": "粘贴 NodeSeek 登录 Cookie（nodeseek.com 的值）",
                                        "type": "textarea",
                                        "rows": 3,
                                        "clearable": True,
                                        "required": True,
                                    },
                                }
                            ],
                        },
                    ],
                },
                # 第三行：签到时间
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
                                        "label": "签到时间",
                                        "placeholder": "30 0 * * *",
                                        "hint": "每天 00:00 刷新签到，越早签到排名越靠前。默认 00:30，想抢前排可改为 5 0 * * *",
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
        "onlyonce": False,
        "cookie": "",
        "cron": "30 0 * * *",
        "notify": True,
        "use_proxy": False,
    }
