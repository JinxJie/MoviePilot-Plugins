"""PT 论坛签到：药丸（invites.fun）、蜂巢（fengchao.chat）、蓝影（hdblue.cc）统一自动签到。

每个论坛独立启用、独立 Cron（叠加全局随机延迟）；任一论坛失败按全局重试设置延迟重试，
当日尝试次数用尽后停止。签到协议实现在 ``clients.py``（对照各上游插件逐条核实）。
"""
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas.types import MessageType
from apscheduler.triggers.cron import CronTrigger

from .clients import FengchaoClient, ForumError, HDBlueClient, InvitesClient
from .config_form import DEFAULTS, build_form

FORUM_KEYS = ("invites", "fengchao", "hdblue")
FORUM_TITLES = {"invites": "药丸", "fengchao": "蜂巢", "hdblue": "蓝影"}
FORUM_ICONS = {"invites": "💊", "fengchao": "🐝", "hdblue": "🎬"}
STATUS_TITLES = {"success": "✅ 签到成功", "already": "📅 今日已签到", "failed": "❌ 签到失败"}
MAX_HISTORY = 300


def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_cron(value, default: str) -> str:
    candidate = str(value or "").strip()
    try:
        CronTrigger.from_crontab(candidate)
        return candidate
    except (TypeError, ValueError):
        return default


def _cron_trigger(expression: str, jitter_seconds: int = 0) -> CronTrigger:
    """直接构造 CronTrigger 以支持 jitter；``from_crontab`` 不接受该参数。"""
    minute, hour, day, month, day_of_week = str(expression).split()
    return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=day_of_week, timezone=getattr(settings, "TZ", None),
                       jitter=jitter_seconds or 0)


def _system_proxy() -> str:
    """读取 MoviePilot 系统代理，兼容字典与对象形式；读不到时回退环境变量。"""
    raw = getattr(settings, "PROXY", None)
    value = None
    if isinstance(raw, dict):
        value = raw.get("https") or raw.get("http")
    else:
        value = getattr(raw, "host", None)
    return str(value or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "").strip()


def _fmt(value) -> str:
    return "—" if value in (None, "") else str(value)


