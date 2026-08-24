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
                # 随机奖励与延迟
                {
                    "component": "VRow",
                    "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "ns_random", "label": "随机鸡腿奖励", "hint": "开启：随机 1~11 个鸡腿；关闭：固定 5 个鸡腿（NS_RANDOM）", "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "random_delay", "label": "签到时间随机延迟（分钟）", "type": "number", "min": 0, "step": 1, "hint": "定时任务触发后随机等待 0~N 分钟再签到；0 表示关闭，手动签到不延迟", "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "cookie_first", "label": "Cookie 优先", "hint": "Cookie 有效就一直使用；仅失效时才尝试账密登录刷新", "persistent-hint": True}}]},
                    ],
                },
                # 账号密码与验证码服务（可选）
                {
                    "component": "VExpansionPanels",
                    "props": {"variant": "accordion", "class": "mb-4"},
                    "content": [
                        {
                            "component": "VExpansionPanel",
                            "content": [
                                {"component": "VExpansionPanelTitle", "text": "🔐 Cookie 失效后的自动登录（可选）"},
                                {
                                    "component": "VExpansionPanelText",
                                    "content": [
                                        {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "density": "compact", "class": "mb-3"}, "text": "账密登录要求 MoviePilot ≥ 2.12.0、CloakBrowser 和 YesCaptcha/2Captcha Turnstile 密钥。登录始终使用干净浏览器上下文，不注入旧 Cookie。"},
                                        {"component": "VTextarea", "props": {"model": "accounts", "label": "账号密码", "rows": 3, "placeholder": "每行一个：用户名----密码", "hint": "仅当 Cookie 失效时使用；密码不会写入日志", "persistent-hint": True}},
                                        {"component": "VSelect", "props": {"model": "solver_type", "label": "验证码服务", "items": [{"title": "YesCaptcha", "value": "yescaptcha"}, {"title": "2Captcha", "value": "2captcha"}], "hint": "用于 Cloudflare Turnstile；需要对应服务的 API 密钥", "persistent-hint": True}},
                                        {"component": "VTextField", "props": {"model": "client_key", "label": "验证码服务 API 密钥", "type": "password", "clearable": True, "hint": "只保存在 MoviePilot 配置中，不会写入日志", "persistent-hint": True}},
                                        {"component": "VTextField", "props": {"model": "api_base_url", "label": "验证码服务节点（可选）", "placeholder": "留空使用默认官方节点", "hint": "一般留空即可；仅在使用自定义兼容节点时填写", "persistent-hint": True}},
                                        {"component": "VSwitch", "props": {"model": "auto_save_cookie", "label": "登录成功后写回 Cookie", "hint": "将干净浏览器登录得到的新 Cookie 写回插件配置，下一次优先复用", "persistent-hint": True}},
                                    ],
                                },
                            ],
                        },
                    ],
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
        "ns_random": False,
        "random_delay": 0,
        "cookie_first": True,
        "accounts": "",
        "solver_type": "yescaptcha",
        "client_key": "",
        "api_base_url": "",
        "auto_save_cookie": True,
    }
