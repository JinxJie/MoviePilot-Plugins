# NodeSeek 自动签到

NodeSeek 论坛每日自动签到插件，适用于 [MoviePilot](https://github.com/jxxghp/MoviePilot)。

## ✨ 功能

- 📅 每日定时自动签到（默认每天 00:30，可自定义 Cron）
- 🛡️ 使用 curl_cffi 模拟 Chrome 浏览器指纹，绕过 Cloudflare 拦截
- 🍗 鸡腿收益统计：累计签到天数、累计鸡腿、本月签到
- 📋 签到历史记录（最近 12 条）
- 🔔 通知：签到成功 / 今日已签到 / 签到失败 / Cookie 失效
- ⌨️ 命令 `/nodeseek` 与 API `/nodeseek/sign` 手动签到

## 📝 使用说明

### 1. 获取 Cookie

1. 浏览器登录 [NodeSeek](https://www.nodeseek.com/)
2. 按 `F12` 打开开发者工具 → `Application`（应用）→ `Cookies` → `https://www.nodeseek.com`
3. 找到名为 `nodeseek.com` 的 Cookie，复制其 Value（整段值）
4. 粘贴到插件配置的「Cookie」输入框

> ⚠️ Cookie 会过期。失效时插件会发送「Cookie 已失效」通知，重新复制更新即可。

### 2. 配置说明

- **启用插件**：开启后按签到时间自动运行
- **立即运行一次**：打开并保存后 3 秒内立即签到一次，完成后自动复位（无需等定时）
- **发送通知**：总开关，签到成功 / 已签到 / 失败 / Cookie 失效通知均受控
- **使用系统代理**：通过 MoviePilot 系统代理访问 NodeSeek

### 3. 签到时间

- NodeSeek 每天 **00:00**（北京时间）刷新签到，越早签到排名越靠前，可能获得额外鸡腿
- 默认 `30 0 * * *`（每天 00:30），想抢前排可改为 `5 0 * * *`（每天 00:05）
- 任意 Cron 表达式均可，如每天 8 点：`0 8 * * *`

### 4. 依赖安装

插件需要 `curl_cffi`（浏览器指纹模拟，绕过 Cloudflare 必装）：

```bash
pip install curl_cffi
```

> 若未安装，插件会自动回退到 requests 发送请求，但大概率会被 Cloudflare 拦截（403），建议务必安装。

## 📖 通知示例

```
🕒 2026-08-24 00:30:05
NodeSeek 签到

✅ NodeSeek 签到成功

🍗 获得 +13 鸡腿
💰 当前 1,234 鸡腿
```

## 🗂️ 数据说明

- 签到历史保存在插件数据中（最多 365 条），插件页展示最近 12 条
- 累计统计：成功签到天数（按日期去重）、累计鸡腿收益、本月签到天数

## 🛠️ 常见问题

| 问题 | 说明 |
|------|------|
| 日志报 `USER NOT FOUND` | Cookie 已失效，重新获取后更新配置 |
| 日志报 `Cloudflare 拦截` | 网络被风控，稍后重试；或启用系统代理 |
| 日志报 `high risk action` | 请求头不全或 IP 风控，确认已安装 curl_cffi 并重试 |
| 签到无通知 | 检查「发送通知」总开关是否开启 |

## 📦 版本

- v1.0.1 修复今日已签到状态识别，调整页面布局与设置说明
