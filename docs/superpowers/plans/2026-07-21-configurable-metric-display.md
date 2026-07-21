# Configurable Metric Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users choose which usage metrics (5-hour / weekly / MCP) each platform shows in the menu bar via the right-click menu, with adaptive font size when fewer metrics are selected. Also display ∞ for MiniMax plans that have no weekly limit.

**Architecture:**
- New `enabledMetrics` config in `ConfigService` (UserDefaults-backed, per-platform `[String]` of metric labels)
- Service layer (`MiniMaxPlatformAPIService`) always returns a weekly metric — `weekly_limit_unlimited` when API reports no weekly limit, instead of omitting it
- `StatusBarView` filters `platformData.metrics` by `enabledMetrics`, then renders 0/1/2 layout variants
- Right-click menu gains an "Enabled Metrics" submenu (per-platform multi-select, max 2)
- Notification `Notification.Name.enabledMetricsChanged` triggers menu bar redraw

**Tech Stack:** Swift 5.9, SwiftUI, AppKit, UserDefaults, XCTest

**Spec:** `docs/superpowers/specs/2026-07-21-configurable-metric-display-design.md`

**Important user-driven rule:** All `git commit` / `git push` / `gh pr` operations require explicit human approval per `CLAUDE.md`. This plan never auto-commits.

---

## File Structure

```
Services/
  ConfigService.swift                # Modify: enabledMetrics getter/setter + defaults + notification
Tests/
  Services/ConfigServiceEnabledMetricsTests.swift  # Create: enabledMetrics tests
  Platforms/MiniMaxPlatformTests.swift              # Modify: update existing weekly-omitted test
  Views/StatusBarViewTests.swift                    # Create: layout tests
Services/Platforms/MiniMaxPlatform/
  MiniMaxPlatformService.swift       # Modify: always return weekly metric (unlimited or limited)
Views/
  StatusBarView.swift                # Modify: enabledMetrics filter + 0/1/2 layout + ∞ rendering
StatusBar/
  StatusBarController.swift          # Modify: enabled-metrics submenu + listener + pass-through
Resources/
  en.json                            # Modify: menu.enabledMetrics, metric.weekly_limit_unlimited
  zh-Hans.json                       # Modify: same keys in Chinese
README.md / README_zh.md             # Modify: mention selectable metrics
```

---

## Task 1: ConfigService — `enabledMetrics` getter with defaults

**Files:**
- Modify: `Services/ConfigService.swift`
- Create: `Tests/Services/ConfigServiceEnabledMetricsTests.swift`

- [ ] **Step 1: Write the failing test**

Create `Tests/Services/ConfigServiceEnabledMetricsTests.swift`:

```swift
import XCTest
@testable import QuotaBar

final class ConfigServiceEnabledMetricsTests: XCTestCase {
    var defaults: UserDefaults!
    var service: ConfigService!

    override func setUp() {
        super.setUp()
        defaults = UserDefaults(suiteName: "test.enabledMetrics.\(UUID().uuidString)")!
        defaults.removePersistentDomain(forName: defaults.dictionaryRepresentation().keys.first ?? "")
        service = ConfigService.shared
        // wipe any pre-existing keys for clean defaults
        defaults.removeObject(forKey: "quotabar.platform.minimax_cn.enabledMetrics")
        defaults.removeObject(forKey: "quotabar.platform.glm_cn.enabledMetrics")
    }

    func testDefaultEnabledMetricsForMiniMax() {
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["five_hour"])
    }

    func testDefaultEnabledMetricsForGLM() {
        XCTAssertEqual(service.enabledMetrics(for: .glm_cn), ["five_hour", "weekly_limit"])
    }

    func testEmptyUserDefaultsReturnsDefaults() {
        defaults.removeObject(forKey: "quotabar.platform.minimax_cn.enabledMetrics")
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["five_hour"])
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/ConfigServiceEnabledMetricsTests`
Expected: FAIL with "value of type 'ConfigService' has no member 'enabledMetrics(for:)'"

- [ ] **Step 3: Implement the getter with defaults**

