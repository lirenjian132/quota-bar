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
}