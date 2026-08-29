"""癫影自动签到 - 配置页 Vuetify JSON 拼装"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """返回插件配置表单"""

    def card(title_icon: str, title_color: str, title_text: str, rows: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"class": "mt-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
                    {"component": "VIcon", "props": {"color": title_color, "class": "mr-2"}, "text": title_icon},
                    {"component": "span", "text": title_text},
                ]},
                {"component": "VDivider"},
                {"component": "VCardText", "content": rows},
            ],
        }

    def col(md: int, component: dict) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

    def _props(model: str, label: str, kwargs: dict) -> dict:
        props = {"model": model, "label": label}
        for k, v in kwargs.items():
            props[k.replace("_", "-")] = v
        return props

    def textfield(model: str, label: str, **kwargs) -> dict:
        return {"component": "VTextField", "props": _props(model, label, kwargs)}

    def switch(model: str, label: str, color: str = "primary", **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["color"] = color
        return {"component": "VSwitch", "props": props}

    def select(model: str, label: str, items: List[dict], **kwargs) -> dict:
        props = _props(model, label, kwargs)
        props["items"] = items
        return {"component": "VSelect", "props": props}

    def alert(atype: str, text: str, **kwargs) -> dict:
        props = {"type": atype, "variant": "tonal", "text": text}
        props.update(kwargs)
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
                        col(4, switch("enabled", "⚙️ 启用插件", "primary",
                                       hint="总开关，打开后才按定时计划自动签到", persistent_hint=True)),
                        col(8, switch("notify", "📬 签到结果通知", "success",
                                       hint="签到成功 / 已签到 / 失败时发送消息通知", persistent_hint=True)),
                    ]),
                    row([
                        col(6, textfield("email", "📧 登录邮箱",
                                           placeholder="you@example.com",
                                           hint="站点邮箱账号；填了邮箱+密码后，Token 过期会自动登录续期",
                                           persistent_hint=True, clearable=True)),
                        col(6, textfield("password", "🔒 登录密码",
                                           placeholder="登录密码",
                                           hint="仅保存在 MoviePilot 插件配置中，用于自动换新 Token",
                                           persistent_hint=True, clearable=True, type="password")),
                    ]),
                    row([
                        col(12, textfield("token", "🔑 登录 Token（可选）",
                                           placeholder="可留空；也可粘贴 __Host-portal_token",
                                           hint="有邮箱密码时可留空。手动粘贴仍可用；站点 JWT 实测约 1 天，插件会在过期前自动续期",
                                           persistent_hint=True, clearable=True)),
                    ]),
                ]),

                # ── 签到设置 ──
                card("mdi-calendar-check", "success", "签到设置", [
                    row([
                        col(4, switch("lucky_mode", "🎲 运气签模式", "warning",
                                       hint="开 = 运气签（49% 得 3~10 倍 / 10% 平手 / 20% 空签 / 21% 倒霉扣分）；关 = 普通签固定积分",
                                       persistent_hint=True)),
                        col(4, select("retry", "🔁 失败重试次数",
                                       items=[
                                           {"title": "不重试", "value": 0},
                                           {"title": "重试 1 次", "value": 1},
                                           {"title": "重试 3 次", "value": 3},
                                           {"title": "重试 5 次", "value": 5},
                                       ],
                                       hint="签到失败后的自动重试次数", persistent_hint=True)),
                        col(4, textfield("cron", "⏰ 定时表达式",
                                          placeholder="30 9 * * *",
                                          hint="Cron 表达式，默认每天 09:30", persistent_hint=True)),
                    ]),
                    row([
                        col(12, textfield("proxy", "🌐 自定义代理（可选）",
                                           placeholder="http://10.0.0.2:7890",
                                           hint="站点被 WAF 拦截（403）时填写代理地址，让签到流量换个出口 IP；留空则直连", persistent_hint=True, clearable=True)),
                        col(12, switch("use_system_proxy", "🖥️ 使用系统代理", "info",
                                        hint="使用 MoviePilot 设置 → 系统 → 代理服务器 中配置的代理；开启后忽略上方自定义代理", persistent_hint=True)),
                    ]),
                ]),

                # ── 调试 ──
                card("mdi-bug", "info", "调试", [
                    row([
                        col(12, switch("onlyonce", "🚀 立即运行一次", "info",
                                        hint="保存后立即执行一次签到（不影响定时计划），用于测试 Token 和网络连通性", persistent_hint=True)),
                    ]),
                ]),

                alert("info", "推荐填邮箱+密码：插件会在 Token 过期前自动登录续期（站点 JWT 实测约 1 天）。也可仍用浏览器 Cookie 里的 __Host-portal_token。当前站点人机验证（Turnstile）为关闭状态；若日后打开，自动登录可能失败。"),
            ],
        },
    ]

    return form, {
        "enabled": False,
        "token": "",
        "email": "",
        "password": "",
        "lucky_mode": False,
        "cron": "30 9 * * *",
        "retry": 3,
        "notify": True,
        "proxy": "",
        "use_system_proxy": True,
        "onlyonce": False,
    }
