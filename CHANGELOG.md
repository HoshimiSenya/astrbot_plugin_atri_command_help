# 更新日志

## 1.2.0 - 2026-11-01

### 变更

- 显示名与默认标题统一为「ATRI指令大全」，`metadata.yaml`、设置页、README 与日志同步。
- 插件更名为 `astrbot_plugin_atri_command_help`，更新 `metadata.yaml` 的仓库、主页与标签字段。
- 生成的图片不再包含「ATRI指令大全」自己的卡片，上游同名插件（`astrbot_plugin_command_help`）也一并排除。
- 更换 `logo.png` 为新的 ATRI 主题图标。
- 图片缓存改为按实际渲染内容生成指纹：插件、指令、标题、栏数、别名开关、唤醒前缀、底图与隐藏规则等内容不变时，直接复用上一张图。
- 清理旧图时跳过当前缓存正在引用的文件。
- 底部统计小字只保留插件数与指令数。
- `main.py` 更新模块说明、日志前缀与版本号，临时图片文件名改为 `atri_command_help_*.png`，避免与上游插件共用清理规则。
- `_conf_schema.json` 默认标题改为「ATRI指令大全」，隐藏规则的示例改为本项目的插件名与目录名。
- README 重写：新增「与上游项目的差异」「上游与许可」「项目结构」等章节，补充 ATRI 底图、按身份鉴权、隐藏规则、跨平台字体与内容级缓存的说明。
- `LICENSE` 保留上游 AcceleratorYue 的 MIT 版权声明，并补充本项目的修改版权。

### 移除

- 移除 `cache_seconds` 配置：图片缓存不再按秒数过期。
- 移除「页脚附加说明」配置：页脚只显示插件数与指令数。

## 1.1.0 - 2026-10-24

本项目首个功能版本，在上游 1.0.1 基础上修改。

### 新增

- 发送前鉴权：AstrBot 管理员看到完整的指令图，普通用户自动隐藏仅管理员 / 群管可用的指令。
- 设置页新增「隐藏指定插件的指令」规则列表，可按插件选择要隐藏的指令，也支持隐藏整个插件。
- 所有文字改用 Linux 常见中文字体（Noto Sans CJK / 思源黑体 / 文泉驿 / Droid Sans Fallback），并自动扫描系统字体目录；Windows 回退 SimHei。

### 变更

- 卡片保留插件名称大标题与简介，不再显示徽标、版本、作者与「内置」标记。
- 底图改为插件目录内的 `ATRI.jpeg`（等比缩放后裁剪铺满）。
- 卡片与图例的背景统一为 80% 不透明度（20% 透明），底图隐约可见。
- 底部统计文字改为与卡片内说明一致的灰色小字，并去掉其白色背景模块。
- 图片缓存按身份与规则分别保存，避免管理员与非管理员互相覆盖。
- 本项目从该版本起独立维护，与上游 `astrbot_plugin_command_help` 分开发布。

### 移除

- 移除图片右上角的 AstrBot 版本、插件/指令数量与生成时间三行文字。
- 移除页脚「由『指令大全』插件自动生成」的署名。
- 移除原蓝色渐变标题条与装饰圆形。

## 上游历史（astrbot_plugin_command_help）

以下版本由上游作者 AcceleratorYue 发布，本项目在其基础上继续演进。

### 1.0.1 - 2026-10-20

插件市场收录准备。

#### 新增

- 新增 `logo.png`（插件市场与插件页图标）。
- 新增 `docs/PUBLISH.md`，记录 AstrBot 插件市场的收录流程与校验清单。
- `metadata.yaml` 补充 `tags`（指令 / 帮助 / 菜单 / 图片）与 `social_link`，便于市场分类和搜索。

功能代码无改动。

### 1.0.0 - 2026-10-19

首个版本。

#### 新增

- 只注册一个指令 `/指令大全`（别名 `/帮助`、`/指令帮助`、`/指令列表`），汇总当前所有已加载插件的指令。
- 支持指令组 / 子指令、别名、必填/可选参数提示、管理员 / 群管权限角标。
- 采集优先走 AstrBot 官方 `command_management.list_commands()`，失败时回退遍历 `star_handlers_registry`。
- 使用 Pillow 在本机把结果渲染成自适应高度的卡片图（1~3 栏），不依赖远端 T2I 服务。
- 支持包含 AstrBot 内置指令（可开关），内置指令带中文说明。
- 图片写入 AstrBot 临时目录，自动清理 1 小时前的旧图；带可配置的短时缓存。
- 提供离线自测脚本 `tests/test_offline_render.py`，覆盖注册、metadata、配置 schema、权限过滤、端到端出图与缓存。

[未发布]: https://github.com/HoshimiSenya/astrbot_plugin_atri_command_help/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/HoshimiSenya/astrbot_plugin_atri_command_help/releases/tag/v1.2.0
[1.1.0]: https://github.com/HoshimiSenya/astrbot_plugin_atri_command_help/releases/tag/v1.1.0
