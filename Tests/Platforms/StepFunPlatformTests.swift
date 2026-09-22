import XCTest
@testable import QuotaBar

final class StepFunPlatformTests: XCTestCase {
    var mockNetwork: MockNetworkService!
    var service: StepFunPlatformAPIService!

    override func setUp() {
        super.setUp()
        mockNetwork = MockNetworkService()
        service = StepFunPlatformAPIService()
    }

    // template 同款: 整串 cookie 存 apiKey (Webid + Token 两个值).
    private let webid = "2c321138f52a7d858f2d9eab619a357038d0f66c"
    private var cookiePair: String {
        "Oasis-Webid=\(webid); Oasis-Token=header.payload.signature"
    }

    private func makeConfig(apiKey: String? = nil) -> PlatformConfigData {
        PlatformConfigData(
            platformType: .stepfun,
            apiBaseURL: "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard",
            authHeader: "Cookie",
            authPrefix: "",
            apiKey: apiKey ?? cookiePair
        )
    }

    // 请求1 (rate limit) 与 请求2 (plan status) 按序返回.
    private var rateLimitJSON: String {
        """
        {"status":1,"desc":"","five_hour_usage_left_rate":0,"weekly_usage_left_rate":0,"plan_credit_rate_limit":{"subscription_credit_left_rate":0.96886694,"subscription_credit_reset_time":"1792502883","topup_credit_left_rate":0,"credit_buckets":[{"type":1,"credit_total":"8000000000","credit_residual":"7750935720","expire_at":"1825171280","next_reset_at":"1792502883"}]}}
        """
    }

    private var planStatusJSON: String {
        #"{"status":1,"desc":"","subscription":{"plan_type":2,"name":"Pro","status":1,"expired_at":"1825171280","auto_renew":false}}"#
    }

    private func resp(_ json: String, _ statusCode: Int = 200, method: String) -> (Data, HTTPURLResponse) {
        (json.data(using: .utf8)!,
         MockNetworkService.makeResponse(url: "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard/\(method)", statusCode: statusCode))
    }

