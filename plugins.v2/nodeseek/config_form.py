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
                    "text": "先完成 Cookie 配置，再按需开启自动登录。保存后可用“一次性签到”检查当前配置是否可用；敏感信息仅由 MoviePilot 保存。",
                },
                {
                    "component": "VDivider",
                    "props": {"class": "mb-4"},
                },
                # 随机奖励与延迟
                {
                    "component": "VRow",
                    "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "ns_random", "label": "浮动收益模式", "hint": "开启后请求随机奖励；关闭时按站点默认的 5 个鸡腿执行。", "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "cookie_first", "label": "优先使用现有 Cookie", "hint": "只有当前会话确认失效时，才会启动备用登录流程。", "persistent-hint": True}}]},
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
                                {"component": "VExpansionPanelTitle", "text": "备用登录方案（Cookie 失效时使用）"},
                                {
                                    "component": "VExpansionPanelText",
                                    "content": [
                                        {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "density": "compact", "class": "mb-3"}, "text": "这是备用通道，不会在每次签到时登录。需要 MoviePilot 2.12.0 或更高版本，并准备好浏览器仿真环境和 Turnstile 验证码服务。"},
                                        {"component": "VTextarea", "props": {"model": "accounts", "label": "登录账号", "rows": 3, "placeholder": "每行一组：用户名----密码", "hint": "仅在现有 Cookie 无法使用时启用；请勿在日志或公开渠道分享。", "persistent-hint": True}},
                                        {"component": "VSelect", "props": {"model": "solver_type", "label": "验证服务类型", "items": [{"title": "YesCaptcha", "value": "yescaptcha"}, {"title": "2Captcha", "value": "2captcha"}], "hint": "用于完成站点的人机验证，请确保账户有可用额度。", "persistent-hint": True}},
                                        {"component": "VTextField", "props": {"model": "client_key", "label": "验证服务密钥", "type": "password", "clearable": True, "hint": "只写入插件配置，运行日志不会输出密钥内容。", "persistent-hint": True}},
                                        {"component": "VTextField", "props": {"model": "api_base_url", "label": "服务地址（可选）", "placeholder": "不确定时保持为空", "hint": "只有使用兼容的自定义节点时才需要填写。", "persistent-hint": True}},
                                        {"component": "VSwitch", "props": {"model": "auto_save_cookie", "label": "保存新会话", "hint": "登录成功后把新 Cookie 写回配置，后续优先复用，减少重复验证。", "persistent-hint": True}},
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
                                        "hint": "控制后台自动执行；关闭后仍可手动触发签到。",
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
                                        "hint": "保存设置后排入一次任务，适合首次配置或排查连接问题；执行结束会自动复位。",
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
                                        "hint": "将结果发送到 MoviePilot 通知渠道，便于不打开页面也能了解执行情况。",
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
                                        "hint": "仅在当前网络需要经 MoviePilot 代理访问站点时打开；否则保持关闭。",
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
                                        "placeholder": "粘贴 NodeSeek 账号的 Cookie 字符串",
                                        "hint": "这是首选登录凭据；有效期间插件不会主动切换到账号密码。",
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
                                        "hint": "后台任务的触发时间；任务触发后会内置随机等待约 1 分钟再访问站点。",
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
        "cookie_first": True,
        "accounts": "",
        "solver_type": "yescaptcha",
        "client_key": "",
        "api_base_url": "",
        "auto_save_cookie": True,
    }
