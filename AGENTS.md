# AGENTS.md

This file provides guidance to AI coding agents (ZCode / Claude Code compatible) when working with code in this repository.

# QuotaBar

A macOS menu bar app displaying AI platform API usage/quota statistics. Built with SwiftUI + AppKit hybrid architecture. Supports multiple platform accounts (MiniMax, GLM, 基元律动 TokenRhythm, Stepfun).

## Build Commands

```bash
# Generate Xcode project
xcodegen generate

# Debug build
xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -configuration Debug build

# Release build
xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -configuration Release build

# Run tests
xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test

# Package DMG (requires create-dmg)
brew install create-dmg
./scripts/package-dmg.sh
```

## Architecture

- **Menu bar app** (LSUIElement=true, no dock icon)
- **SwiftUI + AppKit hybrid**: SwiftUI views inside NSStatusItem via NSHostingController
- **Protocol-based multi-platform architecture**: each platform implements `PlatformAPIService` protocol

### Directory Structure

| Directory | Purpose |
|-----------|---------|
| `App/` | Entry point (`main.swift`, `AppDelegate.swift`), Info.plist |
| `Models/` | Data models (`PlatformProtocol.swift` - core types, `PlatformInstance.swift`) |
| `Services/` | Business logic |
| `Services/Platforms/` | Platform-specific services |
| `Services/Platforms/MiniMaxPlatform/` | MiniMax API service |
| `Services/Platforms/TokenRhythmPlatform/` | 基元律动 (TokenRhythm) 余额 service — 网页端 `/api/wallet/summary` + `tr_session` cookie 鉴权 (凭据存 FileKeyStore; 采集流程见 CHANGELOG [2.2.0], 禁止引导用户点网页「退出登录」) |
| `Services/Platforms/StepFunPlatform/` | Stepfun 套餐 service — 官方开源客户端 Step Code 的 gRPC-Connect 接口 (`step.openapi.devcenter.Dashboard`), Oasis 双 cookie + oasis-* 头鉴权, 显示月 Credits 余量% + 重置日 |
| `Services/Platforms/PlatformManager.swift` | Orchestrates all platform services |
| `Services/Platforms/PlatformConfigStore.swift` | Per-instance config (FileKeyStore-backed key; UserDefaults dict 存非密字段) |
| `Services/Platforms/PlatformInstanceStore.swift` | 账号实例注册表 + 老版本迁移 + 增删改移 |
| `Services/AppEnvironment.swift` | 测试进程存储隔离路由 |
| `Services/KeychainStore.swift` | API key 的 Keychain 存取 (仅迁移读取) |
| `Services/FileKeyStore.swift` | API key 的文件存储 (0600) + Keychain→文件一次性迁移 |
| `Services/ConfigService.swift` | Global config (display mode, active platform, locale) |
| `Services/NetworkService.swift` | Network abstraction for testability |
| `StatusBar/` | Menu bar UI - `StatusBarController` manages NSStatusItem |
| `ViewModels/` | `PlatformViewModel` - manages multiple platform data |
| `Views/` | SwiftUI views - `PopoverContentView`, `StatusBarView` |
| `Tests/` | Unit tests with mocks |
| `Resources/ConfigTemplates/` | Config templates for each platform |

### Key Protocols

- `PlatformAPIService` - each platform implements this for API calls
- `NetworkService` - network abstraction (URLSession wrapper for testability)
- `PlatformType` - enum identifying supported platforms
- `PlatformInstance` - 账号实例 (同一平台可多账号); `PlatformInstanceStore` 持有全部实例
- `KeychainStoring` - key 存储抽象 (`FileKeyStore` 生产: 0600 私有文件 / `InMemoryKeychainStore` 测试; `KeychainStore` 仅用于旧 Keychain 一次性迁移)
- `AppEnvironment` - 测试进程存储路由: 单元测试自动走隔离 defaults suite + 内存 Keychain, **测试永不触碰真实配置** (新写测试勿直接用 UserDefaults.standard / KeychainStore)

### Key Patterns

- **StatusBarController** creates NSStatusItem, adds subview via `button.addSubview(statusBarView)`
- **RightClickStatusBarView** intercepts clicks via override, emits callbacks for left/right click
- **Popover** shown relative to status bar button bounds with `.minY` edge
- **Platform switching** via right-click menu or popover tabs
- **I18nService** uses JSON files in Resources (`en.json`, `zh-Hans.json`), locale stored in ConfigService

## Adding a New Platform