class ForumSignin(_PluginBase):
    plugin_name = "PT 论坛签到"
    plugin_desc = "药丸、蜂巢、蓝影三 PT 论坛统一自动签到：独立启用与周期、失败自动重试、按论坛展示签到数据与结果通知。"
    plugin_icon = "https://raw.githubusercontent.com/JinxJie/MoviePilot-Plugins/main/icons/forumsignin.png"
    plugin_version = "3.0.0"
    plugin_author = "JinxJie"
    author_url = "https://github.com/JinxJie"
    plugin_config_prefix = "forumsignin_"
    plugin_order = 0
    auth_level = 2

    def __init__(self):
        super().__init__()
        self._forum_locks: Dict[str, threading.Lock] = {key: threading.Lock() for key in FORUM_KEYS}
        self._retry_timers: Dict[str, threading.Timer] = {}
        # 共享签到历史一把锁：read-modify-save 期间串行，防并发论坛互相覆盖。
        self._history_lock = threading.Lock()
        # 停止标记：stop_service 后，运行中的旧批次不再落库、不再排新重试。
        self._stopped = False
        # 在途执行线程登记：stop_service 等待其收敛，避免停止后仍有写库动作。
        self._inflight_lock = threading.Lock()
        self._inflight_threads = set()
        self._enabled = False
        self._notify_mode = "failure"
        self._retry_count = 2
        self._retry_interval = 10
        self._jitter_minutes = 30
        self._history_days = 30
        self._forums: Dict[str, Dict[str, Any]] = {}
        self._fc_instance_id = ""

    # ---- 配置 ----

    def init_plugin(self, config: dict = None):
        config = config or {}
        # 重新加载配置即视为新批次恢复运行。
        self._stopped = False
        self._enabled = bool(config.get("enabled", False))
        self._notify_mode = str(config.get("notify_mode") or "failure")
        if self._notify_mode not in ("all", "failure", "off"):
            self._notify_mode = "failure"
        self._retry_count = _clamp_int(config.get("retry_count"), 2, 0, 5)
        self._retry_interval = _clamp_int(config.get("retry_interval"), 10, 1, 240)
        self._jitter_minutes = _clamp_int(config.get("jitter_minutes"), 30, 0, 60)
        self._history_days = _clamp_int(config.get("history_days"), 30, 7, 365)
        # 全局代理开关：默认关闭，开启后应用到全部论坛。
        self._use_proxy = bool(config.get("use_proxy", False))
        self._forums = {
            "invites": {
                "enabled": bool(config.get("inv_enabled", False)),
                "cookie": str(config.get("inv_cookie") or "").strip(),
                "username": str(config.get("inv_username") or "").strip(),
                "password": str(config.get("inv_password") or ""),
                "cron": _safe_cron(config.get("inv_cron"), DEFAULTS["inv_cron"]),
                "use_browser": bool(config.get("inv_use_browser", False)),
            },
            "fengchao": {
                "enabled": bool(config.get("fc_enabled", False)),
                "api_key": str(config.get("fc_api_key") or "").strip(),
                "cron": _safe_cron(config.get("fc_cron"), DEFAULTS["fc_cron"]),
            },
            "hdblue": {
                "enabled": bool(config.get("hb_enabled", False)),
                "api_key": str(config.get("hb_api_key") or "").strip(),
                "cron": _safe_cron(config.get("hb_cron"), DEFAULTS["hb_cron"]),
            },
        }
        # 蜂巢按实例标识绑定账号展示；UUID 稳定持久化，避免每次保存生成新实例。
        self._fc_instance_id = str(self.get_data("fengchao_instance_id") or "")
        if not self._fc_instance_id:
            self._fc_instance_id = str(uuid.uuid4())
            self.save_data("fengchao_instance_id", self._fc_instance_id)
        # 一次性开关统一清除后一次保存：两个都开时避免第二次保存复活 onlyonce。
        one_shot = {"onlyonce": bool(config.get("onlyonce")),
                    "refresh_now": bool(config.get("refresh_now"))}
        if any(one_shot.values()):
            updated = dict(config)
            updated.update({key: False for key in one_shot})
            try:
                self.update_config(updated)
            except Exception:
                logger.warning("PT 论坛签到：一次性开关持久化失败，本次不执行任何即时任务")
                return
        if one_shot["onlyonce"]:
            timer = threading.Timer(3, self._run_all)
            timer.daemon = True
            self._retry_timers["onlyonce"] = timer
            timer.start()
        if one_shot["refresh_now"]:
            timer = threading.Timer(3, self._refresh_all)
            timer.daemon = True
            self._retry_timers["refresh"] = timer
            timer.start()

    def _track_thread(self):
        with self._inflight_lock:
            self._inflight_threads.add(threading.current_thread())

    def _untrack_thread(self):
        with self._inflight_lock:
            self._inflight_threads.discard(threading.current_thread())

    def _forum_ready(self, key: str) -> bool:
        forum = self._forums.get(key) or {}
        if not forum.get("enabled"):
            return False
        if key == "invites":
            return ("flarum_remember=" in str(forum.get("cookie") or "")
                    or bool(forum.get("username") and forum.get("password")))
        return bool(forum.get("api_key"))

    def _build_client(self, key: str):
        """按论坛构造客户端；全局代理开关应用到全部论坛。"""
        forum = self._forums[key]
        proxy = _system_proxy() if self._use_proxy else ""
        log = lambda message: logger.info(f"论坛签到·{FORUM_TITLES[key]}：{message}")  # noqa: E731
        if key == "invites":
            return InvitesClient(cookie=forum.get("cookie", ""),
                                 username=forum.get("username", ""),
                                 password=forum.get("password", ""),
                                 use_browser=forum.get("use_browser", False),
                                 proxy=proxy, on_cookie_update=self._on_invites_cookie,
                                 log=log)
        if key == "fengchao":
            return FengchaoClient(api_key=forum.get("api_key", ""),
                                  instance_id=self._fc_instance_id,
                                  plugin_version=self.plugin_version,
                                  moviepilot_version=str(getattr(settings, "VERSION_FLAG", "") or ""),
                                  proxy=proxy, log=log)
        return HDBlueClient(api_key=forum.get("api_key", ""), proxy=proxy, log=log)

    def _on_invites_cookie(self, remember: str, session_value: str):
        """Cookie 中的 remember 值变化时回写配置；session 每次都会刷新，不据此触发写盘。"""
        forum = self._forums.get("invites") or {}
        current = re.search(r"flarum_remember=([^;\s]+)", str(forum.get("cookie") or ""))
        if current and current.group(1) == remember:
            return
        if self._stopped:
            logger.info("论坛签到·药丸：插件已停止，放弃本次 Cookie 回写")
            return
        updated_cookie = f"flarum_remember={remember}; flarum_session={session_value}"
        forum["cookie"] = updated_cookie
        logger.info("论坛签到·药丸：remember Cookie 已变化，回写插件配置")
        try:
            # 宿主 _PluginBase 提供的是 get_config()；无 get_config_dict。
            config = self.get_config()
            config["inv_cookie"] = updated_cookie
            self.update_config(config)
        except Exception as exc:
            logger.warning(f"论坛签到·药丸：Cookie 回写配置失败（{type(exc).__name__}），不影响本次签到结果")

    # ---- 调度与执行 ----

    def get_state(self) -> bool:
        return self._enabled and any(self._forum_ready(key) for key in FORUM_KEYS)

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        services = []
        jitter_seconds = self._jitter_minutes * 60
        for key in FORUM_KEYS:
            if not self._forum_ready(key):
                continue
            try:
                trigger = _cron_trigger(self._forums[key]["cron"], jitter_seconds)
            except (ValueError, IndexError):
                continue
            services.append({
                "id": f"ForumSignin_{key.capitalize()}",
                "name": f"论坛签到·{FORUM_TITLES[key]}",
                "trigger": trigger,
                "func": self._run_forum,
                # 宿主用 "func_kwargs" 传给执行函数；"kwargs" 是 add_job 调度选项（宿主 reconcile.py）。
                "func_kwargs": {"forum_key": key},
                "kwargs": {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
            })
        return services

    def _run_all(self):
        """立即运行一次：串行执行全部启用论坛，避免并发连接互相挤压。"""
        self._track_thread()
        try:
            ready = [key for key in FORUM_KEYS if self._forum_ready(key)]
            if not ready:
                logger.info("PT 论坛签到：立即执行时没有启用的论坛，跳过")
                return
            logger.info("PT 论坛签到：立即执行 " + "、".join(FORUM_TITLES[key] for key in ready)
                        + f"（共 {len(ready)} 个论坛）")
            for key in ready:
                self._run_forum(key, manual=True)
        finally:
            self._untrack_thread()

    def _attempts(self, key: str) -> int:
        state = self.get_data(f"retry_{key}")
        if not isinstance(state, dict) or state.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 0
        return _clamp_int(state.get("count"), 0, 0, 999)

    def _bump_attempts(self, key: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.get_data(f"retry_{key}")
        if not isinstance(state, dict) or state.get("date") != today:
            state = {"date": today, "count": 0}
        state["count"] = _clamp_int(state.get("count"), 0, 0, 999) + 1
        self.save_data(f"retry_{key}", state)

    def _schedule_retry(self, key: str, error: ForumError = None):
        """失败后延迟重试；当日尝试（首次+重试）用尽即停，跨日自动恢复。

        站点 Retry-After（429/HTTP 日期）优先于全局间隔，超出当日上限时截断。
        """
        title = FORUM_TITLES[key]
        attempts = self._attempts(key)
        budget = self._retry_count + 1
        if self._stopped:
            logger.info(f"论坛签到·{title}：插件已停止，不再安排重试")
            return
        if self._retry_count == 0 or attempts >= budget:
            logger.info(f"论坛签到·{title}：当日尝试已达 {attempts} 次，停止重试")
            return
        pending = self._retry_timers.get(key)
        if pending and pending.is_alive():
            return  # 已有待执行的重试任务（回调触发时会先从表中摘除自身）
        delay_seconds = max(60, self._retry_interval * 60)
        retry_after = getattr(error, "retry_after", 0) or 0
        if retry_after:
            # 完全遵循站点要求的等待下限，不提前重试（不再截断）。
            logger.info(f"论坛签到·{title}：站点要求等待 {retry_after} 秒，按站点要求安排重试")
            delay_seconds = max(delay_seconds, retry_after)

        def _fire():
            # 回调已进入执行：先从待执行表摘除自身，否则存活判断会误挡下一次重试。
            self._retry_timers.pop(key, None)
            self._run_forum(forum_key=key)

        timer = threading.Timer(delay_seconds, _fire)
        timer.daemon = True
        self._retry_timers[key] = timer
        timer.start()
        logger.info(f"论坛签到·{title}：{delay_seconds // 60} 分钟后重试"
                    f"（当日尝试 {attempts}/{budget}）")

    def _refresh_all(self):
        """只读刷新全部启用论坛的账号与签到数据（不签到）。"""
        if self._stopped:
            logger.info("PT 论坛签到：插件已停止，取消信息刷新")
            return
        self._track_thread()
        try:
            outcomes = {}
            for key in FORUM_KEYS:
                if self._forum_ready(key):
                    outcomes[key] = self._refresh_forum(key)
        finally:
            self._untrack_thread()
        self._notify_refresh_outcomes(outcomes)

    def _notify_refresh_outcomes(self, outcomes: Dict[str, str]):
        """刷新结果通知遵循通知模式：全部结果=汇总；仅失败=只报刷新失败的论坛。"""
        if not outcomes or self._notify_mode == "off":
            return
        if self._notify_mode == "all":
            reported = outcomes
        else:
            reported = {key: outcome for key, outcome in outcomes.items()
                        if outcome.startswith("刷新失败")}
            if not reported:
                return  # 仅失败模式：刷新全部成功时不打扰
        lines = [f"{FORUM_ICONS[key]} {FORUM_TITLES[key]}：{outcome}"
                 for key, outcome in reported.items()]
        try:
            self.post_message(mtype=MessageType.Plugin, title="【PT 论坛签到】论坛信息已刷新",
                              text="\n".join(lines))
        except Exception:
            logger.warning("PT 论坛签到：刷新结果通知发送失败")

    def _refresh_forum(self, forum_key: str) -> str:
        """只读拉取单个论坛的账号卡与签到数据并合并进缓存；返回给通知的结果说明。"""
        if forum_key not in self._forum_locks:
            return "不支持"
        lock = self._forum_locks[forum_key]
        if not lock.acquire(blocking=False):
            return "已有任务在执行"
        title = FORUM_TITLES[forum_key]
        self._track_thread()
        try:
            if self._stopped:
                return "插件已停止"
            if not self._forum_ready(forum_key):
                return "未启用或未配置凭证"
            try:
                profile = self._build_client(forum_key).status()
            except ForumError as error:
                logger.warning(f"论坛签到·{title}：信息刷新失败：{error}")
                return f"刷新失败（{error}）"
            except Exception as exc:
                logger.warning(f"论坛签到·{title}：信息刷新异常 {type(exc).__name__}")
                return "刷新失败（网络或配置异常）"
            if self._stopped:
                return "插件已停止"
            now = datetime.now()
            state = self.get_data(f"state_{forum_key}")
            state = state if isinstance(state, dict) else {}
            # 只合并展示数据；最近一次签到结论（status/message）保持不变。
            state.update({key: value for key, value in profile.items()
                          if key not in ("status", "message")})
            # 刷新得到的 checked_today 属于今天。
            state["sign_date"] = now.strftime("%Y-%m-%d")
            state["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.save_data(f"state_{forum_key}", state)
            checked = state.get("checked_today")
            summary = "今日已签到" if checked is True else ("今日尚未签到" if checked is False else "数据已刷新")
            logger.info(f"论坛签到·{title}：信息已刷新（{summary}）")
            return summary
        finally:
            self._untrack_thread()
            lock.release()

    def _run_forum(self, forum_key: str, manual: bool = False):
        if forum_key not in self._forum_locks:
            return
        lock = self._forum_locks[forum_key]
        if not lock.acquire(blocking=False):
            logger.info(f"论坛签到·{FORUM_TITLES[forum_key]}：已有任务在执行，跳过本次触发")
            return
        title = FORUM_TITLES[forum_key]
        started = time.monotonic()
        self._track_thread()
        try:
            if self._stopped:
                logger.info(f"论坛签到·{title}：插件已停止，取消本次签到")
                return
            if not manual and not self._enabled:
                logger.info(f"论坛签到·{title}：定时触发但插件未启用，跳过")
                return
            if not self._forum_ready(forum_key):
                logger.info(f"论坛签到·{title}：未启用或未配置凭证，跳过")
                return
            attempts = self._attempts(forum_key)
            if attempts >= self._retry_count + 1:
                logger.info(f"论坛签到·{title}：当日尝试已达 {attempts} 次上限，跳过")
                return
            self._bump_attempts(forum_key)
            forum = self._forums.get(forum_key) or {}
            logger.info(f"论坛签到·{title}：开始签到（触发={'手动' if manual else '定时'}，"
                        f"当日第 {attempts + 1}/{self._retry_count + 1} 次，"
                        f"代理={'开' if self._use_proxy else '关'}"
                        + (f"，浏览器仿真={'开' if forum.get('use_browser') else '关'}"
                           if forum_key == "invites" else "") + "）")
            try:
                client = self._build_client(forum_key)
                result = client.signin()
            except ForumError as error:
                logger.warning(f"论坛签到·{title}：签到失败（耗时 {time.monotonic() - started:.1f} 秒）：{error}")
                if not self._stopped:
                    self._record_result(forum_key, self._failed(str(error)))
                    self._schedule_retry(forum_key, error)
                return
            except Exception as exc:
                # 网络异常原文可能含 Cookie/代理地址，仅记录类型与所在阶段。
                logger.warning(f"论坛签到·{title}：执行异常 {type(exc).__name__}（耗时 {time.monotonic() - started:.1f} 秒）")
                if not self._stopped:
                    self._record_result(forum_key, self._failed("执行异常，请检查网络与凭证配置"))
                    self._schedule_retry(forum_key)
                return
            if self._stopped:
                logger.info(f"论坛签到·{title}：签到返回时插件已停止，丢弃本次结果")
                return
            self._record_result(forum_key, result, duration=time.monotonic() - started)
            # 成功/已签到即清空当日重试预算，明日自然恢复。
            self.save_data(f"retry_{forum_key}", None)
        finally:
            self._untrack_thread()
            lock.release()

    @staticmethod
    def _failed(message: str) -> dict:
        return {"status": "failed", "message": message, "reward": None,
                "points": None, "streak": None, "account": None}

    # ---- 记录与通知 ----

    def _record_result(self, forum_key: str, result: dict, duration: float = 0.0):
        title = FORUM_TITLES[forum_key]
        now = datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        status = str(result.get("status") or "failed")
        message = str(result.get("message") or "")
        self.save_data(f"state_{forum_key}", {**result, "updated_at": now_text,
                                              "sign_date": now.strftime("%Y-%m-%d")})
        # 共享历史为 read-modify-save：多论坛并发完成时加锁串行，防止互相覆盖。
        with self._history_lock:
            history = self.get_data("history")
            if not isinstance(history, list):
                history = []
            history.insert(0, {"forum": title, "time": now_text, "status": status,
                               "message": message, "reward": result.get("reward"),
                               "points": result.get("points"), "streak": result.get("streak")})
            cutoff = (now - timedelta(days=self._history_days)).strftime("%Y-%m-%d %H:%M:%S")
            history = [row for row in history
                       if isinstance(row, dict) and str(row.get("time") or "") >= cutoff][:MAX_HISTORY]
            self.save_data("history", history)
        logger.info(f"论坛签到·{title}：{STATUS_TITLES.get(status, status)}"
                    f"（{message}；耗时 {duration:.1f} 秒；奖励={_fmt(result.get('reward'))}，"
                    f"积分={_fmt(result.get('points'))}，连续={_fmt(result.get('streak'))}）")
        self._notify_result(forum_key, status, message, result)

    def _notify_result(self, forum_key: str, status: str, message: str, result: dict):
        """结果通知，模板对齐癫影签到插件（标题带日期，正文分区行）。"""
        if self._notify_mode == "off":
            return
        if self._notify_mode == "failure" and status != "failed":
            return
        name = FORUM_TITLES[forum_key]
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        balance_label = "当前余额" if forum_key == "invites" else "当前积分"
        points_text = _fmt(result.get("points"))
        if forum_key == "hdblue" and result.get("point_name"):
            points_text += f" {result['point_name']}"
        streak_text = _fmt(result.get("streak"))
        if forum_key == "hdblue" and result.get("max_streak") is not None:
            streak_text += f" / 最长 {result['max_streak']}"
        if status == "success":
            title = f"{name}签到成功（{today}）"
            lines = [
                f"📅 {name}签到成功",
                "",
                f"🧧 获得奖励：{_fmt(result.get('reward'))}",
                f"💰 {balance_label}：{points_text}",
                f"🔥 连续签到：{streak_text}",
                "",
                f"🕒 {now}",
            ]
        elif status == "already":
            title = f"{name}已签到（{today}）"
            lines = [
                "📅 今日已完成签到，无需重复操作",
                "",
                f"💰 {balance_label}：{points_text}",
                f"🔥 连续签到：{streak_text}",
                "",
                f"🕒 {now}",
            ]
        else:
            attempts = self._attempts(forum_key)
            title = f"{name}签到失败（{today}）"
            credential_hint = {
                "invites": "请检查 Cookie 或账号密码是否有效",
                "fengchao": "请检查 API Key 是否有效",
                "hdblue": "请检查签到密钥是否有效",
            }.get(forum_key, "请检查凭证是否有效或网络是否可达")
            lines = [
                f"❌ 失败原因：{message or '未知'}",
                f"🔁 当日已尝试 {attempts} 次",
                "",
                f"⚠️ {credential_hint}",
                "",
                f"🕒 {now}",
            ]
        try:
            self.post_message(mtype=MessageType.Plugin, title=title, text="\n".join(lines))
        except Exception:
            logger.warning(f"论坛签到·{name}：通知发送失败")

    # ---- 插件页 ----

    @staticmethod
    def _today_text(state: dict) -> str:
        """今日签到状态：只认属于今天的缓存，昨天的结论不能代表今天。"""
        fresh = state.get("sign_date") == datetime.now().strftime("%Y-%m-%d")
        checked = state.get("checked_today") if fresh else None
        if checked is True:
            return "已签到"
        if checked is False:
            return "未签到"
        status = state.get("status") if fresh else None
        if status in ("success", "already"):
            return "已签到"
        if status == "failed":
            return "未知（签到失败）"
        return "未签到"

    def _status_chip(self, state: dict) -> tuple:
        """状态章：今天的实时签到状态（checked_today）优先于本地签到结论。"""
        fresh = state.get("sign_date") == datetime.now().strftime("%Y-%m-%d")
        status = state.get("status")
        checked = state.get("checked_today") if fresh else None
        if checked is True:
            return "今日已签到", "success"
        if checked is False:
            # 实时确认今日未签到；仅当今天本地结论为失败时才保留失败口径
            if fresh and status == "failed":
                return "签到失败", "error"
            return "今日未签到", "warning"
        if status == "failed":
            return ("签到失败", "error") if fresh else ("上次签到失败", "error")
        if status in STATUS_TITLES and fresh:
            return STATUS_TITLES[status], "success"
        if status in ("success", "already"):
            return "今日未签到", "warning"
        return "尚未执行", "grey"

    def _points_text(self, key: str, state: dict) -> str:
        points_text = _fmt(state.get("points"))
        if key == "hdblue" and state.get("point_name"):
            points_text += f" {state['point_name']}"
        return points_text

    def _forum_overview_rows(self, key: str, state: dict) -> List[tuple]:
        account_text = _fmt(state.get("account"))
        if key == "fengchao" and state.get("level_name"):
            account_text += f"（{state.get('level_icon') or ''}{state['level_name']}）"
        reward = state.get("reward")
        if reward in (None, ""):
            reward = state.get("today_reward")
        streak_text = _fmt(state.get("streak"))
        if key == "hdblue" and state.get("max_streak") is not None:
            streak_text += f" / 最长 {state['max_streak']}"
        message = state.get("message")
        return [
            ("👤", "账号", account_text),
            ("💰", "积分 / 余额", self._points_text(key, state)),
            ("🧧", "奖励", _fmt(reward)),
            ("🔥", "连续签到", streak_text),
            ("💬", "最近结果", _fmt(message) if message else "—"),
        ]

    def _forum_recent_rows(self, key: str, state: dict) -> tuple:
        """近期签到表：返回 (表头, 行数据)。药丸站点无逐日历史，回退本地记录。"""
        if key == "invites":
            history = self.get_data("history")
            history = history if isinstance(history, list) else []
            rows = [(str(row.get("time") or "")[:10], _fmt(row.get("points")), _fmt(row.get("streak")))
                    for row in history
                    if isinstance(row, dict) and row.get("forum") == FORUM_TITLES["invites"]
                    and row.get("status") in ("success", "already")]
            return ("日期", "剩余药丸", "累计天数"), rows[:10]
        rows = [(str(item.get("date") or "—"), _fmt(item.get("reward")))
                for item in (state.get("recent") or [])[:10] if isinstance(item, dict)]
        return ("日期", "奖励"), rows

    def _kpi_tile(self, key: str, state: dict) -> dict:
        state = state if isinstance(state, dict) else {}
        _, color = self._status_chip(state)
        note = "尚未执行" if not state else self._today_text(state)
        return {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
            {"component": "VCard", "props": {"variant": "tonal", "class": "h-100"}, "content": [
                {"component": "VCardText", "props": {"class": "text-center pa-3"}, "content": [
                    {"component": "div", "props": {"class": "text-h5 mb-1"}, "text": FORUM_ICONS[key]},
                    {"component": "div", "props": {
                        "class": f"text-h6 font-weight-bold text-{color}",
                        "style": "white-space:normal;overflow-wrap:anywhere;line-height:1.2",
                    }, "text": self._points_text(key, state) if state else "—"},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": f"{FORUM_TITLES[key]} · 今日{note}"},
                ]},
            ]},
        ]}

    def _forum_card(self, key: str) -> dict:
        state = self.get_data(f"state_{key}")
        if not isinstance(state, dict) or not state:
            return {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"},
                    "content": [
                        {"component": "VCardTitle", "text": f"{FORUM_ICONS[key]} {FORUM_TITLES[key]}"},
                        {"component": "VCardText", "text": "尚未执行或未启用，保存配置后可立即签到或刷新论坛信息。"},
                    ]}
        chip_text, color = self._status_chip(state)
        headers, recent_rows = self._forum_recent_rows(key, state)
        overview_table = {"component": "VTable", "props": {"density": "compact"}, "content": [
            {"component": "tbody", "content": [
                {"component": "tr", "content": [
                    {"component": "td", "props": {"class": "text-no-wrap text-medium-emphasis"},
                     "text": f"{icon} {name}"},
                    {"component": "td", "text": str(value)},
                ]} for icon, name, value in self._forum_overview_rows(key, state)
            ]},
        ]}
        recent_table = [{"component": "VTable", "props": {"density": "compact"}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "props": {"class": "text-no-wrap text-medium-emphasis"}, "text": header}
                for header in headers]}]},
            {"component": "tbody", "content": [
                {"component": "tr", "content": [
                    {"component": "td", "text": str(cell)} for cell in row
                ]} for row in recent_rows
            ]},
        ]}] if recent_rows else [{"component": "VAlert", "props": {
            "type": "info", "variant": "tonal",
            "text": "暂无近期签到数据；签到或刷新论坛信息后展示。" if key != "invites"
            else "暂无本地签到记录；站点不提供逐日历史，此处展示本地成功记录。"}}]
        note = "" if key != "invites" else "（站点不提供逐日奖励历史，以下为本地成功记录）"
        return {"component": "VCard", "props": {"variant": "tonal", "class": "mb-3"}, "content": [
            {"component": "VCardTitle", "props": {"class": "d-flex align-center flex-wrap"}, "content": [
                {"component": "span", "props": {"class": "text-h6 mr-3"},
                 "text": f"{FORUM_ICONS[key]} {FORUM_TITLES[key]}"},
                {"component": "VChip", "props": {"color": color, "size": "small", "variant": "tonal"},
                 "text": chip_text},
                {"component": "VSpacer"},
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis"},
                 "text": f"更新时间：{_fmt(state.get('updated_at'))}"},
            ]},
            {"component": "VDivider"},
            {"component": "VCardText", "props": {"class": "pa-2"}, "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 5},
                     "content": [overview_table]},
                    {"component": "VCol", "props": {"cols": 12, "md": 7}, "content": [
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis mb-1"},
                         "text": f"近期签到（最近 10 条）{note}"},
                        {"component": "div", "props": {"style": "overflow-x:auto;max-width:100%"},
                         "content": recent_table},
                    ]},
                ]},
            ]},
        ]}

    def get_page(self) -> List[dict]:
        states = {}
        for key in FORUM_KEYS:
            state = self.get_data(f"state_{key}")
            states[key] = state if isinstance(state, dict) else {}
        kpi_row = {"component": "VRow", "props": {"dense": True, "class": "mb-1"},
                   "content": [self._kpi_tile(key, states[key]) for key in FORUM_KEYS]}
        cards = [self._forum_card(key) for key in FORUM_KEYS]
        history = self.get_data("history")
        if not isinstance(history, list):
            history = []
        history_rows = [{"component": "tr", "content": [
            {"component": "td", "text": str(row.get("time") or "—")},
            {"component": "td", "text": str(row.get("forum") or "—")},
            {"component": "td", "text": STATUS_TITLES.get(row.get("status"), str(row.get("status") or "—"))},
            {"component": "td", "text": str(row.get("message") or "")},
        ]} for row in history[:50]]
        table_content = [{"component": "VTable", "props": {"density": "compact",
                                                           "style": "min-width:640px"}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "props": {"class": "text-no-wrap"}, "text": text}
                for text in ("时间", "论坛", "状态", "说明")]}]},
            {"component": "tbody", "content": history_rows},
        ]}] if history_rows else [{"component": "VAlert", "props": {
            "type": "info", "variant": "tonal", "text": "暂无签到记录"}}]
        return [
            kpi_row,
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-2",
                "text": "本页展示各论坛缓存的账号与签到数据，不会因打开页面发起签到。"
                        "可在设置中“保存后刷新论坛信息”（只读）或“保存后立即签到一次”；"
                        "排查问题请查看 MoviePilot 日志（搜索“论坛签到·”）。"}},
            *cards,
            {"component": "VCard", "props": {"variant": "tonal", "class": "mt-2"}, "content": [
                {"component": "VCardTitle", "text": "📋 签到记录（最近 50 条）"},
                {"component": "VCardText", "props": {"class": "pa-2"}, "content": [
                    {"component": "div", "props": {"style": "overflow-x:auto;max-width:100%"},
                     "content": table_content},
                ]},
            ]},
        ]

    # ---- 标准接口 ----

    def get_form(self):
        return build_form()

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        """停用插件：置停止标记、清理待执行任务，并等待在途请求收敛（有界 10 秒）。

        超时未结束的线程由停止标记兜底：其后续结果落库、通知、Cookie 回写全部被拦截。
        """
        self._stopped = True
        for timer in list(getattr(self, "_retry_timers", {}).values()):
            try:
                timer.cancel()
            except Exception:
                pass
        self._retry_timers.clear()
        deadline = time.monotonic() + 10
        while True:
            with self._inflight_lock:
                running = [thread for thread in self._inflight_threads
                           if thread is not threading.current_thread() and thread.is_alive()]
            if not running or time.monotonic() >= deadline:
                break
            for thread in running:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._inflight_lock:
            lingering = [thread for thread in self._inflight_threads
                         if thread is not threading.current_thread() and thread.is_alive()]
        if lingering:
            logger.warning(f"PT 论坛签到：{len(lingering)} 个在途任务等待超时，"
                           "已由停止标记阻断其结果落库与配置回写")