    func testFetchUsageSuccess() async throws {
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.platform, .stepfun)
        XCTAssertEqual(result.metrics.count, 1)
        XCTAssertEqual(result.metrics[0].label, "credits")
        // 96.886694% 剩余 → currentValue = leftRate * 100
        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001)
        XCTAssertEqual(result.metrics[0].totalValue, 100)
        XCTAssertEqual(result.metrics[0].unit, "%")
        XCTAssertTrue(result.isHealthy, "订阅有效应健康")

        // 请求头: Cookie 整串 + 三个 oasis-* 头, webid 从 cookie 拆出.
        guard let first = mockNetwork.sequenceCapture.first else {
            return XCTFail("缺请求捕获")
        }
        XCTAssertEqual(first.value(forHTTPHeaderField: "Cookie"), cookiePair)
        XCTAssertEqual(first.value(forHTTPHeaderField: "oasis-appid"), "10300")
        XCTAssertEqual(first.value(forHTTPHeaderField: "oasis-platform"), "web")
        XCTAssertEqual(first.value(forHTTPHeaderField: "oasis-webid"), webid)
        // Connect 协议版本头 (上游参考实现固定带 1).
        XCTAssertEqual(first.value(forHTTPHeaderField: "Connect-Protocol-Version"), "1")
        XCTAssertEqual(first.httpMethod, "POST")
        // 第一个请求打的是 rate limit 端点.
        XCTAssertTrue(first.url?.absoluteString.hasSuffix("QueryStepPlanRateLimit") == true)
    }

    func testCreditsResetTimePopulated() async throws {
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        // 1792502883 → 2026-10-20 21:28 北京时间 (月度 credits 池重置)
        let resetTime = try XCTUnwrap(result.metrics[0].resetTime)
        XCTAssertEqual(resetTime.timeIntervalSince1970, 1792502883, accuracy: 1)
    }

    func testExpiredSubscriptionMarksUnhealthy() async throws {
        // 订阅早于当前时间过期 → isHealthy=false (红).
        let expiredJSON = #"{"status":1,"subscription":{"plan_type":2,"name":"Pro","status":0,"expired_at":"1700000000"}}"#
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(expiredJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertFalse(result.isHealthy)
        // 余量仍照常显示.
        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001)
    }

    func testPlanStatusFailureDegradesToCreditsOnly() async throws {
        // 请求2 (plan status) 500: 降级为仅余量显示, 不再判定订阅状态.
        mockNetwork.responseSequence = [
            resp(rateLimitJSON, method: "QueryStepPlanRateLimit"),
            resp("{}", 500, method: "GetStepPlanStatus")
        ]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001)
        XCTAssertTrue(result.isHealthy, "拿不到订阅状态时默认健康 (红黄绿由余量百分比驱动)")
    }

    func testPlanStatus401DegradesToCreditsOnly() async throws {
        // 请求2 401 (状态查询被拒): 同样降级, 主指标不受影响. 预期行为, 钉住防回归.
        mockNetwork.responseSequence = [
            resp(rateLimitJSON, method: "QueryStepPlanRateLimit"),
            resp(#"{"code":"unauthenticated"}"#, 401, method: "GetStepPlanStatus")
        ]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001)
        XCTAssertTrue(result.isHealthy, "401 → 静默降级为仅余量 (与 500 同路径)")
    }

    func testBusinessErrorStatusThrowsAPIErrorWithDesc() async {
        // status != 1 (账户停用/欠费): 透出 desc, 抛 apiError.
        let json = #"{"status":2,"desc":"订阅已停用，请续费后重试"}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit")]

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("业务错误应抛 apiError")
        } catch let error as PlatformError {
            guard case .apiError(_, let message) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertTrue(message.contains("订阅已停用"), "应透出服务端 desc")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testMissingWebidThrowsAPIErrorWithPasteHint() async {
        // cookie 串里漏了 Oasis-Webid: 早失败, 不发必然 401 的请求.
        // 报 apiError 并把粘贴格式说清楚 — invalidResponse ("无效的响应数据") 会误导用户.
        // 文案走 i18n (error.stepfun.cookieFormat), 先显式加载翻译再断言.
        I18nService.shared.loadTranslations()
        I18nService.shared.setLocale("zh-Hans")
        defer { I18nService.shared.setLocale("en") }

        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: "Oasis-Token=header.payload.signature"), network: mockNetwork)
            XCTFail("缺 Oasis-Webid 应早失败")
        } catch let error as PlatformError {
            guard case .apiError(_, let message) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertTrue(message.contains("Oasis-Webid"), "应提示缺哪个 cookie 字段")
            XCTAssertTrue(message.contains("Oasis-Token"), "应给出完整粘贴格式")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
        XCTAssertEqual(mockNetwork.requestCount, 0, "格式错误不应发出任何请求")
    }

    func testStatusFieldTypeMismatchThrowsDecodingError() async {
        // 服务端若把 status 返回成字符串, 走解码错误而非 invalidResponse.
        let json = #"{"status":"1","desc":""}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit")]

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("类型错误应抛 decodingError")
        } catch let error as PlatformError {
            guard case .decodingError = error else {
                return XCTFail("应为 decodingError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testUnauthorizedOn401RateLimit() async {
        mockNetwork.responseSequence = [resp(#"{"code":"unauthenticated"}"#, 401, method: "QueryStepPlanRateLimit")]

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("401 应抛 unauthorized (cookie 过期需重新登录粘贴)")
        } catch let error as PlatformError {
            guard case .unauthorized = error else {
                return XCTFail("应为 unauthorized, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testEmptyCredentialThrowsNotConfigured() async {
        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: "  "), network: mockNetwork)
            XCTFail("空凭据应抛 notConfigured")
        } catch let error as PlatformError {
            guard case .notConfigured = error else {
                return XCTFail("应为 notConfigured, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testLowCreditsStillParses() async throws {
        let lowJSON = """
        {"status":1,"desc":"","plan_credit_rate_limit":{"subscription_credit_left_rate":0.035,"subscription_credit_reset_time":"1792502883"}}
        """
        mockNetwork.responseSequence = [resp(lowJSON, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        // 3.5% 剩余 → 状态栏显示 4, view 层按 <10% 标红.
        XCTAssertEqual(result.metrics[0].currentValue, 3.5, accuracy: 0.001)
    }

    func testDisabledSubscriptionWithFutureExpiryMarksUnhealthy() async throws {
        // 停订 (subscription.status=0) 但 expired_at 在未来 (2030-01-01, 账期未到):
        // 旧逻辑把 statusResponse.status == 1 当入口 guard 后整段判定被 expired_at
        // "未过期" 带偏 → isHealthy 恒 true. status 字段必须独立判定.
        let disabledJSON = #"{"status":1,"desc":"","subscription":{"plan_type":2,"name":"Pro","status":0,"expired_at":"1893456000"}}"#
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(disabledJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertFalse(result.isHealthy, "订阅停用 (status=0) 即使 expired_at 在未来也应判失效")
        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001, "余量仍照常显示")
    }

    func testTopLevelStatusZeroDegradesToCreditsOnly() async throws {
        // P2-6 (miniMax Round 2): 外层 status=0 (查询异常/账户停用) 时 subscription
        // 字段不可信, 旧逻辑此刻也判失效 → 把"查询失败"误判成"停订", 弹窗/状态栏
        // 无端标红. 新语义: 外层 status != 1 → 静默降级, 只显示余量, isHealthy=true.
        // 订阅本身有效 (subscription.status=1) 时更应如此.
        let json = #"{"status":0,"desc":"查询异常","subscription":{"plan_type":2,"name":"Pro","status":1,"expired_at":"1893456000"}}"#
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(json, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertTrue(result.isHealthy, "外层 status=0 时应静默降级为仅余量显示 (不信任 subscription)")
        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001, "余量仍照常显示")
    }

    func testSubscriptionStatusZeroMarksUnhealthyEvenWhenOuterStatusOK() async throws {
        // P2-6 的另一半: 外层 status=1 (查询正常) 且 subscription.status=0 才判失效.
        // (与 testExpiredSubscriptionMarksUnhealthy / testDisabledSubscriptionWithFutureExpiryMarksUnhealthy
        //  互为补充, 这里显式钉住"外层正常 + 订阅停订"的组合.)
        let json = #"{"status":1,"desc":"","subscription":{"plan_type":2,"name":"Pro","status":0,"expired_at":"1893456000"}}"#
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(json, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertFalse(result.isHealthy, "外层查询正常时, subscription.status=0 (停订) 必须判失效")
    }

    func testNonCreditPlanFamilyUsesWindowRates() async throws {
        // 非 credit 套餐族 (plan_family=1): 无 plan_credit_rate_limit, 契约改用
        // five_hour/weekly 窗口剩余率 → 降级生成两个 % 指标 (不整体判无效).
        let json = #"{"status":1,"desc":"","plan_family":1,"five_hour_usage_left_rate":0.42,"weekly_usage_left_rate":0.87}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.metrics.count, 2)
        XCTAssertEqual(result.metrics[0].label, "five_hour")
        XCTAssertEqual(result.metrics[0].currentValue, 42, accuracy: 0.001)
        XCTAssertEqual(result.metrics[0].totalValue, 100)
        XCTAssertEqual(result.metrics[0].unit, "%")
        XCTAssertEqual(result.metrics[1].label, "weekly_limit")
        XCTAssertEqual(result.metrics[1].currentValue, 87, accuracy: 0.001)
        XCTAssertTrue(result.isHealthy)
    }

    func testNonCreditPlanFamilySingleWindowRate() async throws {
        // 只给一个窗口率: 生成对应单个指标, 不报 invalidResponse.
        let json = #"{"status":1,"desc":"","plan_family":1,"five_hour_usage_left_rate":0.42}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.metrics.count, 1)
        XCTAssertEqual(result.metrics[0].label, "five_hour")
        XCTAssertEqual(result.metrics[0].currentValue, 42, accuracy: 0.001)
    }

    func testNonCreditPlanFamilyWeeklyOnlyWindowRate() async throws {
        // 对称性: 只给 weekly 缺失 five_hour 时, 只生成 weekly_limit (不补位/不报错).
        let json = #"{"status":1,"desc":"","plan_family":1,"weekly_usage_left_rate":0.87}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.metrics.count, 1)
        XCTAssertEqual(result.metrics[0].label, "weekly_limit")
        XCTAssertEqual(result.metrics[0].currentValue, 87, accuracy: 0.001)
    }

    func testWindowMetricsVisibleViaStatusBarHelperCrossMatrix() async throws {
        // P0-1 回归 (交叉测试): service 产出的窗口指标 × enabledMetrics 组合.
        // 默认勾选 ["credits"] (credit 套餐主场景) 在窗口指标下可见集为空 — 这正是
        // 状态栏 "--" 的根因; 用户从右键菜单勾上窗口指标后必须可见 (P0-1 修复点).
        let json = #"{"status":1,"desc":"","plan_family":1,"five_hour_usage_left_rate":0.42,"weekly_usage_left_rate":0.87}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]
        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        // 默认勾选: credits 不在窗口指标里 → 无交集, R3-3 防呆回退前 2 个
        // (five_hour/weekly_limit), 状态栏不再恒显 "--" 逼用户翻菜单.
        XCTAssertEqual(
            StatusBarViewHelper.visibleMetrics(from: result.metrics, enabledLabels: ConfigService.defaultEnabledMetrics(for: .stepfun)).map(\.label),
            ["five_hour", "weekly_limit"]
        )
        // 勾一个: 按 enabledLabels 顺序渲染
        XCTAssertEqual(
            StatusBarViewHelper.visibleMetrics(from: result.metrics, enabledLabels: ["weekly_limit"]).map(\.label),
            ["weekly_limit"]
        )
        // 勾两个 (上限 2): 顺序完全由 enabledLabels 决定, 与 metrics 产出顺序无关
        XCTAssertEqual(
            StatusBarViewHelper.visibleMetrics(from: result.metrics, enabledLabels: ["weekly_limit", "five_hour"]).map(\.label),
            ["weekly_limit", "five_hour"]
        )
        // credit 套餐主场景: metrics=[credits] × 默认勾选 → 1 个可见 (原有行为不变)
        let creditVisible = StatusBarViewHelper.visibleMetrics(
            from: [UsageMetric(label: "credits", currentValue: 96.9, totalValue: 100, unit: "%", resetTime: nil)],
            enabledLabels: ConfigService.defaultEnabledMetrics(for: .stepfun)
        )
        XCTAssertEqual(creditVisible.map(\.label), ["credits"])
    }

    func testCacheHitKeepsRequestCountUnchanged() async throws {
        // 同 config 第二次 fetch 命中 service 内 300s 缓存: 不再发请求.
        mockNetwork.responseSequence = [resp(rateLimitJSON, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(mockNetwork.requestCount, 2, "首次: rate limit + plan status 两个请求")

        // 换响应序列也读不到 — 命中缓存应直接返回旧数据.
        mockNetwork.responseSequence = [resp("{}", 500, method: "QueryStepPlanRateLimit")]
        let cached = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(mockNetwork.requestCount, 2, "缓存命中不应再发请求")
        XCTAssertEqual(cached.metrics[0].label, "credits")

        // clearCache 后重新发请求 (换 key 路径依赖这个行为).
        // 清空顺序响应序列 (didSet 会重置游标) 再切单值模式:
        // 否则残留序列项会被重新弹出, 两个端点都拿到错位响应.
        service.clearCache()
        mockNetwork.responseSequence = []
        mockNetwork.mockData = rateLimitJSON.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(
            url: "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit", statusCode: 200)
        let refreshed = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(mockNetwork.requestCount, 4, "清缓存后两个端点都重新发请求")
        XCTAssertEqual(refreshed.metrics[0].label, "credits")
    }

    func testMissingAllPlanFieldsThrowsInvalidResponse() async throws {
        // credits 与两个窗口率全缺: 才是无效响应.
        let json = #"{"status":1,"desc":"","plan_family":1}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit")]

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("无任何可显示指标应抛 invalidResponse")
        } catch let error as PlatformError {
            guard case .invalidResponse = error else {
                return XCTFail("应为 invalidResponse, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testResetTimeIntegerFormParses() async throws {
        // 上游文档 subscription_credit_reset_time 为 "string or integer":
        // 整数形态 (无引号) 曾让纯 String? 字段整体解码失败.
        let json = #"{"status":1,"desc":"","plan_credit_rate_limit":{"subscription_credit_left_rate":0.96886694,"subscription_credit_reset_time":1792502883}}"#
        mockNetwork.responseSequence = [resp(json, method: "QueryStepPlanRateLimit"), resp(planStatusJSON, method: "GetStepPlanStatus")]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.metrics[0].label, "credits", "整数形态 resetTime 不应影响 credits 指标解码")
        XCTAssertEqual(result.metrics[0].currentValue, 96.886694, accuracy: 0.0001)
        let resetTime = try XCTUnwrap(result.metrics[0].resetTime)
        XCTAssertEqual(resetTime.timeIntervalSince1970, 1792502883, accuracy: 1)
    }

    func testBlankWebidThrowsAPIErrorWithPasteHint() async {
        // "Oasis-Webid=   ;" 拆出空白串: 同样是粘错, 早失败, 不发必然失败的请求.
        I18nService.shared.loadTranslations()
        I18nService.shared.setLocale("zh-Hans")
        defer { I18nService.shared.setLocale("en") }

        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: "Oasis-Webid=   ; Oasis-Token=header.payload.signature"), network: mockNetwork)
            XCTFail("空白 webid 应早失败")
        } catch let error as PlatformError {
            guard case .apiError(_, let message) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertTrue(message.contains("Oasis-Webid"), "应提示缺哪个 cookie 字段")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testUnauthorizedOn403RateLimit() async {
        // 403 (风控/拒绝) 与 401 同等对待: 都算凭据问题.
        mockNetwork.responseSequence = [resp(#"{"code":"forbidden"}"#, 403, method: "QueryStepPlanRateLimit")]

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("403 应抛 unauthorized")
        } catch let error as PlatformError {
            guard case .unauthorized = error else {
                return XCTFail("应为 unauthorized, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    // MARK: - DeepSeek 盲区补测 (R3)

    func testServerError5xxThrowsNetworkError() async {
        // 请求1 (rate limit) 5xx → networkError (透出响应体前缀).
        mockNetwork.responseSequence = [resp(#"{"error":"internal"}"#, 500, method: "QueryStepPlanRateLimit")]

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("5xx 应抛 networkError")
        } catch let error as PlatformError {
            guard case .networkError(_, let message) = error else {
                return XCTFail("应为 networkError, 实际: \(error)")
            }
            XCTAssertTrue(message.contains("500"), "应带 HTTP 状态码: \(message)")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testTransportErrorThrowsNetworkError() async {
        // 传输层错误 (连接失败/超时): networkError, 且请求现场留下来供断言.
        mockNetwork.mockError = URLError(.notConnectedToInternet)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("传输层错误应抛 networkError")
        } catch let error as PlatformError {
            guard case .networkError = error else {
                return XCTFail("应为 networkError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
        XCTAssertEqual(mockNetwork.lastRequest?.httpMethod, "POST", "传输失败也应留下请求现场")
    }

    func testMissingWebidMessageComesFromI18nKey() async {
        // R3-8: webid 早失败的硬编码中文文案抽成 i18n key error.stepfun.cookieFormat,
        // message 透传 apiError — 这里显式加载翻译后与 i18n 值比对 (中英双语).
        I18nService.shared.loadTranslations()
        I18nService.shared.setLocale("en")
        let enMessage = I18nService.shared.translate("error.stepfun.cookieFormat")
        XCTAssertTrue(enMessage.contains("Oasis-Webid"), "英文文案需点名缺失字段")
        XCTAssertTrue(enMessage.contains("Oasis-Token"), "英文文案需给出完整粘贴格式")

        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: "Oasis-Token=header.payload.signature"), network: mockNetwork)
            XCTFail("缺 Oasis-Webid 应早失败")
        } catch let error as PlatformError {
            guard case .apiError(_, let message) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertEqual(message, enMessage, "message 应取自 i18n key error.stepfun.cookieFormat")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
        XCTAssertEqual(mockNetwork.requestCount, 0, "格式错误不应发出任何请求")

        I18nService.shared.setLocale("zh-Hans")
        let zhMessage = I18nService.shared.translate("error.stepfun.cookieFormat")
        XCTAssertTrue(zhMessage.contains("Oasis-Webid"))
        XCTAssertTrue(zhMessage.contains("Oasis-Token"))
        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: "Oasis-Token=header.payload.signature"), network: mockNetwork)
            XCTFail("缺 Oasis-Webid 应早失败")
        } catch let error as PlatformError {
            guard case .apiError(_, let message) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertEqual(message, zhMessage, "中文语境同样走 i18n")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
        I18nService.shared.setLocale("en")  // 复位, 不影响其它用例
    }
}