In `Services/ConfigService.swift`, add at the end of the class (before the closing brace):

```swift
// MARK: - Enabled Metrics

/// 每个平台用户勾选要在菜单栏显示的 metric label 列表. 顺序即显示顺序.
/// getter 优先读 UserDefaults, 无值时返回平台默认值 (首次安装 / 老用户升级).
func enabledMetrics(for platform: PlatformType) -> [String] {
    configLock.lock()
    defer { configLock.unlock() }
    let key = "quotabar.platform.\(platform.rawValue).enabledMetrics"
    if let raw = UserDefaults.standard.string(forKey: key),
       let data = raw.data(using: .utf8),
       let labels = try? JSONDecoder().decode([String].self, from: data),
       !labels.isEmpty, labels.count <= 2 {
        return labels
    }
    return Self.defaultEnabledMetrics(for: platform)
}

/// 平台首次安装的默认勾选. 改了这里会改变新用户体验, 不影响已配置的用户.
static func defaultEnabledMetrics(for platform: PlatformType) -> [String] {
    switch platform {
    case .minimax_cn:
        return ["five_hour"]
    case .glm_cn:
        return ["five_hour", "weekly_limit"]
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/ConfigServiceEnabledMetricsTests`
Expected: PASS (3 tests)

---

## Task 2: ConfigService — `setEnabledMetrics` with persistence + boundary rejection

**Files:**
- Modify: `Services/ConfigService.swift`
- Modify: `Tests/Services/ConfigServiceEnabledMetricsTests.swift`

- [ ] **Step 1: Write the failing tests**

Append to `Tests/Services/ConfigServiceEnabledMetricsTests.swift`:

```swift
    func testSetEnabledMetricsPersists() {
        service.setEnabledMetrics(["mcp_monthly"], for: .minimax_cn)
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["mcp_monthly"])
        // 模拟重启: 用一个独立的 ConfigService 实例读
        let key = "quotabar.platform.minimax_cn.enabledMetrics"
        let stored = UserDefaults.standard.string(forKey: key)
        XCTAssertNotNil(stored)
        XCTAssertTrue(stored!.contains("mcp_monthly"))
    }

    func testSetEnabledMetricsRejectsEmpty() {
        service.setEnabledMetrics(["five_hour"], for: .minimax_cn)  // baseline
        service.setEnabledMetrics([], for: .minimax_cn)              // rejected
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["five_hour"])
    }

    func testSetEnabledMetricsRejectsTooMany() {
        service.setEnabledMetrics(["five_hour"], for: .minimax_cn)
        service.setEnabledMetrics(["five_hour", "weekly_limit", "mcp_monthly"], for: .minimax_cn)
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["five_hour"])
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/ConfigServiceEnabledMetricsTests`
Expected: FAIL with "value of type 'ConfigService' has no member 'setEnabledMetrics(_:for:)'"

- [ ] **Step 3: Implement the setter**

In `Services/ConfigService.swift`, add after `enabledMetrics(for:)`:

```swift
/// 设置平台启用的 metric label 列表. 拒绝空数组 (保留旧值) 和长度 > 2 的数组.
/// 写入成功时发 `.enabledMetricsChanged` 通知.
func setEnabledMetrics(_ labels: [String], for platform: PlatformType) {
    guard !labels.isEmpty, labels.count <= 2 else { return }

    configLock.lock()
    let key = "quotabar.platform.\(platform.rawValue).enabledMetrics"
    let encoded = (try? JSONEncoder().encode(labels)).flatMap { String(data: $0, encoding: .utf8) }
    configLock.unlock()

    guard let encoded else { return }
    UserDefaults.standard.set(encoded, forKey: key)
    NotificationCenter.default.post(name: .enabledMetricsChanged, object: platform)
}
```

Add the notification name in a top-level extension. In `Services/ConfigService.swift`, add at the bottom of the file (outside the class):

```swift
extension Notification.Name {
    static let enabledMetricsChanged = Notification.Name("enabledMetricsChanged")
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/ConfigServiceEnabledMetricsTests`
Expected: PASS (6 tests)

---

