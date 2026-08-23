"""配置页的 Vuetify JSON 纯拼装，不碰 self。"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """
    返回插件配置表单（Vuetify 组件）
    """

    # ── 基础设置 ──
    _base = {
        "component": "VCardTitle",
        "props": {"class": "d-flex align-center"},
        "content": [
            {"component": "VIcon", "props": {"color": "primary", "class": "mr-2"}, "text": "mdi-cog"},
            {"component": "span", "text": "基础设置"},
        ],
    }
    _divider = {"component": "VDivider"}

    def card(title_icon: str, title_color: str, title_text: str, rows: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"class": "mt-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
                    {"component": "VIcon", "props": {"color": title_color, "class": "mr-2"}, "text": title_icon},
                    {"component": "span", "text": title_text},
                ]},
                _divider,
                {"component": "VCardText", "content": rows},
            ],
        }

    def col(md: int, component: dict) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

    def textfield(model: str, label: str, **kwargs) -> dict:
        props = {"model": model, "label": label}
        for k, v in kwargs.items():
            props[k] = v
        return {"component": "VTextField", "props": props}

    def switch(model: str, label: str, color: str = "primary", **kwargs) -> dict:
        props = {"model": model, "label": label, "color": color}
        for k, v in kwargs.items():
            props[k] = v
        return {"component": "VSwitch", "props": props}

    def select(model: str, label: str, items: List[dict], **kwargs) -> dict:
        props = {"model": model, "label": label, "items": items}
        for k, v in kwargs.items():
            props[k] = v
        return {"component": "VSelect", "props": props}

    def textarea(model: str, label: str, **kwargs) -> dict:
        props = {"model": model, "label": label}
        for k, v in kwargs.items():
            props[k] = v
        return {"component": "VTextarea", "props": props}

    def alert(atype: str, text: str, **kwargs) -> dict:
        props = {"type": atype, "variant": "tonal", "text": text}
        for k, v in kwargs.items():
            props[k] = v
        return {"component": "VAlert", "props": props}

    def row(items: List[dict]) -> dict:
        return {"component": "VRow", "content": items}

    form = [
        {
            "component": "VForm",
            "content": [
                # ── 基础设置 ──
                card("mdi-cog", "primary", "基础设置", [
                    row([
                        col(3, switch("enabled", "启用插件", "primary")),
                        col(3, switch("notify", "开启通知", "info")),
                        col(3, switch("onlyonce", "立即运行一次", "success")),
                        col(3, switch("stop_current", "停止当前抽奖", "error",
                                       hint="保存后立刻收工，已抽成绩照常落盘", persistent_hint=True)),
                    ]),
                    row([
                        col(4, textfield("cron", "⏰ 定时表达式", placeholder="5 0 * * *",
                                         hint="留空则不自动运行", persistent_hint=True)),
                    ]),
                ]),

                # ── 站点设置 ──
                card("mdi-web", "warning", "站点设置", [
                    row([
                        col(4, select("cookie_source", "Cookie 来源", [
                            {"title": "手动填写", "value": "manual"},
                            {"title": "MoviePilot 站点管理", "value": "site"},
                        ], hint="选站点管理则从 MoviePilot 站点 Cookie 自动填充", persistent_hint=True)),
                        col(4, textfield("host", "站点域名", placeholder="hhanclub.net",
                                         hint="CookieCloud / 站点管理里的匹配域名", persistent_hint=True)),
                        col(4, textfield("site_url", "🌐 站点地址", placeholder="https://hhanclub.net")),
                    ]),
                    row([
                        col(12, textarea("cookie", "🔑 Cookie（来源选「手动填写」时必填）",
                                         placeholder="从浏览器 F12 复制完整 Cookie",
                                         rows=2, auto_grow=False,
                                         hint="选「手动填写」时必填；选「站点管理」则自动获取",
                                         persistent_hint=True)),
                    ]),
                ]),

                # ── 抽奖参数 ──
                card("mdi-slot-machine", "success", "抽奖参数", [
                    row([
                        col(3, textfield("interval", "⏱ 抽奖间隔（秒）", type="number", placeholder="8",
                                         hint="最小 3 秒", persistent_hint=True)),
                        col(3, textfield("max_count", "🎲 每次抽多少抽", type="number", placeholder="0=一抽到底",
                                         hint="0 = 一抽到底（保留线以下停）", persistent_hint=True)),
                        col(3, textfield("reserve_beans", "💰 保留憨豆", type="number", placeholder="0",
                                         hint="一抽到底时留多少不动", persistent_hint=True)),
                        col(3, switch("grand_stop", "大奖自动停", "warning")),
                    ]),
                    row([
                        col(3, switch("gambler_mode", "赌徒模式", "error",
                                       hint="忽略最大抽奖次数，一直抽到爆", persistent_hint=True)),
                        col(3, switch("clean_mail", "清理站内信", "info")),
                    ]),
                    row([
                        col(12, textfield("big_prize_keywords", "🏆 大奖关键词", placeholder="VIP,邀请,780000",
                                         hint="逗号分隔，匹配到则推送通知", persistent_hint=True)),
                    ]),
                ]),

                # ── 说明 ──
                card("mdi-information", "info", "说明", [
                    row([col(12, alert("info", "💡 若你也在使用油猴脚本，可配合使用 HHCLUB 自动抽奖 · 庆典版 https://greasyfork.org/zh-CN/scripts/591722"))]),
                ]),
            ],
        }
    ]

    default_config = {
        "enabled": False,
        "cron": "5 0 * * *",
        "cookie_source": "manual",
        "host": "hhanclub.net",
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
