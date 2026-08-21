"""
HHCLUB 自动抽奖插件 - MoviePilot V2/V3 兼容

功能：
- 自动抽奖（自适应间隔）
- 大奖即时通知
- 站内信自动清理
- Cron 定时运行
- 抽奖统计与历史记录
"""

import re
import time
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType
from app.utils.http import RequestUtils

from app.plugins import _PluginBase


class HHLottery(_PluginBase):
    """
    HHCLUB 自动抽奖插件
    """

    # 插件元信息
    plugin_name = "HHCLUB 自动抽奖"
    plugin_desc = "HHCLUB 自动抽奖增强版 · 大奖即时通知、站内信自动清理、Cron 定时运行 · 油猴脚本版：https://greasyfork.org/zh-CN/scripts/591722"
    plugin_icon = "hhlottery.png"
    plugin_version = "1.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "hhlottery_"
    plugin_order = 0
    auth_level = 2

    # ======================== 常量定义 ========================

    # 默认浏览器 User-Agent
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # 请求 headers（抽奖用）
    DRAW_HEADERS = {
        "accept": "*/*",
        "origin": "https://hhanclub.net",
        "referer": "https://hhanclub.net/lucky.php",
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    # 默认抽奖间隔（秒）
    DEFAULT_INTERVAL = 8

    # 最大间隔（秒）
    MAX_INTERVAL = 30

    # 间隔递增系数
    INTERVAL_MULTIPLIER = 1.5

    # 连续异常停止阈值
    MAX_CONSECUTIVE_ERRORS = 5

    # 连续限流停止阈值
    MAX_CONSECUTIVE_THROTTLE = 12

    # 余额校准间隔（每 N 次抽奖校准一次余额）
    BALANCE_CHECK_INTERVAL = 20

    # 站内信清理间隔（每 N 次抽奖清理一次）
    MAIL_CLEAN_INTERVAL = 20

    # 大额憨豆阈值
    BIG_BEANS_THRESHOLD = 500000

    # ======================== 实例变量 ========================

    # 配置项
    _enabled: bool = False
    _cron: str = "5 0 * * *"
    _cookie: str = ""
    _site_url: str = "https://hhanclub.net"
    _interval: int = DEFAULT_INTERVAL
    _max_count: int = 0
    _reserve_beans: int = 0
    _notify: bool = True
    _big_prize_keywords: str = "VIP,邀请,500000"
    _clean_mail: bool = True
    _onlyonce: bool = False

    # 运行状态
    _running: bool = False

    def init_plugin(self, config: dict = None):
        """
        初始化插件，加载配置
        """
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "5 0 * * *")
            self._cookie = config.get("cookie", "")
            self._site_url = config.get("site_url", "https://hhanclub.net").rstrip("/")
            self._interval = int(config.get("interval", self.DEFAULT_INTERVAL))
            self._max_count = int(config.get("max_count", 0))
            self._reserve_beans = int(config.get("reserve_beans", 0))
            self._notify = config.get("notify", True)
            self._big_prize_keywords = config.get("big_prize_keywords", "VIP,邀请,500000")
            self._clean_mail = config.get("clean_mail", True)
            self._onlyonce = config.get("onlyonce", False)

        # 如果设置了立即运行
        if self._onlyonce and self._cookie:
            self._onlyonce = False
            # 更新配置，重置 onlyonce
            self.update_config({
                "enabled": self._enabled,
                "cron": self._cron,
                "cookie": self._cookie,
                "site_url": self._site_url,
                "interval": self._interval,
                "max_count": self._max_count,
                "reserve_beans": self._reserve_beans,
                "notify": self._notify,
                "big_prize_keywords": self._big_prize_keywords,
                "clean_mail": self._clean_mail,
                "onlyonce": False,
            })
            # 异步触发抽奖任务
            import threading
            threading.Thread(target=self._lottery_job, daemon=True).start()

    def get_state(self) -> bool:
        """
        获取插件启用状态
        """
        return self._enabled and bool(self._cookie)

    def get_command(self) -> List[Dict[str, Any]]:
        """
        注册命令（供消息平台调用）
        """
        return [
            {
                "cmd": "/hhlottery",
                "event": EventType.PluginAction,
                "desc": "HHCLUB 立即抽奖",
                "category": "站点",
                "data": {
                    "action": "hhlottery_run"
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册 API 路由
        """
        return [
            {
                "path": "/hhlottery/run",
                "summary": "立即运行抽奖",
                "description": "触发一次抽奖任务",
                "method": "POST",
                "func": self._api_run_lottery,
            },
            {
                "path": "/hhlottery/stats",
                "summary": "获取抽奖统计",
                "description": "获取当前统计和历史记录",
                "method": "GET",
                "func": self._api_get_stats,
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                            "hint": "0 = 不限制",
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
                        ],
                    },
                    # ── 通知与清理 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "📢 推送通知",
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clean_mail",
                                            "label": "🧹 自动清理站内信",
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "sm": 4},
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
                        ],
                    },
                    # ── 大奖关键词 ──
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
                                            "label": "🏆 大奖关键词",
                                            "placeholder": "VIP,邀请,500000",
                                            "hint": "逗号分隔，命中立即通知并停止抽奖",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 脚本备注 ──
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
                    },
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
            "notify": True,
            "big_prize_keywords": "VIP,邀请,500000",
            "clean_mail": True,
            "onlyonce": False,
        }

        return form, default_config

    def get_page(self) -> List[dict]:
        """
        统计页面（Vuetify 组件，适配移动端）
        """
        data = self._load_data()
        stats = data.get("stats", {})
        history = data.get("history", [])

        # 累计统计
        total_count = stats.get("total_count", 0)
        total_cost = stats.get("total_cost", 0)
        total_wins = stats.get("total_wins", 0)
        prize_detail = stats.get("prize_detail", {})
        last_balance = stats.get("last_balance", 0)

        # 本轮统计
        round_stats = stats.get("round", {})
        round_count = round_stats.get("count", 0)
        round_cost = round_stats.get("cost", 0)
        round_wins = round_stats.get("wins", 0)

        # 奖品统计文本
        prize_lines = []
        for name, count in sorted(prize_detail.items(), key=lambda x: -x[1]):
            prize_lines.append(f"• {name} × {count}")
        prize_text = "\n".join(prize_lines) if prize_lines else "暂无记录"

        # 最近中奖（最新 20 条）
        recent = history[-20:][::-1]
        history_lines = []
        for item in recent:
            ts = item.get("time", "")[-8:]  # 只取时间部分
            prize = item.get("prize", "")
            history_lines.append(f"[{ts}] {prize}")
        history_text = "\n".join(history_lines) if history_lines else "暂无记录"

        page = [
            # ── 顶部统计卡片 ──
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 6, "sm": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "primary"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "text": f"🎲 {total_count:,}",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center text-caption"},
                                        "text": "总抽奖次数",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 6, "sm": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "warning"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "text": f"💸 {total_cost:,}",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center text-caption"},
                                        "text": "总消耗憨豆",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 6, "sm": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "success"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "text": f"🏆 {total_wins:,}",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center text-caption"},
                                        "text": "总中奖次数",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 6, "sm": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "info"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "text": f"💰 {last_balance:,}",
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center text-caption"},
                                        "text": "当前余额",
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
            # ── 本轮统计 + 奖品统计 ──
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "sm": 4},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "outlined"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "🔄 本轮统计",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": (
                                            f"抽奖次数：{round_count:,}\n"
                                            f"消耗憨豆：{round_cost:,}\n"
                                            f"中奖次数：{round_wins:,}"
                                        ),
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "sm": 8},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "outlined"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "🎁 奖品统计",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": prize_text,
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
            # ── 最近中奖记录 ──
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "outlined"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "📜 最近中奖记录",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": history_text,
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ]

        return page
    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册定时服务
        """
        if self._enabled and self._cookie:
            return [
                {
                    "id": "hhlottery",
                    "name": "HHCLUB 自动抽奖",
                    "trigger": "cron",
                    "cron": self._cron,
                    "func": self._lottery_job,
                    "kwargs": {},
                }
            ]
        return []

    # ======================== 核心逻辑 ========================

    def _lottery_job(self):
        """
        抽奖主任务入口
        流程：获取余额 → 循环抽奖 → 清理站内信 → 发送汇总通知
        """
        if self._running:
            logger.warning("HHCLUB 抽奖任务正在运行，跳过本次")
            return

        self._running = True
        logger.info("🎰 HHCLUB 自动抽奖任务开始")

        # 初始化本轮统计
        round_stats = {
            "count": 0,
            "cost": 0,
            "wins": 0,
            "start_balance": 0,
            "prize_detail": {},
            "history": [],
        }

        # 停止原因
        stop_reason = ""

        # 自适应间隔
        current_interval = self._interval

        # 连续异常计数
        consecutive_errors = 0

        # 连续限流计数
        consecutive_throttle = 0

        # 是否命中大奖
        big_prize_hit = False

        try:
            # 1. 获取初始余额
            balance, cost_per_draw = self._fetch_balance()
            if balance is None:
                stop_reason = "无法获取余额，请检查 Cookie 是否有效"
                logger.error(stop_reason)
                self._send_notification(f"❌ HHCLUB 抽奖异常\n{stop_reason}")
                self._running = False
                return

            logger.info(f"💰 当前余额：{balance:,} 憨豆，单次消耗：{cost_per_draw:,} 憨豆")
            round_stats["start_balance"] = balance

            # 更新余额到统计
            self._update_stats_field("last_balance", balance)

            draw_count = 0

            # 2. 抽奖循环
            while True:
                # 检查停止条件：余额不足
                if balance < cost_per_draw:
                    stop_reason = f"余额不足（余额 {balance:,}，需要 {cost_per_draw:,}）"
                    break

                # 检查停止条件：保留憨豆
                if self._reserve_beans > 0 and balance - cost_per_draw < self._reserve_beans:
                    stop_reason = f"余额低于保留线（余额 {balance:,}，保留线 {self._reserve_beans:,}）"
                    break

                # 检查停止条件：最大次数
                if self._max_count > 0 and draw_count >= self._max_count:
                    stop_reason = f"达到最大抽奖次数（{self._max_count}）"
                    break

                # 检查停止条件：连续异常
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    stop_reason = f"连续 {consecutive_errors} 次异常"
                    break

                # 检查停止条件：连续限流
                if consecutive_throttle >= self.MAX_CONSECUTIVE_THROTTLE:
                    stop_reason = f"连续 {consecutive_throttle} 次被限流"
                    break

                # 执行抽奖
                try:
                    result = self._do_draw()
                except Exception as e:
                    logger.error(f"抽奖请求异常：{e}")
                    consecutive_errors += 1
                    consecutive_throttle = 0
                    time.sleep(current_interval)
                    continue

                if result is None:
                    # 请求失败
                    consecutive_errors += 1
                    consecutive_throttle = 0
                    time.sleep(current_interval)
                    continue

                # 解析结果
                ret = result.get("ret")
                data = result.get("data", {})

                if ret == -1 or ret == "throttle":
                    # 被限流
                    consecutive_throttle += 1
                    consecutive_errors = 0
                    logger.warning(f"⚠️ 被限流（第 {consecutive_throttle} 次），增加间隔")

                    # 自适应增加间隔
                    current_interval = min(
                        current_interval * self.INTERVAL_MULTIPLIER,
                        self.MAX_INTERVAL,
                    )
                    logger.info(f"⏱️ 间隔调整为 {current_interval:.1f} 秒")
                    time.sleep(current_interval)
                    continue

                if ret != 0 and ret != "0":
                    # 其他错误
                    msg = data.get("msg", "") or data.get("message", "") or str(result)
                    logger.warning(f"抽奖返回异常：ret={ret}, msg={msg}")
                    consecutive_errors += 1
                    consecutive_throttle = 0
                    time.sleep(current_interval)
                    continue

                # 成功抽奖
                consecutive_errors = 0
                consecutive_throttle = 0

                # 恢复默认间隔
                if current_interval > self._interval:
                    current_interval = self._interval
                    logger.info(f"⏱️ 间隔恢复为 {current_interval} 秒")

                draw_count += 1
                balance -= cost_per_draw

                # 解析奖品
                prize_text = data.get("prize_text", "")
                prize_type, prize_name, prize_value = self._parse_prize(prize_text)

                logger.info(f"🎰 第 {draw_count} 抽：{prize_name}（{prize_text}）")

                # 更新统计
                round_stats["count"] += 1
                round_stats["cost"] += cost_per_draw

                # 奖品统计（所有类型都记录）
                if prize_type == "beans":
                    balance += prize_value
                round_stats["wins"] += 1
                round_stats["prize_detail"][prize_name] = (
                    round_stats["prize_detail"].get(prize_name, 0) + 1
                )

                # 记录历史
                history_item = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "prize": prize_text,
                    "type": prize_type,
                    "balance": balance,
                }
                round_stats["history"].append(history_item)

                # 检查大奖
                if self._is_big_prize(prize_text, prize_type, prize_value):
                    big_prize_hit = True
                    stop_reason = f"命中大奖：{prize_text}"
                    logger.info(f"🏆 {stop_reason}")

                    # 立即通知（喜庆风格）
                    self._send_notification(
                        f"🎊✨ 恭喜恭喜！天选之人！✨🎊\n\n"
                        f"🏆 命中大奖：{prize_text}\n"
                        f"💰 当前余额：{balance:,} 憨豆\n"
                        f"🎲 本轮已抽：{draw_count} 次\n\n"
                        f"🎯 建议去买彩票，今天运势拉满！"
                    )
                    break

                # 定期校准余额
                if draw_count % self.BALANCE_CHECK_INTERVAL == 0:
                    real_balance, _ = self._fetch_balance()
                    if real_balance is not None:
                        balance = real_balance
                        logger.info(f"💰 余额校准：{balance:,}")

                    # 自动清理站内信
                    if self._clean_mail:
                        self._clean_messages()

                # 等待间隔
                time.sleep(current_interval)

            # 3. 最终清理站内信
            if self._clean_mail and draw_count > 0:
                self._clean_messages()

            # 4. 保存统计数据
            self._save_round_stats(round_stats, balance)

            # 5. 发送汇总通知
            if not big_prize_hit and self._notify:
                summary = self._build_summary(round_stats, balance, stop_reason)
                self._send_notification(summary)

        except Exception as e:
            logger.error(f"HHCLUB 抽奖任务异常：{e}", exc_info=True)
            self._send_notification(f"❌ HHCLUB 抽奖异常停止\n{e}")
        finally:
            self._running = False
            logger.info("🎰 HHCLUB 自动抽奖任务结束")

    def _do_draw(self) -> Optional[dict]:
        """
        执行一次抽奖请求

        Returns:
            dict: JSON 响应，或 None（失败）
        """
        url = f"{self._site_url}/plugin/lucky-draw"
        headers = {**self.DRAW_HEADERS, "User-Agent": self.DEFAULT_UA}

        try:
            req = RequestUtils(
                headers=headers,
                cookies=self._cookie,
            )
            res = req.post_res(url=url, data="")

            if res is None:
                logger.warning("抽奖请求返回 None")
                return None

            if hasattr(res, "json"):
                return res.json()
            else:
                # 兼容不同版本的 RequestUtils 返回值
                try:
                    text = res.text if hasattr(res, "text") else str(res)
                    return json.loads(text)
                except (json.JSONDecodeError, AttributeError):
                    logger.warning(f"抽奖响应解析失败：{res}")
                    return None

        except Exception as e:
            logger.error(f"抽奖请求异常：{e}")
            return None

    def _fetch_balance(self) -> Tuple[Optional[int], int]:
        """
        获取当前余额和单次消耗

        Returns:
            (余额, 单次消耗) 或 (None, 0)
        """
        url = f"{self._site_url}/lucky.php"
        headers = {
            "User-Agent": self.DEFAULT_UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            req = RequestUtils(
                headers=headers,
                cookies=self._cookie,
            )
            res = req.get_res(url=url)

            if res is None:
                return None, 0

            html = res.text if hasattr(res, "text") else str(res)

            # 解析余额：.bean-number
            balance = 0
            balance_match = re.search(
                r'class=["\']bean-number["\'][^>]*>([^<]+)<', html
            )
            if balance_match:
                balance_text = balance_match.group(1).strip()
                balance = self._parse_number(balance_text)

            # 解析单次消耗：.use-bean
            cost = 0
            cost_match = re.search(
                r'class=["\']use-bean["\'][^>]*>([^<]+)<', html
            )
            if cost_match:
                cost_text = cost_match.group(1).strip()
                cost = self._parse_number(cost_text)

            # 备选解析方式
            if balance == 0:
                # 尝试其他选择器
                alt_match = re.search(r'bean[^>]*>\s*(\d[\d,]*)', html)
                if alt_match:
                    balance = self._parse_number(alt_match.group(1))

            if cost == 0:
                alt_match = re.search(r'use[-_]?bean[^>]*>\s*(\d[\d,]*)', html)
                if alt_match:
                    cost = self._parse_number(alt_match.group(1))

            return balance, cost

        except Exception as e:
            logger.error(f"获取余额异常：{e}")
            return None, 0
    def _clean_messages(self):
        """
        清理站内信（包含"幸运大转盘"主题的信件）
        完全复刻油猴脚本 sweepLotteryMail + parseMailboxPage + isLotteryMail
        反复清第一页，因为删掉后后面的信会移到第一页
        """
        logger.info("🧹 开始清理站内信...")
        MAIL_KEYWORD = "幸运大转盘"
        MAX_ROUNDS = 20
        total_deleted = 0

        try:
            # 反复清第一页（复刻油猴脚本 sweepLotteryMail）
            for round_num in range(1, MAX_ROUNDS + 1):
                url = (
                    f"{self._site_url}/messages.php?"
                    f"action=viewmailbox&box=1&page=0"
                )
                headers = {
                    "User-Agent": self.DEFAULT_UA,
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }

                req = RequestUtils(
                    headers=headers,
                    cookies=self._cookie,
                )
                res = req.get_res(url=url)

                if res is None:
                    logger.warning("🧹 第一页请求失败（返回 None）")
                    break

                if hasattr(res, "status_code") and res.status_code in (301, 302):
                    logger.warning("🧹 Cookie 已过期，被重定向到登录页")
                    break

                html = res.text if hasattr(res, "text") else str(res)

                if len(html) < 100:
                    logger.warning(f"🧹 第一页内容过短（{len(html)} 字节）")
                    break

                # ── 解析信件（参考油猴脚本 parseMailboxPage）──
                # 每封信的结构：
                #   <input type="checkbox" name="messages[]" value="12345">
                #   ... 所在行里有 <a href="...viewmessage...">主题文本</a>
                #
                # 步骤1：按 <input name="messages[]" value="ID"> 切分页面
                # 步骤2：对每段，找最近的 viewmessage 链接文本

                # 用正则找到所有 input checkbox 的 value
                input_pattern = re.compile(
                    r'<input[^>]*name=["\']messages\[\]["\'][^>]*value=["\']?(\d+)["\']?',
                    re.IGNORECASE
                )
                # 找所有 viewmessage 链接及其文本
                link_pattern = re.compile(
                    r'<a[^>]*href=["\'][^"\']*viewmessage[^"\']*["\'][^>]*>([^<]+)</a>',
                    re.IGNORECASE
                )

                # 把页面按 input 切分，每段包含该 input 及其后续内容（到下一个 input 为止）
                input_matches = list(input_pattern.finditer(html))
                if not input_matches:
                    # 备用：value 在前 name 在后
                    input_pattern2 = re.compile(
                        r'<input[^>]*value=["\']?(\d+)["\']?[^>]*name=["\']messages\[\]',
                        re.IGNORECASE
                    )
                    input_matches = list(input_pattern2.finditer(html))

                if not input_matches:
                    logger.info(f"🧹 第 {round_num} 轮：第一页无信件，清理完成")
                    break

                # 对每个 input，取它到下一个 input 之间的文本，找 viewmessage 链接
                delete_ids = []
                for i, m in enumerate(input_matches):
                    msg_id = m.group(1)
                    # 取当前 input 到下一个 input 之间的文本
                    start = m.start()
                    end = input_matches[i + 1].start() if i + 1 < len(input_matches) else len(html)
                    segment = html[start:end]

                    # 在这段里找 viewmessage 链接的文本
                    link_match = link_pattern.search(segment)
                    if link_match:
                        subject = link_match.group(1).strip()
                        if MAIL_KEYWORD in subject:
                            delete_ids.append(msg_id)

                if not delete_ids:
                    logger.info(f"🧹 第 {round_num} 轮：第一页无「{MAIL_KEYWORD}」信件，清理完成")
                    break

                # 删除匹配的信件
                del_data = "action=moveordel"
                for mid in delete_ids:
                    del_data += f"&messages%5B%5D={mid}"
                del_data += "&delete=%E5%88%A0%E9%99%A4"  # "删除" URL 编码

                headers_post = {
                    "User-Agent": self.DEFAULT_UA,
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": url,
                }

                resp = RequestUtils(
                    headers=headers_post,
                    cookies=self._cookie,
                ).post_res(
                    url=f"{self._site_url}/messages.php",
                    data=del_data,
                )

                status = resp.status_code if resp and hasattr(resp, "status_code") else "?"
                total_deleted += len(delete_ids)
                logger.info(f"🧹 第 {round_num} 轮：删除 {len(delete_ids)} 封站内信（HTTP {status}）")
                time.sleep(0.5)

        except Exception as e:
            logger.error(f"清理站内信异常：{e}", exc_info=True)

        if total_deleted > 0:
            logger.info(f"🧹 共清理 {total_deleted} 封站内信")
        else:
            logger.info("🧹 未发现需要清理的站内信")

    def _parse_prize(self, prize_text: str) -> Tuple[str, str, int]:
        """
        解析奖品文本

        Args:
            prize_text: 原始奖品文本，如 "憨豆 × 2,000"、"VIP × 7 天"

        Returns:
            (奖品类型, 奖品名称, 数值)
        """
        if not prize_text:
            return "unknown", "未知", 0

        text = prize_text.strip()

        # 憨豆 / 魔力（都是豆子）
        if "憨豆" in text or "魔力" in text:
            value = self._extract_number(text)
            return "beans", f"魔力 {value:,}", value

        # VIP
        if "VIP" in text.upper() or "vip" in text:
            value = self._extract_number(text)
            if value > 0:
                return "vip", f"VIP × {value} 天", value
            return "vip", "VIP 会员", 1

        # 邀请卡
        if "邀请" in text:
            return "invite", "邀请卡", 1

        # 彩虹糖
        if "彩虹糖" in text:
            value = self._extract_number(text)
            return "rainbow", f"彩虹糖 × {value}", value

        # 补签卡
        if "补签" in text:
            return "makeup", "补签卡", 1

        # 上传量
        if "上传" in text or "GB" in text.upper():
            value = self._extract_number(text)
            return "upload", f"上传量 × {value} GB", value

        # 未识别，作为憨豆处理（VIP 可能被折算为 1000000 憨豆）
        value = self._extract_number(text)
        if value >= 1000000:
            return "beans", f"憨豆 × {value:,}（VIP折算）", value

        return "unknown", text, 0

    def _is_big_prize(
        self, prize_text: str, prize_type: str, prize_value: int
    ) -> bool:
        """
        判断是否为大奖

        大奖条件：
        - VIP 会员
        - 邀请卡
        - 大额憨豆（≥500000）
        - 命中自定义关键词
        """
        # VIP
        if prize_type == "vip":
            return True

        # 邀请卡
        if prize_type == "invite":
            return True

        # 大额憨豆
        if prize_type == "beans" and prize_value >= self.BIG_BEANS_THRESHOLD:
            return True

        # 自定义关键词匹配
        if self._big_prize_keywords:
            keywords = [
                kw.strip()
                for kw in self._big_prize_keywords.split(",")
                if kw.strip()
            ]
            for keyword in keywords:
                # 支持数字关键词（匹配大额）
                if keyword.isdigit():
                    if prize_value >= int(keyword):
                        return True
                elif keyword.upper() in prize_text.upper():
                    return True

        return False

    def _parse_number(self, text: str) -> int:
        """
        从文本中提取数字（支持千分位逗号）
        """
        if not text:
            return 0
        # 移除逗号和空格
        cleaned = text.replace(",", "").replace(" ", "").replace("，", "")
        match = re.search(r"(\d+)", cleaned)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return 0
        return 0

    def _extract_number(self, text: str) -> int:
        """
        从奖品文本中提取数字
        如 "憨豆 × 2,000" → 2000
        """
        return self._parse_number(text)

    # ======================== 数据管理 ========================

    def _load_data(self) -> dict:
        """
        从插件数据存储加载数据
        格式：{"stats": {...}, "history": [...]}
        """
        try:
            return self.get_data("hhlottery_data") or {}
        except Exception:
            return {}

    def _save_data(self, data: dict):
        """
        保存数据到插件数据存储
        """
        try:
            self.save_data("hhlottery_data", data)
        except Exception as e:
            logger.error(f"保存数据异常：{e}")

    def _save_round_stats(self, round_stats: dict, final_balance: int):
        """
        保存本轮统计到累计数据
        """
        data = self._load_data()
        stats = data.get("stats", {})
        history = data.get("history", [])

        # 更新累计统计
        stats["total_count"] = stats.get("total_count", 0) + round_stats["count"]
        stats["total_cost"] = stats.get("total_cost", 0) + round_stats["cost"]
        stats["total_wins"] = stats.get("total_wins", 0) + round_stats["wins"]
        stats["last_balance"] = final_balance

        # 合并奖品明细
        prize_detail = stats.get("prize_detail", {})
        for name, count in round_stats.get("prize_detail", {}).items():
            prize_detail[name] = prize_detail.get(name, 0) + count
        stats["prize_detail"] = prize_detail

        # 更新本轮
        stats["round"] = {
            "count": round_stats["count"],
            "cost": round_stats["cost"],
            "wins": round_stats["wins"],
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 合并历史（保留最近 100 条）
        history.extend(round_stats.get("history", []))
        history = history[-100:]

        data["stats"] = stats
        data["history"] = history

        self._save_data(data)

    def _update_stats_field(self, key: str, value):
        """
        更新统计字段
        """
        data = self._load_data()
        stats = data.get("stats", {})
        stats[key] = value
        data["stats"] = stats
        self._save_data(data)

    # ======================== 通知 ========================

    def _send_notification(self, message: str):
        """
        通过 MoviePilot 通知系统发送消息（Telegram/微信/飞书等）
        """
        if not self._notify:
            return

        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="HHCLUB 自动抽奖",
                text=message,
            )
        except Exception as e:
            logger.error(f"发送通知失败：{e}")

    def _build_summary(
        self,
        round_stats: dict,
        final_balance: int,
        stop_reason: str,
    ) -> str:
        """
        构建抽奖结束汇总消息
        """
        lines = ["🎰 HHCLUB 抽奖结束\n"]

        lines.append(f"🎲 完成次数：{round_stats['count']:,}")
        lines.append(f"💸 本轮消耗：{round_stats['cost']:,} 憨豆")
        lines.append(f"🫘 当前余额：{final_balance:,} 憨豆")

        if stop_reason:
            lines.append(f"⏹️ {stop_reason}")

        # 奖品统计
        prize_detail = round_stats.get("prize_detail", {})
        if prize_detail:
            lines.append(f"\n🎁 奖品统计：")
            for name, count in sorted(
                prize_detail.items(), key=lambda x: -x[1]
            ):
                lines.append(f"• {name} × {count}")

        return "\n".join(lines)

    # ======================== API 处理 ========================

    def _api_run_lottery(self, *args, **kwargs) -> dict:
        """
        API: 立即运行抽奖
        """
        if self._running:
            return {"success": False, "message": "抽奖任务正在运行中"}

        if not self._cookie:
            return {"success": False, "message": "未配置 Cookie"}

        import threading
        threading.Thread(target=self._lottery_job, daemon=True).start()
        return {"success": True, "message": "抽奖任务已启动"}

    def _api_get_stats(self, *args, **kwargs) -> dict:
        """
        API: 获取统计信息
        """
        data = self._load_data()
        return {
            "success": True,
            "data": data,
        }

    def stop_service(self):
        """
        停止插件服务
        """
        pass
