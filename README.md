# MoviePilot-Plugins

JinxJie 的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 第三方插件库。

本库 fork 自官方 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)，仅保留本人开发的插件，便于后续持续新增。

## 插件列表

| 插件 | 目录 | 版本 | 说明 |
|------|------|------|------|
| HHCLUB 自动抽奖 | [`plugins.v2/hhlottery`](plugins.v2/hhlottery) | 1.0.7 | HHCLUB 自动抽奖增强版，支持定时抽奖、手动停止、最新配置接管、大奖通知、今日/历史汇总。若你也在使用油猴脚本，可使用 [HHCLUB 自动抽奖 · 庆典版](https://greasyfork.org/zh-CN/scripts/591722)。 |

## 安装

在 MoviePilot 「插件管理」中添加仓库地址：
```
https://github.com/JinxJie/MoviePilot-Plugins
```

## 目录结构

```
icons/                          # 插件图标
plugins/                        # V1 插件（暂无）
plugins.v2/                    # V2 插件
  hhlottery/                   # HHCLUB 自动抽奖
    __init__.py                # 插件入口
    config_form.py             # 配置页表单
    helpers.py                 # 工具函数
package.json                    # V1 插件索引
package.v2.json                # V2 插件索引
package.v3.json                # V3 插件索引
docs/                          # 官方文档
scripts/                       # 官方脚本
tests/                         # 官方测试
```

## 开发新插件

参考 [官方插件开发文档](https://wiki.movie-pilot.org/docs/development/create-plugin)。

## 致谢

- [MoviePilot](https://github.com/jxxghp/MoviePilot)
- [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
