"""PT 论坛签到的配置页声明式表单。

只负责拼装 Vuetify 组件树与默认值；配置读取、校验与调度都在 ``__init__.py``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

DEFAULTS: Dict[str, Any] = {
    # 全局
    "enabled": False,
    "notify_mode": "failure",
    "onlyonce": False,
    "refresh_now": False,
    "retry_count": 2,
    "retry_interval": 10,
    "jitter_minutes": 30,
    "history_days": 30,
    "use_proxy": False,
    # 药丸 invites.fun
    "inv_enabled": False,
    "inv_cookie": "",
    "inv_username": "",
    "inv_password": "",
    "inv_cron": "0 9 * * *",
    "inv_use_browser": False,
    # 蜂巢 fengchao.chat
    "fc_enabled": False,
    "fc_api_key": "",
    "fc_cron": "30 8 * * *",
    # 蓝影 hdblue.cc
    "hb_enabled": False,
    "hb_api_key": "",
    "hb_cron": "30 8 * * *",
}


def _field(component: str, model: str, label: str, **props) -> dict:
    return {"component": component, "props": {"model": model, "label": label, **props}}


def _row(*cols) -> dict:
    return {"component": "VRow", "content": list(cols)}


def _col(*content, **props) -> dict:
    props.setdefault("cols", 12)
    return {"component": "VCol", "props": props, "content": list(content)}


def _card(title: str, icon: str, rows: list, alert: str = "") -> dict:
    content = [
        {"component": "VCardTitle", "props": {"class": "d-flex align-center"}, "content": [
            {"component": "VIcon", "props": {"color": "info", "class": "mr-2"}, "text": icon},
            {"component": "span", "text": title},
        ]},
        {"component": "VDivider"},
        {"component": "VCardText", "content": rows},
    ]
    if alert:
        content.append({"component": "VCardText", "props": {"class": "pt-0"}, "content": [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": alert}},
        ]})
    return {"component": "VCard", "props": {"class": "mb-3", "variant": "outlined"}, "content": content}


def _cron_field(model: str, label: str, placeholder: str) -> dict:
    return _field("VCronField", model, label, placeholder=placeholder,
                  hint="五段 Cron；随机延迟按全局设置叠加",
                  **{"prepend-inner-icon": "mdi-clock-outline", "persistent-hint": True})


def build_form() -> Tuple[List[dict], Dict[str, Any]]:
    """返回配置页组件树与默认值。"""
    form = [{"component": "VForm", "content": [
        # 全局设置
        _card("全局设置", "mdi-cog", [
            _row(
                _col(_field("VSwitch", "enabled", "启用定时签到",
                            hint="总开关；关闭后全部论坛停止定时签到", **{"persistent-hint": True}), md=3),
                _col(_field("VSwitch", "onlyonce", "保存后立即签到一次",
                            hint="执行后自动关闭", **{"persistent-hint": True}), md=3),
                _col(_field("VSwitch", "refresh_now", "保存后刷新论坛信息（只读）",
                            hint="只拉取账号与签到数据，不签到", **{"persistent-hint": True}), md=3),
                _col(_field("VSelect", "notify_mode", "结果通知",
                            items=[{"title": "仅失败", "value": "failure"},
                                   {"title": "全部结果", "value": "all"},
                                   {"title": "关闭", "value": "off"}],
                            hint="仅失败：只通知失败；全部结果：成功/已签到/失败均通知",
                            **{"persistent-hint": True}), md=3),
            ),
            _row(
                _col(_field("VTextField", "jitter_minutes", "随机延迟上限（分钟）",
                            type="number", min=0, max=60,
                            hint="0-60；每个论坛在 Cron 后随机延迟", **{"persistent-hint": True}), md=3),
                _col(_field("VTextField", "retry_count", "失败重试次数（0-5）",
                            type="number", min=0, max=5,
                            hint="含首次共 N+1 次，当日封顶", **{"persistent-hint": True}), md=3),
                _col(_field("VTextField", "retry_interval", "失败重试间隔（分钟）",
                            type="number", min=1,
                            hint="失败后延迟 N 分钟重试", **{"persistent-hint": True}), md=3),
                _col(_field("VTextField", "history_days", "历史记录保留天数",
                            type="number", min=7, max=365,
                            hint="首页记录与本地历史的保留时长", **{"persistent-hint": True}), md=3),
            ),
            _row(
                _col(_field("VSwitch", "use_proxy", "使用系统代理（默认关闭，应用到全部论坛）",
                            hint="开启后三个论坛全部走 MoviePilot 系统代理；关闭则全部直连",
                            **{"persistent-hint": True}), md=3),
            ),
        ], alert="日志排查：运行详情请查看 MoviePilot 日志，搜索“论坛签到·”，每个阶段（会话/页面/状态查询/签到提交）均有状态码与耗时。"),
        # 药丸
        _card("药丸论坛（invites.fun）", "mdi-pill", [
            _row(
                _col(_field("VSwitch", "inv_enabled", "启用药丸签到",
                            hint="独立开关；关闭只跳过药丸，不影响其他论坛",
                            **{"persistent-hint": True}), md=3),
                _col(_cron_field("inv_cron", "药丸签到周期", "0 9 * * *"), md=6),
                _col(_field("VSwitch", "inv_use_browser", "浏览器仿真（绕 CF）",
                            hint="普通请求被人机验证拦截时开启；需宿主已装 cloakbrowser，失败自动降级普通请求",
                            **{"persistent-hint": True}), md=3),
            ),
            _row(
                _col(_field("VTextField", "inv_cookie", "药丸 Cookie（需含 flarum_remember）",
                            type="password", autocomplete="new-cookie", clearable=True,
                            placeholder="登录勾选“记住我”后复制 Cookie",
                            hint="首选签到方式；remember 值变化时自动回写本配置", **{"persistent-hint": True}), md=6),
                _col(_field("VTextField", "inv_username", "药丸用户名",
                            autocomplete="new-username", clearable=True,
                            hint="可选；Cookie 失效或未配置时的登录兜底", **{"persistent-hint": True}), md=3),
                _col(_field("VTextField", "inv_password", "药丸密码",
                            type="password", autocomplete="new-password", clearable=True,
                            hint="与用户名配套，勾选记住我等效", **{"persistent-hint": True}), md=3),
            ),
        ], alert="Cookie 签到失败或未配置时，自动尝试账号密码登录；两者都未配置则该论坛跳过。签到后自动刷新会话，Cookie 中的 remember 值有变化时会回写插件配置。"),
        # 蜂巢
        _card("蜂巢论坛（fengchao.chat）", "mdi-hexagon-multiple", [
            _row(
                _col(_field("VSwitch", "fc_enabled", "启用蜂巢签到",
                            hint="独立开关；关闭只跳过蜂巢，不影响其他论坛",
                            **{"persistent-hint": True}), md=3),
                _col(_cron_field("fc_cron", "蜂巢签到周期", "30 8 * * *"), md=7),
            ),
            _row(
                _col(_field("VTextField", "fc_api_key", "蜂巢 MP 专用 API Key",
                            type="password", autocomplete="off", clearable=True,
                            placeholder="论坛“隐秘的角落”生成",
                            hint="Bearer Key 鉴权；签到后自动拉取账号卡与近 30 天签到奖励",
                            **{"persistent-hint": True}), md=6),
            ),
        ], alert="在蜂巢论坛“隐秘的角落”生成 MoviePilot 专用 API Key 并粘贴；签到、账号信息经官方集成 API 完成。"),
        # 蓝影
        _card("蓝影论坛（hdblue.cc）", "mdi-movie-filter", [
            _row(
                _col(_field("VSwitch", "hb_enabled", "启用蓝影签到",
                            hint="独立开关；关闭只跳过蓝影，不影响其他论坛",
                            **{"persistent-hint": True}), md=3),
                _col(_cron_field("hb_cron", "蓝影签到周期", "30 8 * * *"), md=7),
            ),
            _row(
                _col(_field("VTextField", "hb_api_key", "蓝影签到密钥",
                            type="password", autocomplete="off", clearable=True,
                            placeholder="hdbmp_…",
                            hint="论坛个人设置 → MoviePilot 签到中生成；首页可展示近 30 天奖励",
                            **{"persistent-hint": True}), md=6),
            ),
        ], alert="在蓝影论坛个人设置 → MoviePilot 签到中生成专用密钥；签到以北京时间为准，不会自动补签往日。"),
        # 使用说明
        {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text":
            "每个论坛独立启用、独立 Cron（随机延迟按全局设置叠加），定时任务互不等待；"
            "“保存后立即签到一次”按 药丸 → 蜂巢 → 蓝影 顺序串行执行；"
            "任一论坛失败按全局重试设置自动重试，当日次数用尽后停止。"
            "插件首页仅展示缓存结果，不会因打开页面发起签到。"}},
    ]}]
    return form, dict(DEFAULTS)