## Task 3: MiniMax service — always return weekly metric (∞ variant)

**Files:**
- Modify: `Services/Platforms/MiniMaxPlatform/MiniMaxPlatformService.swift`
- Modify: `Tests/Platforms/MiniMaxPlatformTests.swift`

- [ ] **Step 1: Write the failing test**

Add to `Tests/Platforms/MiniMaxPlatformTests.swift`:

```swift
    func testFetchUsageReturnsUnlimitedWeeklyWhenStatusIsNotOne() async throws {
        // current_weekly_status == 3 (无周限额 plan) → 应返回 weekly_limit_unlimited metric,
        // 不再"省略"周指标. 这让上层 UI 永远可以按 metrics 列表渲染, 不需要特判.
        let json = """
        {
            "model_remains": [{
                "model_name": "general",
                "current_interval_remaining_percent": 80.0,
                "current_weekly_remaining_percent": 50.0,
                "current_weekly_status": 3
            }]
        }
        """
        service.clearCache()
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        let config = PlatformConfigData(
            platformType: .minimax_cn,
            apiBaseURL: "https://test.com",
            authHeader: "Authorization",
            authPrefix: "Bearer ",
            apiKey: "test-key"
        )

        let result = try await service.fetchUsage(config: config, network: mockNetwork)

        XCTAssertEqual(result.metrics.count, 2)
        XCTAssertEqual(result.metrics[0].label, "five_hour")
        XCTAssertEqual(result.metrics[1].label, "weekly_limit_unlimited")
        XCTAssertNil(result.metrics[1].totalValue)
        XCTAssertEqual(result.metrics[1].unit, "unlimited")
        XCTAssertNil(result.metrics[1].resetTime)
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/MiniMaxPlatformTests/testFetchUsageReturnsUnlimitedWeeklyWhenStatusIsNotOne`
Expected: FAIL — `result.metrics.count` is 1, not 2.

- [ ] **Step 3: Update service to always return weekly metric**

In `Services/Platforms/MiniMaxPlatform/MiniMaxPlatformService.swift`, replace the `parseUsageData(from:platform:)` body (the section that builds metrics). Find this block:

```swift
        // 5 小时窗口永远存在
        var metrics: [UsageMetric] = [
            UsageMetric(label: "five_hour", currentValue: dailyRemainingPct, totalValue: 100, unit: "%", resetTime: dailyResetTime)
        ]

        // 周限额仅在 current_weekly_status == 1 时展示. 其它值(包括 nil)表示该 plan
        // 无周限额 — 跟上游 cc-switch 的语义对齐, 避免展示过时的/随机的 weekly 数据.
        if model.currentWeeklyStatus == 1 {
            // 周额度加成 (weekly_boost_permille > 1000 表示有加成, 如 1500 = 150%):
            // 区分"标准 100%"和"加成 150%"两种 plan, 用不同 label 让 UI 显示对应类型.
            let boosted = (model.weeklyBoostPermille ?? 0) > 1000
            metrics.append(
                UsageMetric(label: boosted ? "weekly_limit_boosted" : "weekly_limit", currentValue: weeklyRemainingPct, totalValue: 100, unit: "%", resetTime: weeklyResetTime)
            )
        }
```

Replace with:

```swift
        // 5 小时窗口永远存在
        var metrics: [UsageMetric] = [
            UsageMetric(label: "five_hour", currentValue: dailyRemainingPct, totalValue: 100, unit: "%", resetTime: dailyResetTime)
        ]

        // 周限额总是 append, 但状态不同时用不同 label + 字段:
        //   - currentWeeklyStatus == 1 + 无加成 → weekly_limit (剩余百分比)
        //   - currentWeeklyStatus == 1 + 加成   → weekly_limit_boosted (剩余百分比)
        //   - 其它值 (3 / nil)                  → weekly_limit_unlimited (∞, totalValue=nil)
        // 这样上层永远按 metrics 列表渲染, 不需要"如果只有一个 metric 就特殊处理"这种特判.
        if model.currentWeeklyStatus == 1 {
            let boosted = (model.weeklyBoostPermille ?? 0) > 1000
            metrics.append(
                UsageMetric(label: boosted ? "weekly_limit_boosted" : "weekly_limit", currentValue: weeklyRemainingPct, totalValue: 100, unit: "%", resetTime: weeklyResetTime)
            )
        } else {
            metrics.append(
                UsageMetric(
                    label: "weekly_limit_unlimited",
                    currentValue: 0,
                    totalValue: nil,
                    unit: "unlimited",
                    resetTime: nil
                )
            )
        }
```

