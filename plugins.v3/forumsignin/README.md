# PT论坛签到（药丸 / 蜂巢 / 蓝影）

MoviePilot V3 插件：把三个 PT论坛签到插件合并为一个，统一配置、调度、记录与通知。

| 论坛 | 站点 | 凭证 | 集成来源 |
|------|------|------|----------|
| 药丸 | https://invites.fun | Cookie（`flarum_remember`）或账号密码 | jxxghp plugins.v2 `invitessignin` v3.0.1 |
| 蜂巢 | https://fengchao.chat | MP 专用 API Key（论坛"隐秘的角落"生成） | madrays plugins.v3 `fengchaosignin` v3.1.10 |
| 蓝影 | https://hdblue.cc | 专用密钥 `hdbmp_…`（个人设置 → MoviePilot 签到） | jxxghp plugins.v3 `hdbluesignin` v2.0.2 |

## 功能

- 每个论坛独立启用、独立 Cron 周期（`VCronField`），叠加全局随机延迟错峰；代理为**全局开关**（默认关闭，开启后读取 MoviePilot 系统代理，应用到全部论坛）。
- 药丸：Cookie 签到优先（自动用 `flarum_remember` 刷新 `flarum_session`），失败自动改用账号密码登录兜底；`remember` 值变化时自动回写插件配置；可选 CloakBrowser 浏览器仿真绕过 Cloudflare（宿主运行时已内置，插件不额外声明依赖，缺库时自动降级为普通请求）。
- 蜂巢 / 蓝影：Bearer Key 官方集成 API（`/api/integrations/moviepilot/v1/check-in`），蓝影 POST 结果不确定时自动用 GET 复核，遵循服务端 `Retry-After` 限流。
- 失败按全局设置延迟重试（次数/间隔可配，当日封顶，跨日恢复；成功后清零）。
- 通知模式三选一：全部结果 / 仅失败 / 关闭。
- 首页按论坛分别展示：账号（蜂巢含等级）、积分/余额、奖励、连续/最长签到、今日状态，以及**近期签到数据表**——蜂巢与蓝影展示论坛侧近 30 天逐日奖励（`/me` 的 `recentCheckIns`、`/check-in` 的 `history`），药丸站点不提供逐日历史、展示本地成功记录（日期/余额/累计天数）。另有合并的最近 50 条签到记录。
- 「保存后刷新论坛信息」（只读）不发签到请求，拉取三论坛账号卡与签到数据并合并进缓存；签到结论不受刷新影响。

## 与原插件的差异

- 蜂巢仅保留签到 + 账号信息（`GET /me`）；原插件的「PT 人生快照推送」（`PUT /pt-life/snapshot`）与「论坛通知 webhook」未集成。
- 蓝影原插件的自定义代理地址字段并入"使用系统代理"开关（读取 MoviePilot 系统代理）。
- 药丸签到判定以站点实时状态为准（`canCheckin=false` 且 `lastCheckinTime` 为今天），本地历史只作展示。

## 签到接口（核实自上游源码）

### 药丸 invites.fun（Flarum）

1. `GET https://invites.fun`（禁重定向）携带 `Cookie: flarum_remember=…` → 从 Set-Cookie 取新 `flarum_session`。
2. `GET https://invites.fun` 页面 HTML → 正则提取 `"csrfToken":"…"` 与 `"userId":\d+`。
3. 状态查询 `GET /api/users/{userId}`（带 `x-csrf-token` 头）→ JSON:API `data.attributes`：`canCheckin`、`lastCheckinTime`、`totalContinuousCheckIn`、`money`。
4. 签到 `POST /api/checkin` → 成功判定：响应 `canCheckin=false` 且 `lastCheckinTime` 以今天开头。
5. 登录兜底：`GET /` 取初始 `flarum_session` + `x-csrf-token` 响应头 → `POST /login` JSON `{identification, password, remember:true}` → 取 `flarum_remember`/新会话/`userId`。

### 蜂巢 fengchao.chat

- 头：`Authorization: Bearer <Key>`、`X-MoviePilot-Instance-Id`（UUID）、`X-MoviePilot-Plugin-Version`、`X-MoviePilot-Version`。
- `POST /api/integrations/moviepilot/v1/check-in` `{}` → data：`alreadyCheckedIn`、`reward`、`currentStreak`、`checkIn.todayReward`。
- `GET …/me` → 身份卡：`status.account`（displayName/points/levelName/levelIcon/badges/**recentCheckIns** 近 30 天逐日奖励）与 `status.checkIn`（checkedInToday/todayReward/currentStreak）。
- 响应封装 `{code, message, data}`，`code≠0` 报错；408/425/429/5xx 有限重试，`trust_env=False` 隔离环境代理。

### 蓝影 hdblue.cc

- `Authorization: Bearer hdbmp_…`；`GET /api/integrations/moviepilot/v1/check-in` 查询状态、`POST …/check-in` 签到、`GET …/me` 取账号与数据。
- data 白名单校验（`protocolVersion=1`、日期、账号、数值字段），载荷自带 `points`、`currentStreak`、`maxStreak`、`pointName` 与 `history`（近 30 天逐日奖励）；POST 结果不确定（超时/畸形响应）时 GET 复核；401=密钥失效、429 读 `Retry-After`、禁跟随跳转。

### 药丸数据边界

`GET /api/users/{userId}` 仅返回 `canCheckin`、`lastCheckinTime`、`totalContinuousCheckIn`（累计天数）、`money`（余额），**站点不提供逐日奖励历史**；首页近期签到表使用本地成功记录（日期/余额/累计天数）。

## 测试

```bash
# 仓库根目录
"../MoviePilot/.venv/Scripts/python.exe" tests/run.py   # 或 pytest tests/v3/forumsignin
```

离线单测位于 `tests/v3/forumsignin/`，全部 HTTP 请求伪造，不需要真实凭证。
