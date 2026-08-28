# Changelog

本项目的所有重要变更记录。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [2.1.0] - 未发布

### Added
- 可配置指标显示：每个平台可勾选最多 2 个指标（5 小时窗口 / 周限额 / MCP 月度）显示在菜单栏，右键菜单「显示指标」多选
- MiniMax 无限套餐（∞）渲染：weekly_status 非 1 时显示 ∞ 而非百分比
- 立即刷新菜单项：一键清除所有平台缓存并重新拉取（平台卡住时自愈）
- MiniMax 周额度加成（boost）检测：加成套餐显示专属标签

### Changed
- 精简平台支持：移除 DeepSeek / MiMo / StepFun，仅保留 MiniMax + GLM
- API key 迁移至 macOS Keychain 存储（自 v2.0.4 引入，本版本延续），UserDefaults 不再保留明文
- 版本号体系：合并 v2.0.4 的发布线，build number 单调递增（→ 6）

### Fixed
- 修正 GLM 平台测试断言：GLM 鉴权不带 Bearer 前缀，模板 `auth_prefix` 为空串是预期行为

## [2.0.4] - 2026-07-16

### Added
- 开机时启动开关（状态栏右键菜单，基于 SMAppService）
- API key 持久化迁移到 macOS Keychain，UserDefaults 明文清除（含一次性迁移与降级策略）
- Sparkle appcast 构建版本号自动化（update-appcast 脚本 + 测试）

## [2.0.3] - 2026-06-03

### Added
- 状态栏右键菜单「关于」项（版本信息 + 打开发布页）

### Fixed
- fetchAllUsage() 异步化，消除虚假 await 警告
- 鼠标拖出状态栏视图时隐藏按压高亮

## [2.0.2] - 2026-06-02

### Fixed
- MiniMax 对齐 cc-switch 新版 API 适配
- 清理死 i18n key、修正过期测试断言

## [2.0.0] - 2026-05-25

### Added
- 多平台架构：协议驱动（PlatformAPIService），新增 GLM / DeepSeek / SiliconFlow / OpenRouter / Novita 等平台
- Sparkle 自动更新（EdDSA 签名校验）
- DMG 打包脚本（含 Applications 拖拽安装别名）

## [1.0.x] - 2026-04 ~ 2026-05

- 1.0.0：首个版本，MiniMax 用量菜单栏显示（原名 MiniMaxBar）
- 1.0.2：显示模式切换（已用 / 剩余）、中英双语 i18n
- 1.0.3：刷新间隔配置、平台区域（国内 / 国际）支持