- [ ] **Step 4: Update existing weekly-omitted test**

In `Tests/Platforms/MiniMaxPlatformTests.swift`, replace the existing `testFetchUsageWeeklyOmittedWhenStatusIsNotOne` (around line 338-365) — it asserts `metrics.count == 1`. Since the new behavior always returns 2, rename it and update the assertion:

```swift
    func testFetchUsageWeeklyUnlimitedWhenStatusIsNotOne() async throws {
        // current_weekly_status 不是 1 → 返回 weekly_limit_unlimited (不是省略).
        // 跟 spec "可配置指标显示" 一致: 上层永远按 metrics 列表渲染.
        let json = """
        {
            "model_remains": [{
                "model_name": "general",
                "current_interval_remaining_percent": 80.0,
                "current_weekly_remaining_percent": 50.0,
                "current_weekly_status": 3
            }]
        }
        """
        service.clearCache()
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        let config = PlatformConfigData(
            platformType: .minimax_cn,
            apiBaseURL: "https://test.com",
            authHeader: "Authorization",
            authPrefix: "Bearer ",
            apiKey: "test-key"
        )

        let result = try await service.fetchUsage(config: config, network: mockNetwork)
        XCTAssertEqual(result.metrics.count, 2)
        XCTAssertEqual(result.metrics[0].label, "five_hour")
        XCTAssertEqual(result.metrics[1].label, "weekly_limit_unlimited")
    }
```

- [ ] **Step 5: Run all MiniMax tests to verify they pass**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/MiniMaxPlatformTests`
Expected: PASS (all MiniMax tests)

---

## Task 4: StatusBarView — filter by enabledMetrics + 0/1/2 layout + ∞ rendering

**Files:**
- Modify: `Views/StatusBarView.swift`
- Create: `Tests/Views/StatusBarViewTests.swift`

- [ ] **Step 1: Write the failing test**

Create `Tests/Views/StatusBarViewTests.swift`:

```swift
import XCTest
import SwiftUI
@testable import QuotaBar

final class StatusBarViewTests: XCTestCase {
    // 测试 StatusBarView 渲染时按 enabledMetrics 过滤 + ∞ 渲染.
    // SwiftUI 视图不直接抛值, 用 mirror 取出 internal _body 字段做 snapshot 成本太高,
    // 改为: 把渲染逻辑抽成 pure helper (formatMetricText), 直接对 helper 单测.
    // 这里只验证 enabledMetrics 过滤顺序和 ∞ 文案.

    func testEnabledMetricsFiltersAndOrders() {
        // 模拟数据: 5h, weekly_limit, mcp_monthly
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 90, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "mcp_monthly", currentValue: 50, totalValue: 100, unit: "times", resetTime: nil)
        ]

        // 用户只勾 5h + mcp_monthly
        let enabled: [String] = ["five_hour", "mcp_monthly"]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: enabled)
        XCTAssertEqual(visible.map(\.label), ["five_hour", "mcp_monthly"])
    }

    func testEnabledMetricsNilReturnsAll() {
        // enabledLabels == nil 表示"不过滤" (兼容老调用)
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 90, totalValue: 100, unit: "%", resetTime: nil)
        ]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: nil)
        XCTAssertEqual(visible.map(\.label), ["five_hour", "weekly_limit"])
    }

    func testEnabledMetricsOrderFollowsEnabledList() {
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 90, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "mcp_monthly", currentValue: 50, totalValue: 100, unit: "times", resetTime: nil)
        ]
        // 用户期望顺序: mcp_monthly 在前
        let enabled: [String] = ["mcp_monthly", "five_hour"]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: enabled)
        XCTAssertEqual(visible.map(\.label), ["mcp_monthly", "five_hour"])
    }

    func testFormatMetricTextReturnsInfinityForUnlimited() {
        let unlimited = UsageMetric(label: "weekly_limit_unlimited", currentValue: 0, totalValue: nil, unit: "unlimited", resetTime: nil)
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(unlimited, displayMode: .remaining), "∞")
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(unlimited, displayMode: .used), "∞")
    }

    func testFormatMetricTextReturnsPercentageWhenTotalPresent() {
        let m = UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil)
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(m, displayMode: .remaining), "80%")
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(m, displayMode: .used), "20%")
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/StatusBarViewTests`
Expected: FAIL with "cannot find 'StatusBarViewHelper' in scope"

- [ ] **Step 3: Refactor StatusBarView with helper + new init param**

In `Views/StatusBarView.swift`, replace the entire file with:

```swift
import SwiftUI