0. 新增 Swift 文件后必须跑 `xcodegen generate` — `project.pbxproj` 由 XcodeGen 生成，手加的文件不进编译（症状：编译报"cannot find X in scope"但文件明明存在）
1. Add case to `PlatformType` enum in `Models/PlatformProtocol.swift`
2. Create config template in `Resources/ConfigTemplates/{platform}.template.json`
3. Create `Services/Platforms/{Platform}Platform/{Platform}PlatformService.swift` implementing `PlatformAPIService`
4. Register in `PlatformManager.init()`
5. Add I18n strings in `Resources/en.json` and `Resources/zh-Hans.json` — 新平台必配的 key 清单:
   - `menu.addAccount.{p}` (右键添加账号项)
   - `menu.metric.{label}` × 每个 metric label (右键「显示指标」可勾选项标题)
   - `metric.{label}` × 每个 metric label (弹窗卡片标题, 缺失时回退显示原始 label)
   - `popover.configurePlatform.{p}` + `popover.inputPlaceholder.{p}` (配置面板标题/占位文案; 不配则回退通用 API Key 文案)
   - cookie/session 型凭据平台的 401 文案: 在 `PlatformError.errorDescription` 的 `unauthorized` 分派 switch 归入 `error.unauthorized.session` 分支 (API key 型归入 `error.unauthorized.key`; switch 穷举由编译器强制, 新平台漏归入直接编译失败)
6. Check `PopoverContentView` platformType branches (`metricIcon` / `configTitleKey` / `configPlaceholderKey`) — `default` 兜底漏了不报错, 新平台会静默拿到通用图标/文案
7. Check `ConfigService.availableMetricLabels(for:)` — 新平台若产出菜单默认清单外的 metric label, 用户无法勾选, 状态栏恒显 "--"; 周额度这类同指标多态 (标准/加成/∞) 登记进 `StatusBarViewHelper.labelFamilies` 做同族匹配
8. Write tests first (TDD)

## ⚠️ Human-in-the-Loop 原则 (强制)

**任何 commit / push / PR / Release 操作都必须经过人工确认**, Claude **禁止** 自主执行:

- ❌ 禁止未经用户确认就 `git commit` / `git push` / `gh pr create` / `gh pr merge` / `gh release create`
- ❌ 禁止未经用户确认就 `git tag` / `git push --tags` / 推 `gh-pages` 分支
- ❌ 禁止未经用户确认就删除远端分支 / 改动 `appcast.xml`
- ❌ 禁止在用户没看到 diff 的情况下自动 amend / rebase / force-push

**标准协作模式**:
1. Claude 完成代码 + 跑测试 + 自查 → 把**改动总结** (改了哪些文件 / 改了什么 / 测试结果) 呈现给用户
2. 用户审核 diff, 确认无误后**明确授权** "提交" / "push" / "开 PR" / "merge"
3. Claude 在得到明确指令后才执行对应的 git / gh 命令
4. PR 的 merge / Release 的创建是**用户最终行为** — 即使 Claude 建议了命令, 也由用户点击 GitHub UI 或亲自跑 `gh` 命令

**例外** (这些不需要逐步确认, 但应该在汇报里说明):
- 本地 `git status` / `git diff` / `git log` 等只读命令
- 构建 + 跑测试 (`xcodebuild ... test`)
- `curl` 等网络探测 (但避免向真实生产端点发送写操作)
- 删除 Claude 自己创建的临时文件 (worktree 等)

## Git Workflow

**main 分支受保护**, 修改代码必须通过 PR 合入:
1. 创建新分支或使用临时分支
2. **经过用户审核后** 提交更改并 push
3. **经过用户审核后** 通过 `gh pr create` 创建 PR
4. **用户** 使用 squash merge 合入 main (推荐在 GitHub UI 操作)

```bash
# 创建 PR (需用户授权)
gh pr create --title "description" --body "change details"

# Squash merge (由用户执行, 通常在 GitHub UI 点击按钮)
gh pr merge <pr-number> --squash
```

## Sparkle Update Release Process

Use `scripts/update-appcast.sh`; `sparkle:version` must be `CURRENT_PROJECT_VERSION` (integer build), not marketing `X.Y.Z`. See `docs/sparkle-integration.md` and `docs/release-process.md`.

1. Update `appcast.xml` on `gh-pages` via the script (marketing + build + DMG length)
2. Create and push GitHub Release with the `.dmg` file
3. Ensure `length` matches DMG size; verify via raw gh-pages URL after human-approved push

## Tech Stack

- Swift 5.9, macOS 14.0+
- SwiftUI (views) + AppKit (NSStatusItem, NSPopover)
- [Sparkle](https://github.com/sparkle-project/Sparkle) 2.6.0+ for auto-update
- XcodeGen for project generation
- EdDSA signing key for update verification (public key in project.yml)
