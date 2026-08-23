"""配置页的 Vuetify JSON 纯拼装，不碰 self。"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """
    返回插件配置表单（Vuetify 组件）
    """
    form = [
        {
            "component": "VForm",
            "content": [
                # ── 基础设置 ──
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enabled",
                                        "label": "启用插件",
                                        "color": "primary",
                                    },
                                }
                            ],
                        },
                    ],
                },
                # ── 定时与站点 ──
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "⏰ 定时表达式",
                                        "placeholder": "5 0 * * *",
                                        "hint": "默认每天 0:05 运行",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "site_url",
                                        "label": "🌐 站点地址",
                                        "placeholder": "https://hhanclub.net",
                                    },
                                }
                            ],
                        },
                    ],
                },
                # ── Cookie ──
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "cookie",
                                        "label": "🔑 Cookie",
                                        "placeholder": "从浏览器 F12 复制完整 Cookie",
                                        "rows": 2,
                                        "hint": "必填，F12 → Network → 复制 Cookie",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                    ],
                },
                # ── 抽奖参数 ──
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "interval",
                                        "label": "🎲 抽奖间隔（秒）",
                                        "placeholder": "8",
                                        "type": "number",
                                        "hint": "被限流时自动增加",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_count",
                                        "label": "🔢 最大抽奖次数",
                                        "placeholder": "0",
                                        "type": "number",
                                        "hint": "0 = 不限制；赌徒模式开启时自动忽略",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "reserve_beans",
                                        "label": "💰 保留憨豆数",
                                        "placeholder": "0",
                                        "type": "number",
                                        "hint": "低于此值停止，0 = 不限制",
                                        "persistent-hint": True,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "log_lines",
                                        "label": "📝 日志条数",
                                        "placeholder": "200",
                                        "type": "number",
                                        "hint": "页面显示保留的日志条数",
                                        "persistent-hint": True,
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
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "grand_stop",
                                        "label": "🏆 大奖止损",
                                        "color": "primary",
                                        "hint": "总开关：命中目标大奖后停止抽奖",
                                        "persistent-hint": True,
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
                                        "model": "big_prize_keywords",
                                        "label": "🏆 目标关键词",
                                        "placeholder": "VIP,邀请,780000",
                                        "hint": "逗号分割，如 VIP,邀请,780000",
                                        "persistent-hint": True,
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
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "gambler_mode",
                                        "label": "🎲 赌徒模式",
                                        "color": "warning",
                                        "hint": "开启后：命中大奖也继续抽，且最大抽奖次数按 0 处理",
                                        "persistent-hint": True,
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
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "onlyonce",
                                        "label": "▶️ 立即运行一次",
                                        "color": "warning",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "stop_current",
                                        "label": "🛑 停止当前抽奖",
                                        "color": "error",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "div",
                                    "children": "保存后会立即停止当前抽奖；这是一次性开关，触发后会自动清回关闭状态",
                                    "props": {"style": "font-size:12px;color:#9a6a5a;margin-top:-4px;"},
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
                                        "density": "compact",
                                        "text": "💡 也可使用油猴脚本：HHCLUB 自动抽奖 · 庆典版 https://greasyfork.org/zh-CN/scripts/591722",
                                    },
                                }
                            ],
                        }
                    ]
                }
            ],
        }
    ]

    default_config = {
        "enabled": False,
        "cron": "5 0 * * *",
        "cookie": "",
        "site_url": "https://hhanclub.net",
        "interval": 8,
        "max_count": 0,
        "reserve_beans": 0,
        "log_lines": 200,
        "notify": True,
        "clean_mail": True,
        "onlyonce": False,
        "stop_current": False,
        "save_epoch": 0,
        "save_id": "",
        "current_save_version": "",
        "grand_stop": True,
        "gambler_mode": False,
        "big_prize_keywords": "VIP,邀请,780000",
    }

    return form, default_config