/// 把渲染逻辑抽成 pure helper, 让单测可以脱离 SwiftUI runtime 直接验证过滤 / 格式.
enum StatusBarViewHelper {
    /// 按 enabledLabels 过滤并排序 metrics. nil 表示"不过滤", 兼容老调用.
    /// 顺序: 严格按 enabledLabels 给的顺序; enabledLabels 里没有的 metric 会被丢弃.
    static func visibleMetrics(from metrics: [UsageMetric], enabledLabels: [String]?) -> [UsageMetric] {
        guard let enabledLabels else { return metrics }
        let byLabel = Dictionary(uniqueKeysWithValues: metrics.map { ($0.label, $0) })
        return enabledLabels.compactMap { byLabel[$0] }
    }

    /// 单个 metric 的渲染文本.
    ///   - unit == "unlimited" → "∞"
    ///   - totalValue > 0       → 按 displayMode (remaining/used) 算百分比
    ///   - 其它 (无 total)      → formatBalance
    static func formatMetricText(_ metric: UsageMetric, displayMode: DisplayMode) -> String {
        if metric.unit == "unlimited" { return "∞" }
        guard let total = metric.totalValue, total > 0 else {
            return formatBalance(metric.currentValue)
        }
        let ratio: Double
        switch displayMode {
        case .remaining:
            ratio = metric.currentValue / total
        case .used:
            ratio = (total - metric.currentValue) / total
        }
        return "\(Int(ratio * 100))%"
    }

    private static func formatBalance(_ value: Double) -> String {
        if value >= 1000 { return String(format: "%.1fK", value / 1000) }
        if value >= 100 { return String(format: "%.0f", value) }
        return String(format: "%.1f", value)
    }
}

struct StatusBarView: View {
    let platformData: PlatformUsageData?
    var displayMode: DisplayMode = .used
    var enabledMetrics: [String]? = nil  // nil = 不过滤, 兼容老调用

    init(platformData: PlatformUsageData?, displayMode: DisplayMode = .used, enabledMetrics: [String]? = nil) {
        self.platformData = platformData
        self.displayMode = displayMode
        self.enabledMetrics = enabledMetrics
    }

    private var visibleMetrics: [UsageMetric] {
        StatusBarViewHelper.visibleMetrics(from: platformData?.metrics ?? [], enabledLabels: enabledMetrics)
    }

    private var primaryText: String {
        let v = visibleMetrics
        guard !v.isEmpty else { return "--" }
        return StatusBarViewHelper.formatMetricText(v[0], displayMode: displayMode)
    }

    private var secondaryText: String? {
        let v = visibleMetrics
        guard v.count > 1 else { return nil }
        return StatusBarViewHelper.formatMetricText(v[1], displayMode: displayMode)
    }

