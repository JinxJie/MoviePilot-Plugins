# MoviePilot-Plugins

JinxJie 的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 第三方插件库。

本库 fork 自官方 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)，仅保留本人开发的插件，便于后续持续新增。

## 插件列表

| 插件 | 目录 | 版本 | 说明 |
|------|------|------|------|
| HHCLUB 自动抽奖 | [`plugins.v2/hhlottery`](plugins.v2/hhlottery) | 1.0.8 | 定时自动抽奖、大奖即时通知、盈亏统计、站内信清理 |
| NodeSeek 自动签到 | [`plugins.v2/nodeseek`](plugins.v2/nodeseek) | 1.0.0 | NodeSeek 论坛每日自动签到、鸡腿收益统计、消息通知 |

## 📖 使用说明

**1. 添加仓库地址**

在 MoviePilot 「插件管理」中添加仓库地址：
```
https://github.com/JinxJie/MoviePilot-Plugins
```

**2. 安装与配置**

- 在 MoviePilot 中安装插件。
- 根据插件说明配置相关参数。
- 启用插件并设置定时任务（如需要）。

## 🧩 插件详情

点击插件名展开查看功能与更新历史。

<details>
<summary>1. 📢 HHCLUB 自动抽奖（HHLottery）</summary>

**v1.0.8 · 站点自动化 · HHCLUB 幸运大转盘**

**功能：**
后台自动执行 HHCLUB 幸运大转盘抽奖，支持定时运行、手动停止、抽奖统计和结果通知。

**标签：**
站点自动化、幸运抽奖、定时任务、数据统计、消息通知

**特点：**
- ⏰ 支持 Cron 定时自动抽奖
- 🎲 支持自定义抽奖间隔、抽奖次数和保留憨豆
- 🛑 支持手动停止当前抽奖任务
- 🔄 支持最新配置接管正在运行的任务
- 🍪 支持从 MoviePilot 站点管理读取 Cookie，也支持手动填写
- 📡 支持每 N 抽发送运行状态播报，设置为 0 可关闭
- 🏆 支持大奖关键词匹配和即时通知
- 🧹 支持抽奖后自动清理站内信
- 📊 提供今日与历史抽奖汇总、盈亏统计和奖品分布
- 🥧 支持具体奖项占比饼图与奖品明细
- 📋 支持运行记录查看，移动端可横向查看完整字段
- 🎨 使用 SiteStatistic 风格的 KPI 统计卡片，适配桌面端和移动端
- 🎯 插件图标使用宝可梦精灵球样式

**使用说明：**
1. 在 MoviePilot 插件管理中添加本仓库地址并安装插件。
2. 在插件设置中选择 Cookie 来源，配置抽奖参数和通知选项。
3. 开启插件后由后台按 Cron 规则自动运行，也可以在插件页面手动操作。
4. 如需实时查看抽奖过程或进行更细致的手动操作，建议搭配 [HHCLUB 自动抽奖 · 庆典版油猴脚本](https://greasyfork.org/zh-CN/scripts/591722)。

**免责声明：**
本插件仅通过站点现有接口执行自动化操作，不会修改任何站点数据。站点规则、接口或页面变更可能导致功能异常、抽奖结果或账号相关风险，请自行评估后使用并理性抽奖；因使用本插件产生的任何问题，开发者不承担责任。

**更新说明：**
- v1.0.8：优化了一些细节问题。
- v1.0.7：重构插件界面，优化仓库首页说明。
- v1.0.6：新增手动停止开关，支持最新配置接管运行。
- v1.0.5：抽奖结束后校准最新余额。
- v1.0.4：赌徒模式忽略最大抽奖次数并优化提示。
- v1.0.3：重算历史统计并自动迁移旧数据。
- v1.0.2：对齐油猴脚本 VIP 折算逻辑，修正历史盈亏累计，并优化今日/历史盈亏显示。
- v1.0.1：优化统计页面、通知汇总和兼容性。
- v1.0.0：初始版本，支持自动抽奖、余额追踪、大奖通知、站内信清理和 Cron 定时运行。

</details>

<details>
<summary>2. 📌 NodeSeek 自动签到（NodeSeekSign）</summary>

**v1.0.0 · 签到自动化 · NodeSeek 论坛**

**功能：**
NodeSeek 论坛每日自动签到，使用 curl_cffi 模拟 Chrome 浏览器指纹绕过 Cloudflare 拦截，支持鸡腿收益统计、签到历史记录和消息通知。

**标签：**
论坛签到、每日定时、自动化、数据统计、消息通知

**特点：**
- 📅 支持 Cron 定时自动签到（默认每天 00:30，可自定义）
- 🛡️ 使用 curl_cffi 模拟 Chrome 指纹，绕过 Cloudflare 拦截
- 🍗 鸡腿收益统计：累计签到天数、累计鸡腿、本月签到
- 📋 签到历史记录（最近 12 条）
- 🔔 通知：签到成功 / 今日已签到 / 签到失败 / Cookie 失效
- ⌨️ 命令 `/nodeseek` 与 API `/nodeseek/sign` 手动签到

**使用说明：**
1. 在 MoviePilot 插件管理中添加本仓库地址并安装插件。
2. 浏览器登录 NodeSeek，F12 → Application → Cookies 复制 `nodeseek.com` Cookie 值，填入插件配置。
3. 配置签到时间（默认每天 00:30，想抢前排排名可改为 `5 0 * * *`）。
4. 确保安装 `curl_cffi` 依赖，否则会被 Cloudflare 拦截（插件会自动回退 requests 并提示）。

**免责声明：**
本插件仅通过站点现有接口执行签到操作，不会修改任何站点数据。站点规则、接口或防护策略变更可能导致功能异常，请自行评估后使用；因使用本插件产生的任何问题，开发者不承担责任。

**更新说明：**
- v1.0.0：首发版本，支持每日定时自动签到、curl_cffi 过 Cloudflare、鸡腿收益统计、签到历史、消息通知、手动签到命令与 API。

</details>

## ⚠️ 注意事项

- 本插件库中的插件均为个人维护，使用前请仔细阅读说明。
- 部分插件需要特定权限或配置才能正常使用。
- 如遇到问题，请先查看插件说明或提交 Issue。
- 建议定期更新插件以获取最新功能和修复。

## 目录结构

```text
MoviePilot-Plugins/
├── docs/                       # 官方文档
├── icons/                      # 插件图标
├── plugins/                    # V1 插件
├── plugins.v2/                 # V2 插件
│   ├── hhlottery/              # HHCLUB 自动抽奖
│   └── nodeseek/               # NodeSeek 自动签到
├── scripts/                    # 官方脚本
├── tests/                      # 官方测试
├── package.json                # V1 插件索引
├── package.v2.json             # V2 插件索引
├── package.v3.json             # V3 插件索引
└── README.md                   # 仓库说明
```

## 开发新插件

参考 [官方插件开发文档](https://wiki.movie-pilot.org/)。

## 致谢

- [MoviePilot](https://github.com/jxxghp/MoviePilot)
- [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
