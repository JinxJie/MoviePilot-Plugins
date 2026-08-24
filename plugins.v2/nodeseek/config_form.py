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
            "component": "VTextField",
            "props": {
                "model": "cookie",
                "label": "Cookie",
                "placeholder": "粘贴 NodeSeek 登录 Cookie（如 nodeseek.com=xxx; ...）",
                "type": "textarea",
                "rows": 3,
                "clearable": True,
                "required": True,
            },
        },
        {
            "component": "VTextField",
            "props": {
                "model": "cron",
                "label": "签到时间（Cron 表达式）",
                "placeholder": "30 0 * * *",
                "hint": "每天 00:00 刷新签到，越早签到排名越靠前。默认 00:30，想抢前排可改为 5 0 * * *",
                "clearable": True,
            },
        },
        {
            "component": "VSwitch",
            "props": {
                "model": "notify_success",
                "label": "签到成功/已签到 通知",
                "hint": "签到成功或今日已签到时发送通知",
            },
        },
        {
            "component": "VSwitch",
            "props": {
                "model": "notify_fail",
                "label": "签到失败 通知",
                "hint": "签到失败或 Cookie 失效时发送通知",
            },
        },
        {
            "component": "VSwitch",
            "props": {
                "model": "use_proxy",
                "label": "使用系统代理",
                "hint": "通过 MoviePilot 系统代理访问 NodeSeek",
            },
        },
    ], {
        "cookie": "",
        "cron": "30 0 * * *",
        "notify_success": True,
        "notify_fail": True,
        "use_proxy": False,
    }
