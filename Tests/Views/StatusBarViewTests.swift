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

    func testFormatMetricTextReturnsNumberWhenTotalPresent() {
        // 菜单栏空间紧: 渲染文本只显示数字 (e.g. "80"), 不带 %, 颜色变化由
        // 左边圆点提示. Popover 详情面板仍显示完整 "80%".
        let m = UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil)
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(m, displayMode: .remaining), "80")
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(m, displayMode: .used), "20")
    }

    func testFormatMetricTextBalanceShowsInteger() {
        // 余额型 metric (无 total): 一律四舍五入到整数, 个位精度足够判断换账号.
        let m = UsageMetric(label: "balance", currentValue: 253.762, totalValue: nil, unit: "CNY", resetTime: nil)
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(m, displayMode: .remaining), "254")
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(m, displayMode: .used), "254")

        let low = UsageMetric(label: "balance", currentValue: 8.49, totalValue: nil, unit: "CNY", resetTime: nil)
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(low, displayMode: .used), "8")

        let zero = UsageMetric(label: "balance", currentValue: 0, totalValue: nil, unit: "CNY", resetTime: nil)
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(zero, displayMode: .used), "0")
    }

    private func balanceData(expiry: Date?, healthy: Bool) -> PlatformUsageData {
        PlatformUsageData(
            platform: .tokenrhythm,
            instanceID: "tokenrhythm",
            displayName: "T1",
            metrics: [UsageMetric(label: "balance", currentValue: 100, totalValue: nil, unit: "CNY", resetTime: expiry)],
            lastUpdated: Date(),
            isHealthy: healthy
        )
    }

    func testStatusColorBalanceExpiryTiers() {
        // 到期 3 天内 → 红 (钱马上蒸发, 最急)
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: balanceData(expiry: Date().addingTimeInterval(2 * 86400), healthy: true)), .red)
        // 到期 7 天内 (但 > 3 天) → 黄
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: balanceData(expiry: Date().addingTimeInterval(5 * 86400), healthy: true)), .yellow)
        // 无到期信息 + 健康 → 绿
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: balanceData(expiry: nil, healthy: true)), .green)
        // 无到期信息 + 不健康 (低余额) → 红
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: balanceData(expiry: nil, healthy: false)), .red)
        // nil 数据 / 空 metrics → secondary
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: nil), .secondary)
    }

    func testStatusColorPercentageTiersUnchanged() {
        // 回归: 百分比型 (有 total) 的红黄绿阈值不受余额型新增逻辑影响.
        func pctData(_ value: Double) -> PlatformUsageData {
            PlatformUsageData(
                platform: .glm_cn,
                instanceID: "glm_cn",
                displayName: "GLM",
                metrics: [UsageMetric(label: "five_hour", currentValue: value, totalValue: 100, unit: "%", resetTime: nil)],
                lastUpdated: Date(),
                isHealthy: true
            )
        }
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: pctData(5)), .red)
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: pctData(30)), .yellow)
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: pctData(80)), .green)
    }
}