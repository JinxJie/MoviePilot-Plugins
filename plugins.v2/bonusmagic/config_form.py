"""NexusPHP 魔力值自动兑换 - 配置页"""

from typing import Any, Dict, List, Tuple


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
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
                card("mdi-cog", "primary", "基础设置", [
                    row([
                        col(4, switch("enabled", "⚙️ 启用插件", "primary",
                                      hint="总开关。关闭后定时任务不会跑；立即运行一次仍可手动触发", persistent_hint=True)),
                        col(4, switch("notify", "📬 发送通知", "success",
                                      hint="任务结束或站点异常时发送通知", persistent_hint=True)),
                        col(4, switch("use_system_proxy", "🖥️ 使用系统代理", "info",
                                      hint="读取 MoviePilot 设置 → 系统 → 代理服务器；站点出口被拦时建议打开", persistent_hint=True)),
                    ]),
                    row([
                        col(6, {
                            "component": "VCronField",
                            "props": {
                                "model": "cron",
                                "label": "⏰ 定时执行",
                                "hint": "默认每天 10:00。Cron 表达式",
                                "persistent-hint": True,
                            },
                        }),
                        col(6, textfield("site_filter", "🎯 站点过滤（可选）",
                                         placeholder="留空=全部已配置站点；多个用逗号分隔，填站点名或域名",
                                         hint="只兑换这些站点。留空则扫描 MoviePilot 站点管理里全部启用站点",
                                         persistent_hint=True, clearable=True)),
                    ]),
                ]),
                card("mdi-swap-horizontal", "success", "兑换开关", [
                    row([
                        col(4, switch("enable_upload", "⬆️ 兑换上传量", "success",
                                      hint="解析到上传量兑换项时才执行，价格不写死", persistent_hint=True)),
                        col(4, switch("enable_download", "⬇️ 兑换下载量", "warning",
                                      hint="解析到下载量兑换项时才执行。多数站点不建议开", persistent_hint=True)),
                        col(4, select("item_prefer", "🎁 兑换档位",
                                      items=[
                                          {"title": "最便宜档", "value": "cheap"},
                                          {"title": "单次流量最大", "value": "max"},
                                          {"title": "性价比最高", "value": "efficient"},
                                      ],
                                      hint="同一类目有多档时怎么选。价格一律从页面动态解析", persistent_hint=True)),
                    ]),
                ]),
                card("mdi-strategy", "info", "兑换策略", [
                    row([
                        col(6, select("strategy", "📐 策略",
                                      items=[
                                          {"title": "保留余额后最大化", "value": "keep"},
                                          {"title": "最大化兑换", "value": "max"},
                                          {"title": "固定次数", "value": "fixed"},
                                      ],
                                      hint="keep=扣掉保留魔力后再尽量兑；max=能兑多少兑多少；fixed=按下面次数", persistent_hint=True)),
                        col(6, select("priority", "⚖️ 上传/下载优先级",
                                      items=[
                                          {"title": "按分享率自动", "value": "auto"},
                                          {"title": "优先上传", "value": "upload"},
                                          {"title": "优先下载", "value": "download"},
                                      ],
                                      hint="auto：分享率低于阈值先兑上传，否则先兑下载", persistent_hint=True)),
                    ]),
                    row([
                        col(4, textfield("keep_bonus", "💎 最低保留魔力",
                                         placeholder="10000",
                                         hint="余额低于此值停止兑换。解析不到价格时禁止兑换", persistent_hint=True)),
                        col(4, textfield("ratio_threshold", "📊 分享率阈值",
                                         placeholder="1.0",
                                         hint="优先级=自动 时生效。低于此值优先兑上传", persistent_hint=True)),
                        col(4, textfield("max_spend", "🛡️ 单站本次最多消耗魔力",
                                         placeholder="0=不限制",
                                         hint="单个站点单次任务消耗上限，0 表示不限制", persistent_hint=True)),
                    ]),
                    row([
                        col(3, textfield("fixed_upload", "⬆️ 固定上传次数",
                                         placeholder="0", hint="仅固定次数策略生效", persistent_hint=True)),
                        col(3, textfield("fixed_download", "⬇️ 固定下载次数",
                                         placeholder="0", hint="仅固定次数策略生效", persistent_hint=True)),
                        col(3, textfield("max_upload", "⬆️ 单站最多兑上传",
                                         placeholder="10", hint="单站点单次任务上传兑换上限", persistent_hint=True)),
                        col(3, textfield("max_download", "⬇️ 单站最多兑下载",
                                         placeholder="0", hint="单站点单次任务下载兑换上限，0=不允许下载或仅看开关", persistent_hint=True)),
                    ]),
                ]),
                card("mdi-layers-triple", "secondary", "站点架构", [
                    row([
                        col(6, select("architecture", "🧩 站点架构",
                                      items=[
                                          {"title": "自动识别（推荐）", "value": "auto"},
                                          {"title": "NexusPHP 传统架构", "value": "nexusphp"},
                                      ],
                                      hint="核心调度不绑死架构。自动识别失败时首期回退 NexusPHP。后续新架构只加适配器", persistent_hint=True)),
                        col(6, textfield("site_overrides", "🛠️ 站点特殊规则（可选）",
                                         placeholder="pt.example.com=architecture:nexusphp;catalog_path:mybonus.php",
                                         hint="每行一个站点：域名=architecture:nexusphp;catalog_path:mybonus.php。只改适配器参数，不改调度", persistent_hint=True, clearable=True)),
                    ]),
                ]),
                card("mdi-timer-sand", "warning", "限速与并发", [
                    row([
                        col(4, textfield("safety_buffer", "⏱️ 安全缓冲（秒）",
                                         placeholder="1.5",
                                         hint="在站点返回的等待秒数上额外加这么多。无限制站点不强制等 10 秒", persistent_hint=True)),
                        col(4, textfield("retry", "🔁 失败重试次数",
                                         placeholder="3",
                                         hint="网络失败或限制等待后的重试次数，达到上限则放弃该站", persistent_hint=True)),
                        col(4, textfield("concurrency", "🧵 站点并发数",
                                         placeholder="3",
                                         hint="多个站点同时跑；同一站点内部严格串行", persistent_hint=True)),
                    ]),
                ]),
                card("mdi-bug", "info", "调试", [
                    row([
                        col(12, switch("onlyonce", "🚀 立即运行一次", "info",
                                       hint="保存后立即执行一轮兑换（不受启用开关拦截），用于验证站点和 Cookie", persistent_hint=True)),
                    ]),
                ]),
                alert("warning", "本插件只兑换适配器能动态解析到价格的上传/下载项目。解析失败、登录失效或魔力不足会自动跳过该站，不影响其它站点。请自行评估站点规则后再用。"),
            ],
        },
    ]

    return form, {
        "enabled": False,
        "notify": True,
        "use_system_proxy": True,
        "cron": "0 10 * * *",
        "site_filter": "",
        "enable_upload": True,
        "enable_download": False,
        "item_prefer": "cheap",
        "strategy": "keep",
        "priority": "auto",
        "keep_bonus": "10000",
        "ratio_threshold": "1.0",
        "max_spend": "0",
        "fixed_upload": "0",
        "fixed_download": "0",
        "max_upload": "10",
        "max_download": "0",
        "safety_buffer": "1.5",
        "retry": "3",
        "concurrency": "3",
        "architecture": "auto",
        "site_overrides": "",
        "onlyonce": False,
    }
