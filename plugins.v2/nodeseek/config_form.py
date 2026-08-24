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
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mb-4", "icon": "mdi-information-outline"},
                    "text": "首次使用请粘贴 NodeSeek Cookie；保存“立即运行一次”后可验证 Cookie 是否有效。Cookie 仅保存在 MoviePilot 插件配置中。",
                },
                {
                    "component": "VDivider",
                    "props": {"class": "mb-4"},
                },
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
                                        "hint": "仅影响定时任务；关闭后仍可手动签到或使用“立即运行一次”",
                                        "persistent-hint": True,
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
                                        "hint": "保存后立即执行一次签到，用于验证 Cookie；完成后自动关闭，不会重复执行",
                                        "persistent-hint": True,
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
                                        "hint": "建议开启；签到成功、重复签到、Cookie 失效和网络错误都会发送结果",
                                        "persistent-hint": True,
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
                                        "hint": "仅在你的 MoviePilot 已正确配置系统代理时开启；关闭则直连 NodeSeek",
                                        "persistent-hint": True,
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