    private var statusColor: Color {
        guard let data = platformData else { return .secondary }
        if data.metrics.isEmpty { return .secondary }
        // 颜色只看第一个 metric (5h 是最重要的); ∞ 不参与颜色.
        if let metric = data.metrics.first, metric.unit != "unlimited",
           let total = metric.totalValue, total > 0 {
            let remainingRatio = metric.currentValue / total
            if remainingRatio < 0.1 { return .red }
            if remainingRatio < 0.5 { return .yellow }
            return .green
        }
        return data.isHealthy ? .green : .red
    }

    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: "circle.fill")
                .font(.system(size: 12))
                .frame(width: 12)
                .foregroundColor(statusColor)

            // 0 / 1 个 metric: 大字居中; 2 个: 当前上下两行布局.
            if let secondary = secondaryText {
                VStack(alignment: .leading, spacing: 0) {
                    Text(primaryText)
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .lineLimit(1)
                    Text(secondary)
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            } else {
                // 0 / 1 个 metric: 大字居中.
                // 字号按 NSStatusBar 系统厚度减内边距算, 避免被菜单栏裁切.
                // 22pt 高度 - 上下各 2pt 内边距 = 18pt 上限. 用户实测后可能再调.
                Text(primaryText)
                    .font(.system(size: 18, weight: .medium, design: .monospaced))
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            }
        }
        .padding(.horizontal, 2)
        .frame(minWidth: 36, idealWidth: 44, maxWidth: .infinity, minHeight: 22, idealHeight: 22, maxHeight: 22, alignment: .leading)
    }
}

#Preview {
    StatusBarView(platformData: nil)
}
```

- [ ] **Step 4: Run StatusBarView tests**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test -only-testing:quota-bar-tests/StatusBarViewTests`
Expected: PASS (5 tests)

- [ ] **Step 5: Build to catch type errors elsewhere**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -configuration Debug build`
Expected: BUILD SUCCEEDED (StatusBarView 是 public API, init 加默认参数后所有调用方兼容)

---

## Task 5: StatusBarController — wire up enabled-metrics submenu + pass-through + listener

**Files:**
- Modify: `StatusBar/StatusBarController.swift`

- [ ] **Step 1: Pass `enabledMetrics` into `StatusBarView` constructors**

In `StatusBar/StatusBarController.swift`, update four call sites that construct `StatusBarView`:

1. In `init()` line ~23:
```swift
        let statusBarContentView = StatusBarView(
            platformData: viewModel.activePlatformData,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: .minimax_cn)
        )
```

2. In `updateStatusBarView()` line ~420:
```swift
    private func updateStatusBarView() {
        let active = viewModel.activePlatform
        statusBarView.update(rootView: StatusBarView(
            platformData: viewModel.activePlatformData,
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: active)
        ))
        statusBarView.layoutSubtreeIfNeeded()
    }
```

3. In `updateAll(data:)` line ~450 (active-platform branch):
```swift
        statusItem.isVisible = true
        statusBarView.update(rootView: StatusBarView(
            platformData: viewModel.activePlatformData,
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: viewModel.activePlatform)
        ))
        statusBarView.layoutSubtreeIfNeeded()
```

4. In `updatePinnedItem(_:data:)` line ~462:
```swift
    private func updatePinnedItem(_ platform: PlatformType, data: PlatformUsageData?) {
        guard let view = pinnedViews[platform] else { return }

        view.update(rootView: StatusBarView(
            platformData: data,
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: platform)
        ))
        view.layoutSubtreeIfNeeded()
```

5. In `createPinnedItem(for:)` line ~513:
```swift
        let view = RightClickStatusBarView(rootView: StatusBarView(
            platformData: viewModel.platformData[platform],
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: platform)
        ))
```

- [ ] **Step 2: Listen for `enabledMetricsChanged` notification**

In `StatusBar/StatusBarController.swift`, find `init()` (around line 18) where existing `platformEnabledChanged` observer is added. Add a sibling observer:

```swift
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleEnabledMetricsChanged(_:)),
            name: .enabledMetricsChanged,
            object: nil
        )
```

Add the new selector method (place it near `handlePlatformChanged` around line 482):

```swift
    @objc private func handleEnabledMetricsChanged(_ note: Notification) {
        // 通知的 object 是 PlatformType (来自 setter), 用于决定重绘范围.
        // 简化: 直接重绘全部 status item, 避免按平台分支.
        updateAll(data: viewModel.platformData)
    }
