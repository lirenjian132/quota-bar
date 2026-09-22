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

    func testAvailableMetricLabelsPerPlatform() {
        // R3-2: 清单必须与各平台 service 实际产出的 label 对齐.
        // MiniMax 产 four 态 (boosted=加成套餐, unlimited=∞), 不产 mcp_monthly;
        // GLM 产 [five_hour, weekly_limit, mcp_monthly], 无 boosted/unlimited.
        // P0-1: stepfun 的菜单可勾选项必须含窗口降级标签 — 非 credit 套餐族
        // (plan_family != 2) 产出 five_hour/weekly_limit, 只列 credits 会让这类
        // 用户状态栏恒显 "--" 且右键菜单无可选项.
        XCTAssertEqual(
            ConfigService.availableMetricLabels(for: .minimax_cn),
            ["five_hour", "weekly_limit", "weekly_limit_boosted", "weekly_limit_unlimited"]
        )
        XCTAssertEqual(
            ConfigService.availableMetricLabels(for: .glm_cn),
            ["five_hour", "weekly_limit", "mcp_monthly"]
        )
        XCTAssertEqual(
            ConfigService.availableMetricLabels(for: .stepfun),
            ["credits", "five_hour", "weekly_limit"]
        )
        XCTAssertEqual(ConfigService.availableMetricLabels(for: .tokenrhythm), ["balance"])
    }

    /// R3-2 对照回归: 用各平台 service 的 fixture 真实拉取产出, 断言"服务可产出的
    /// 每个 label 都在菜单可勾选清单里" — 防以后加 label 时漏更 availableMetricLabels
    /// (漏了就会被 visibleMetrics 精确匹配过滤, 用户状态栏恒 "--" 且无可勾选项).
    func testAvailableMetricLabelsCoverEveryServiceProducedLabel() async throws {
        // MiniMax: 标准周 / 加成周 / 无限周三种 fixture.
        let minimaxMock = MockNetworkService()
        let minimaxService = MiniMaxPlatformAPIService()
        func minimaxFetch(weeklyBoostPermille: Int?, weeklyStatus: Int?) async throws -> PlatformUsageData {
            minimaxService.clearCache()
            var remain = "\"current_interval_remaining_percent\": 80.0, \"current_weekly_remaining_percent\": 50.0"
            if let weeklyStatus { remain += ", \"current_weekly_status\": \(weeklyStatus)" }
            if let weeklyBoostPermille { remain += ", \"weekly_boost_permille\": \(weeklyBoostPermille)" }
            minimaxMock.mockData = """
            {"model_remains": [{"model_name": "general", \(remain)}]}
            """.data(using: .utf8)
            minimaxMock.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)
            let config = PlatformConfigData(
                platformType: .minimax_cn,
                apiBaseURL: "https://test.com",
                authHeader: "Authorization",
                authPrefix: "Bearer ",
                apiKey: "test-key"
            )
            return try await minimaxService.fetchUsage(config: config, network: minimaxMock)
        }
        let standard = try await minimaxFetch(weeklyBoostPermille: nil, weeklyStatus: 1)
        let boosted = try await minimaxFetch(weeklyBoostPermille: 1500, weeklyStatus: 1)
        let unlimited = try await minimaxFetch(weeklyBoostPermille: nil, weeklyStatus: 3)
        for data in [standard, boosted, unlimited] {
            for metric in data.metrics {
                XCTAssertTrue(
                    ConfigService.availableMetricLabels(for: .minimax_cn).contains(metric.label),
                    "MiniMax 产出的 \(metric.label) 必须可在右键菜单勾选"
                )
            }
        }

        // GLM: five_hour / weekly_limit / mcp_monthly.
        let glmMock = MockNetworkService()
        let glmService = GLMPlatformAPIService()
        glmMock.mockData = """
        {
            "code": 200, "msg": "success", "success": true,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20},
                    {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 10},
                    {"type": "TIME_LIMIT", "usage": 1000, "currentValue": 68, "remaining": 932}
                ],
                "level": "v1"
            }
        }
        """.data(using: .utf8)
        glmMock.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)
        let glmData = try await glmService.fetchUsage(
            config: PlatformConfigData(
                platformType: .glm_cn, apiBaseURL: "https://test.com",
                authHeader: "Authorization", authPrefix: "Bearer ", apiKey: "test-key"
            ),
            network: glmMock
        )
        for metric in glmData.metrics {
            XCTAssertTrue(
                ConfigService.availableMetricLabels(for: .glm_cn).contains(metric.label),
                "GLM 产出的 \(metric.label) 必须可在右键菜单勾选"
            )
        }

        // Stepfun: credit 套餐 credits / 非 credit 套餐 five_hour+weekly_limit.
        let stepfunMock = MockNetworkService()
        let stepfunService = StepFunPlatformAPIService()
        let stepfunConfig = PlatformConfigData(
            platformType: .stepfun,
            apiBaseURL: "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard",
            authHeader: "Cookie",
            authPrefix: "",
            apiKey: "Oasis-Webid=abc; Oasis-Token=def"
        )
        stepfunMock.mockData = """
        {"status":1,"desc":"","plan_credit_rate_limit":{"subscription_credit_left_rate":0.96}}
        """.data(using: .utf8)
        stepfunMock.mockResponse = MockNetworkService.makeResponse(
            url: "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit", statusCode: 200)
        let creditData = try await stepfunService.fetchUsage(config: stepfunConfig, network: stepfunMock)
        for metric in creditData.metrics {
            XCTAssertTrue(ConfigService.availableMetricLabels(for: .stepfun).contains(metric.label))
        }
        stepfunService.clearCache()
        stepfunMock.mockData = """
        {"status":1,"desc":"","plan_family":1,"five_hour_usage_left_rate":0.42,"weekly_usage_left_rate":0.87}
        """.data(using: .utf8)
        let windowData = try await stepfunService.fetchUsage(config: stepfunConfig, network: stepfunMock)
        for metric in windowData.metrics {
            XCTAssertTrue(ConfigService.availableMetricLabels(for: .stepfun).contains(metric.label))
        }

        // TokenRhythm: balance.
        let trMock = MockNetworkService()
        let trService = TokenRhythmPlatformAPIService()
        trMock.mockData = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"253.76"}}
        """.data(using: .utf8)
        trMock.mockResponse = MockNetworkService.makeResponse(
            url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)
        let trData = try await trService.fetchUsage(
            config: PlatformConfigData(
                platformType: .tokenrhythm, apiBaseURL: "https://tokenrhythm.studio/api/wallet/summary",
                authHeader: "Cookie", authPrefix: "tr_session=", apiKey: "sess_x"
            ),
            network: trMock
        )
        for metric in trData.metrics {
            XCTAssertTrue(ConfigService.availableMetricLabels(for: .tokenrhythm).contains(metric.label))
        }
    }

    func testVisibleMetricsFallsBackWhenNoIntersection() {
        // R3-3 防呆: 勾选与产出无交集 (默认勾 credits, 实际是非 credit 套餐产出
        // five_hour/weekly_limit) 时回退前 2 个, 状态栏不再恒显 "--" 逼用户翻菜单.
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 42, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 87, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "mcp_monthly", currentValue: 50, totalValue: 100, unit: "times", resetTime: nil)
        ]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["credits"])
        XCTAssertEqual(visible.map(\.label), ["five_hour", "weekly_limit"], "无交集回退前 2 个 (上限 2)")

        // 有交集时行为不变: 严格按 enabledLabels 顺序, 未勾的丢弃.
        let partial = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["weekly_limit"])
        XCTAssertEqual(partial.map(\.label), ["weekly_limit"])

        // enabledLabels == nil 老调用不过滤; 空 metrics 回退也为空 (不造数据).
        XCTAssertEqual(StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: nil).count, 3)
        XCTAssertTrue(StatusBarViewHelper.visibleMetrics(from: [], enabledLabels: ["credits"]).isEmpty)
    }

    // MARK: - A4-2 同族匹配 (miniMax P0-1 + DeepSeek A2 复现)

    func testVisibleMetricsMatchesWeeklyLimitFamilyWhenServiceProducesBoosted() {
        // P0-1 复现: 用户勾 weekly_limit 后套餐加成 → service 产 boosted label.
        // 精确匹配过滤空 → prefix(2) 防呆把用户没勾的 five_hour 复活.
        // 同族匹配后必须只显示产出的实际 label (weekly_limit_boosted).
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit_boosted", currentValue: 45, totalValue: 100, unit: "%", resetTime: nil)
        ]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["weekly_limit"])
        XCTAssertEqual(visible.map(\.label), ["weekly_limit_boosted"],
                       "勾族名 weekly_limit 应匹配 boosted 产出, 不得回退复活 five_hour")
    }

    func testVisibleMetricsMatchesWeeklyLimitFamilyWhenServiceProducesUnlimited() {
        // ∞ 套餐 (weekly_status != 1): 产 weekly_limit_unlimited. 同族匹配必须接到.
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit_unlimited", currentValue: 0, totalValue: nil, unit: "unlimited", resetTime: nil)
        ]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["weekly_limit"])
        XCTAssertEqual(visible.map(\.label), ["weekly_limit_unlimited"])
        XCTAssertFalse(visible.map(\.label).contains("five_hour"), "不得显示用户未勾选的 five_hour")
        // 渲染文本: unlimited → ∞
        XCTAssertEqual(StatusBarViewHelper.formatMetricText(visible[0], displayMode: .remaining), "∞")
    }

    func testVisibleMetricsFamilyMatchWhenUserCheckedSpecificFamilyMember() {
        // 用户勾的是族内具体 label (如 weekly_limit_boosted), 套餐回落到标准态:
        // 整族展开后仍显示周额度 (weekly_limit), 不回退到 five_hour.
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 50, totalValue: 100, unit: "%", resetTime: nil)
        ]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["weekly_limit_boosted"])
        XCTAssertEqual(visible.map(\.label), ["weekly_limit"])

        // 勾 boosted 而产出 standard + five_hour 都在: 只出周额度, 且 enabledLabels
        // 顺序仍受尊重 (five_hour 在前但用户没勾 → 不出现).
        let mixed = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["weekly_limit_unlimited"])
        XCTAssertEqual(mixed.map(\.label), ["weekly_limit"])
    }

    func testVisibleMetricsFamilyMatchKeepsOrderAndDedup() {
        // 同族展开不得打乱顺序, 也不得因族名与族内 label 重复勾选产生重复项.
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 80, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 50, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "mcp_monthly", currentValue: 50, totalValue: 100, unit: "times", resetTime: nil)
        ]
        // 用户同时勾了族名和族内 label (菜单构造允许): 展开去重后 weekly_limit 只出现一次.
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ["weekly_limit", "weekly_limit", "mcp_monthly"])
        XCTAssertEqual(visible.map(\.label), ["weekly_limit", "mcp_monthly"])
    }

    func testVisibleMetricsStepFunNonCreditFallsBackToWindowMetrics() {
        // Stepfun 非 credit 套餐新用户: 默认勾 credits, 产出 five_hour/weekly_limit.
        // 同族匹配无交集 → prefix(2) 防呆回退 [five_hour, weekly_limit] (R3-3/A4-2 兜底).
        let metrics = [
            UsageMetric(label: "five_hour", currentValue: 42, totalValue: 100, unit: "%", resetTime: nil),
            UsageMetric(label: "weekly_limit", currentValue: 87, totalValue: 100, unit: "%", resetTime: nil)
        ]
        let visible = StatusBarViewHelper.visibleMetrics(from: metrics, enabledLabels: ConfigService.defaultEnabledMetrics(for: .stepfun))
        XCTAssertEqual(visible.map(\.label), ["five_hour", "weekly_limit"])
    }

    func testStatusColorEmptyMetricsSecondaryEvenWhenUnhealthy() {
        // P2-8: 空 metrics + isHealthy=false (如 GLM 空 limits) → secondary.
        // 旧顺序先判 isHealthy 再查空 metrics, 状态栏红而弹窗灰 (弹窗走 noData 分支).
        let emptyUnhealthy = PlatformUsageData(
            platform: .glm_cn,
            instanceID: "glm_cn",
            displayName: "GLM",
            metrics: [],
            lastUpdated: Date(),
            isHealthy: false
        )
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: emptyUnhealthy), .secondary)

        // 空 metrics + 健康同样 secondary (无数据可判)
        let emptyHealthy = PlatformUsageData(
            platform: .glm_cn,
            instanceID: "glm_cn",
            displayName: "GLM",
            metrics: [],
            lastUpdated: Date(),
            isHealthy: true
        )
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: emptyHealthy), .secondary)
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

    func testStatusColorUnhealthyOverridesPercentage() {
        // 订阅失效 (isHealthy=false) 对所有平台优先: StepFun 停订后 credits 余量
        // 仍有 96.9% (>50%), 旧逻辑在百分比分支直接返回绿, 与弹窗矛盾.
        let unhealthy = PlatformUsageData(
            platform: .stepfun,
            instanceID: "stepfun",
            displayName: "Stepfun",
            metrics: [UsageMetric(label: "credits", currentValue: 96.886694, totalValue: 100, unit: "%", resetTime: nil)],
            lastUpdated: Date(),
            isHealthy: false
        )
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: unhealthy), .red)

        // 健康且余量高 → 仍走百分比阈值 (绿).
        let healthy = PlatformUsageData(
            platform: .stepfun,
            instanceID: "stepfun",
            displayName: "Stepfun",
            metrics: [UsageMetric(label: "credits", currentValue: 96.886694, totalValue: 100, unit: "%", resetTime: nil)],
            lastUpdated: Date(),
            isHealthy: true
        )
        XCTAssertEqual(StatusBarViewHelper.statusColor(for: healthy), .green)
    }
}