```

- [ ] **Step 3: Add the "Enabled Metrics" submenu to right-click menu**

In `StatusBar/StatusBarController.swift`, find `showDisplaySettingsSubmenu(from:)` (around line 127). The right-click menu assembly block adds items in order: refreshNow, displaySettings, refreshItem, platformItem, languageItem. Add an `enabledMetricsItem` between `platformItem` and `languageItem`.

Find this block (around line 230):
```swift
        rootMenu.addItem(displaySettingsItem)
        rootMenu.addItem(refreshItem)
        rootMenu.addItem(platformItem)
        rootMenu.addItem(languageItem)
        rootMenu.addItem(NSMenuItem.separator())
```

Replace with:
```swift
        rootMenu.addItem(displaySettingsItem)
        rootMenu.addItem(refreshItem)
        rootMenu.addItem(platformItem)
        rootMenu.addItem(enabledMetricsItem)
        rootMenu.addItem(languageItem)
        rootMenu.addItem(NSMenuItem.separator())
```

Now add the construction of `enabledMetricsItem` just above the "Language submenu" section (around line 204). Add this block:

```swift
        // Enabled Metrics submenu: 每个平台一个子菜单, 含该平台所有可显示指标的多选.
        // 已勾 2 个时第 3 个菜单项禁用, 不让超过上限.
        let enabledMetricsMenu = NSMenu()
        let availableLabels = ["five_hour", "weekly_limit", "mcp_monthly"]
        for platform in PlatformType.allCases {
            let platSub = NSMenu()
            let current = ConfigService.shared.enabledMetrics(for: platform)
            for label in availableLabels {
                let item = NSMenuItem(
                    title: I18nService.shared.translate("menu.metric.\(label)"),
                    action: #selector(toggleEnabledMetric(_:)),
                    keyEquivalent: ""
                )
                item.target = self
                item.representedObject = ["platform": platform, "label": label]
                let checked = current.contains(label)
                let atLimit = current.count >= 2 && !checked
                item.state = checked ? .on : .off
                item.isEnabled = !atLimit
                platSub.addItem(item)
            }
            let platTitleItem = NSMenuItem(title: platform.displayName, action: nil, keyEquivalent: "")
            platTitleItem.submenu = platSub
            enabledMetricsMenu.addItem(platTitleItem)
        }
        let enabledMetricsItem = NSMenuItem(title: I18nService.shared.translate("menu.enabledMetrics"), action: nil, keyEquivalent: "")
        enabledMetricsItem.submenu = enabledMetricsMenu
```

- [ ] **Step 4: Add the toggle action method**

In `StatusBar/StatusBarController.swift`, add this method near `setDisplayModeUsed` (around line 320):

```swift
    @objc private func toggleEnabledMetric(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any],
              let platform = payload["platform"] as? PlatformType,
              let label = payload["label"] as? String else { return }

        var current = ConfigService.shared.enabledMetrics(for: platform)
        if current.contains(label) {
            current.removeAll { $0 == label }
        } else {
            current.append(label)
        }
        ConfigService.shared.setEnabledMetrics(current, for: platform)
        // 通知已由 setter 发, 无需再手动 post.
        // 同步更新菜单项状态 (因为菜单已弹出, 不会重新构造).
        let atLimit = current.count >= 2
        for item in sender.menu?.items ?? [] {
            guard let p = item.representedObject as? [String: Any],
                  let l = p["label"] as? String else { continue }
            let isCurrent = current.contains(l)
            item.state = isCurrent ? .on : .off
            item.isEnabled = !(atLimit && !isCurrent)
        }
    }
```

- [ ] **Step 5: Build to verify**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -configuration Debug build`
Expected: BUILD SUCCEEDED

---

## Task 6: i18n — add new keys

**Files:**
- Modify: `Resources/en.json`
- Modify: `Resources/zh-Hans.json`

- [ ] **Step 1: Add English keys**

In `Resources/en.json`, add (anywhere in the JSON object):

```json
    "menu.enabledMetrics": "Enabled Metrics",
    "menu.metric.five_hour": "5-Hour Quota",
    "menu.metric.weekly_limit": "Weekly Quota",
    "menu.metric.mcp_monthly": "MCP Monthly",
    "metric.weekly_limit_unlimited": "Unlimited",
```

- [ ] **Step 2: Add Chinese keys**

In `Resources/zh-Hans.json`, add:

```json
    "menu.enabledMetrics": "显示指标",
    "menu.metric.five_hour": "5 小时额度",
    "menu.metric.weekly_limit": "周额度",
    "menu.metric.mcp_monthly": "MCP 月度",
    "metric.weekly_limit_unlimited": "无限",
```

- [ ] **Step 3: Build to verify**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -configuration Debug build`
Expected: BUILD SUCCEEDED

---

## Task 7: Update README + spec cross-link

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`

- [ ] **Step 1: Update English README**

In `README.md`, find the "功能" / "Features" section (around line 17-28 of `README_zh.md`). Add a bullet point under existing feature list:

```markdown
- Selectable metrics per platform: choose up to 2 of 5-hour / weekly / MCP via right-click menu; menu bar adapts font size automatically
```

(Place in the English `README.md` under "Features" section.)

- [ ] **Step 2: Update Chinese README**

In `README_zh.md`, add:

```markdown
- 每个平台可选择最多 2 个指标显示（5 小时额度 / 周额度 / MCP 月度），通过右键菜单配置；菜单栏字号自动适配
```

---

## Task 8: Build + run all tests

- [ ] **Step 1: Generate project + build**

Run: `xcodegen generate && xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -configuration Debug build`
Expected: BUILD SUCCEEDED

- [ ] **Step 2: Run all unit tests**

Run: `xcodebuild -project quota-bar.xcodeproj -scheme quota-bar -destination 'platform=macOS' test`
Expected: ALL TESTS PASS (including the 6 new + 1 updated + 5 StatusBarView tests)

---

## Task 9: User-driven manual verification (HUMAN-IN-THE-LOOP)

This task is intentionally executed by the human, not by the agent. The agent pauses and reports results.

- [ ] **Step 1: Agent runs the app and screenshots**

Agent launches the built app via `open dist/QuotaBar.app` (or whatever build path) and captures three screenshots:
- Screenshot A: Default state — MiniMax shows 5h only, GLM shows 5h + weekly
- Screenshot B: Right-click → Enabled Metrics → MiniMax → enable "Weekly Quota" → menu bar updates to show 5h + ∞
- Screenshot C: Right-click → Enabled Metrics → GLM → disable "Weekly Quota" (only 5h left) → font size jumps larger, centered

Agent presents all three screenshots to user.

- [ ] **Step 2: User verifies font size does not clip**

User opens the screenshots, confirms:
- No metric text is clipped by menu bar height
- Single-metric font size is visibly larger than two-metric (target ~1.5–2x)
- ∞ displays correctly for MiniMax without weekly limit
- Popover detail view still shows all metrics unchanged

User reports OK or requests font-size adjustment. If adjustment needed, agent modifies the `size: 18` literal in `StatusBarView.swift` (Task 4) per user's actual measurement, no spec change required.

- [ ] **Step 3: User authorizes commit**

Only after Step 2 passes, user explicitly says "commit". Agent then:
1. Shows `git status` / `git diff --stat` summary
2. After user re-confirms, runs `git add` + `git commit` with a message like `feat: configurable metric display + ∞ for unlimited plans`
3. **Never** pushes, creates PR, or releases — those require separate user authorization per `CLAUDE.md`

---

## Migration / Compatibility

- **Old users without `enabledMetrics` key**: returns platform defaults (MiniMax=`["five_hour"]`, GLM=`["five_hour", "weekly_limit"]`)
- **Existing tests referencing weekly omission**: updated in Task 3 Step 4 to match new behavior
- **Popover unchanged**: still shows all metrics in detail view
- **Right-click menu extended, not replaced**: existing items retain their position

---

## Plan complete and saved to `docs/superpowers/plans/2026-07-21-configurable-metric-display.md`

**Two execution options:**

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**