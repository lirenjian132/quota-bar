import XCTest
@testable import QuotaBar

final class TokenRhythmPlatformTests: XCTestCase {
    var mockNetwork: MockNetworkService!
    var service: TokenRhythmPlatformAPIService!

    override func setUp() {
        super.setUp()
        mockNetwork = MockNetworkService()
        service = TokenRhythmPlatformAPIService()
    }

    // template 的同款配置: Cookie 头 + tr_session= 前缀, apiKey 字段存 session 值.
    private func makeConfig(apiKey: String = "sess_test_123", apiBaseURL: String = "https://tokenrhythm.studio/api/wallet/summary") -> PlatformConfigData {
        PlatformConfigData(
            platformType: .tokenrhythm,
            apiBaseURL: apiBaseURL,
            authHeader: "Cookie",
            authPrefix: "tr_session=",
            apiKey: apiKey
        )
    }

    private var successJSON: String {
        """
        {
            "code": 0,
            "message": "ok",
            "data": {
                "currency": "CNY",
                "availableBalanceCny": "253.76250848",
                "giftAvailableCny": "253.76250848",
                "rechargeBalanceCny": "0.00000000",
                "asOf": "2026-09-19T03:42:16.271Z"
            },
            "traceId": "trace_test"
        }
        """
    }

    private func primeCache() async throws {
        mockNetwork.mockData = successJSON.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)
        _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
    }

    func testFetchUsageSuccessParsesBalance() async throws {
        mockNetwork.mockData = successJSON.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.platform, .tokenrhythm)
        XCTAssertEqual(result.metrics.count, 1)
        XCTAssertEqual(result.metrics[0].label, "balance")
        XCTAssertEqual(result.metrics[0].currentValue, 253.76250848, accuracy: 0.000001)
        XCTAssertNil(result.metrics[0].totalValue, "余额型指标无 total, 让显示层走整数格式化")
        XCTAssertEqual(result.metrics[0].unit, "CNY")
        XCTAssertTrue(result.isHealthy, "余额充足应显示健康")
        // 请求头: Cookie: tr_session=<session 值>
        XCTAssertEqual(mockNetwork.lastRequest?.value(forHTTPHeaderField: "Cookie"), "tr_session=sess_test_123")
    }

    func testLowBalanceMarksUnhealthy() async throws {
        let json = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"8.50000000"}}
        """
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertFalse(result.isHealthy, "余额低于 10 元应标红提示换账号")
        XCTAssertEqual(result.metrics[0].currentValue, 8.5, accuracy: 0.000001)
    }

    func testUnauthorizedOn401() async {
        mockNetwork.mockData = #"{"code":"UNAUTHORIZED","message":"未认证或登录已过期"}"#.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 401)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("401 应抛 unauthorized")
        } catch let error as PlatformError {
            guard case .unauthorized = error else {
                return XCTFail("应为 unauthorized, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testBusinessCodeErrorThrowsAPIError() async {
        let json = #"{"code":1003,"message":"账户已被限制","data":null}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("业务码非 0 应抛 apiError")
        } catch let error as PlatformError {
            guard case .apiError(_, let message) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertTrue(message.contains("账户已被限制"), "应透出服务端 message")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testMissingBalanceFieldThrowsInvalidResponse() async {
        let json = #"{"code":0,"message":"ok","data":{"currency":"CNY"}}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("缺余额字段应抛 invalidResponse")
        } catch let error as PlatformError {
            guard case .invalidResponse = error else {
                return XCTFail("应为 invalidResponse, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testEmptyKeyThrowsNotConfigured() async {
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

    func testBalanceTypeDriftThrowsDecodingError() async {
        // DeepSeek 盲区 (R3): availableBalanceCny 契约是 8 位小数字符串, 上游若漂移成
        // JSON 数字, 纯 String? 字段整体解码失败 → decodingError (不静默按 0 显示).
        let json = #"{"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":253.76}}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("数字型余额应抛 decodingError")
        } catch let error as PlatformError {
            guard case .decodingError = error else {
                return XCTFail("应为 decodingError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testServerError5xxThrowsNetworkError() async {
        mockNetwork.mockData = #"{"code":"INTERNAL","message":"server error"}"#.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 500)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("5xx 应抛 networkError")
        } catch let error as PlatformError {
            guard case .networkError = error else {
                return XCTFail("应为 networkError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testTransportErrorThrowsNetworkError() async {
        mockNetwork.mockError = URLError(.timedOut)

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
        // mockError 也不能吞掉请求现场: lastRequest 记在 error 检查之前 (曾回归).
        XCTAssertEqual(mockNetwork.lastRequest?.url?.absoluteString, "https://tokenrhythm.studio/api/wallet/summary")
    }

    // 两次请求按序返回: [0]=wallet/summary, [1]=wallet/expiring-credits.
    private func expiringJSON(daysFromNow: Double) -> String {
        let iso = ISO8601DateFormatter().string(from: Date().addingTimeInterval(daysFromNow * 86400))
        return """
        {"code":0,"message":"ok","data":{"summary":{"expiringBalanceCny":"253.76250848","nextExpiryAt":"\(iso)"},"list":[]}}
        """
    }

    private var summaryResponse: (Data, HTTPURLResponse) {
        (successJSON.data(using: .utf8)!,
         MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200))
    }

    private func expiringResponse(json: String) -> (Data, HTTPURLResponse) {
        (json.data(using: .utf8)!,
         MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/expiring-credits", statusCode: 200))
    }

    func testExpiryPopulatedAnd3DayMarksUnhealthy() async throws {
        mockNetwork.responseSequence = [summaryResponse, expiringResponse(json: expiringJSON(daysFromNow: 2))]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        let resetTime = try XCTUnwrap(result.metrics[0].resetTime, "到期时间应写入 metric.resetTime")
        XCTAssertEqual(resetTime.timeIntervalSinceNow, 2 * 86400, accuracy: 60)
        XCTAssertFalse(result.isHealthy, "最早到期 3 天内应标不健康 (状态栏红)")
        // 第二个请求打的是 expiring-credits 路径.
        XCTAssertEqual(mockNetwork.lastRequest?.url?.absoluteString, "https://tokenrhythm.studio/api/wallet/expiring-credits")
    }

    func testExpiry5DaysOutStaysHealthy() async throws {
        mockNetwork.responseSequence = [summaryResponse, expiringResponse(json: expiringJSON(daysFromNow: 5))]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertNotNil(result.metrics[0].resetTime)
        XCTAssertTrue(result.isHealthy, "到期 5 天: service 层仍健康 (黄色警示由 view 层按 7 天窗口渲染)")
    }

    func testExpiryRequestFailureDegradesGracefully() async throws {
        // expiring-credits 返回 500: 到期信息缺失, 但余额正常返回.
        mockNetwork.responseSequence = [
            summaryResponse,
            ( #"{"code":"ERR"}"#.data(using: .utf8)!,
              MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/expiring-credits", statusCode: 500) )
        ]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertNil(result.metrics[0].resetTime, "expiring 请求失败应降级为无到期信息")
        XCTAssertEqual(result.metrics[0].currentValue, 253.76250848, accuracy: 0.000001)
        XCTAssertTrue(result.isHealthy)
    }

    func testExpiryWithFractionalSecondsParses() async throws {
        // 实测上游原文: ISO8601 带毫秒 ("2026-09-26T15:20:56.281Z").
        // ISO8601DateFormatter 默认选项解毫秒串返回 nil — 曾导致到期警示全链失效
        // (resetTime 恒 nil, 7 天黄/3 天红永不触发). fixture 硬编码实测原文钉住回归.
        let json = """
        {"code":0,"message":"ok","data":{"summary":{"nextExpiryAt":"2026-09-26T15:20:56.281Z"},"list":[]}}
        """
        mockNetwork.responseSequence = [summaryResponse, expiringResponse(json: json)]

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        let resetTime = try XCTUnwrap(result.metrics[0].resetTime, "带毫秒的 ISO 串应解析出到期时间")
        // 1790436056.281 = 2026-09-26T15:20:56.281Z; 毫秒位 281ms 也要解出来.
        XCTAssertEqual(resetTime.timeIntervalSince1970, 1790436056.281, accuracy: 0.001)
    }

    func testExpiringURLNoopWhenBaseNotSummary() async throws {
        // apiBaseURL 不含 /wallet/summary: replacingOccurrences 是 no-op, URL 与主请求
        // 相同. 不应再打一趟同样的 summary, 直接降级为无到期信息 (只发 1 个请求).
        mockNetwork.responseSequence = [summaryResponse]

        let result = try await service.fetchUsage(
            config: makeConfig(apiBaseURL: "https://tokenrhythm.studio/api/wallet/other"),
            network: mockNetwork
        )

        XCTAssertNil(result.metrics[0].resetTime, "URL 替换无效时应降级为无到期信息")
        XCTAssertEqual(mockNetwork.sequenceCapture.count, 1, "no-op 替换不应产生第二个请求")
        XCTAssertTrue(result.isHealthy)
    }

    func testParseISO8601ReturnsNilForMalformedStrings() throws {
        // 钉住 nil 边界: 无时区 / 仅日期 / 空串 / 垃圾串都返回 nil,
        // 调用方 (nextExpiryDate) 依赖 nil 走"无到期信息"降级, 不崩不误判.
        XCTAssertNil(TokenRhythmPlatformAPIService.parseISO8601("2026-09-26T15:20:56"), "无时区后缀应解析失败")
        XCTAssertNil(TokenRhythmPlatformAPIService.parseISO8601("2026-09-26"), "仅日期应解析失败")
        XCTAssertNil(TokenRhythmPlatformAPIService.parseISO8601(""), "空串应解析失败")
        XCTAssertNil(TokenRhythmPlatformAPIService.parseISO8601("not-a-date"), "垃圾串应解析失败")
        // 实测行为钉住: .withInternetDateTime 也接受 ±hh:mm 偏移 (自动换算 UTC),
        // 上游若发偏移形态不会误判为无到期时间. 15:20:56+08:00 = 07:20:56Z.
        let offset = try XCTUnwrap(TokenRhythmPlatformAPIService.parseISO8601("2026-09-26T15:20:56+08:00"))
        XCTAssertEqual(offset.timeIntervalSince1970, 1790407256, accuracy: 1, "偏移时区应换算成 UTC")
        // 对照: 两种上游合法形态都要能解 (毫秒 / 无毫秒, 均带 Z)
        XCTAssertNotNil(TokenRhythmPlatformAPIService.parseISO8601("2026-09-26T15:20:56.281Z"))
        XCTAssertNotNil(TokenRhythmPlatformAPIService.parseISO8601("2026-09-26T15:20:56Z"))
    }

    func testCacheSuppressesRepeatRequests() async throws {
        try await primeCache()

        // 换 mock 响应为 401: 若 service 真发请求会抛 unauthorized, 命中缓存则返回旧数据.
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 401)
        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(result.metrics[0].currentValue, 253.76250848, accuracy: 0.000001)

        // clearCache 后应真正发请求, 此时 401 生效.
        service.clearCache()
        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("清缓存后应发起真实请求并抛 unauthorized")
        } catch let error as PlatformError {
            guard case .unauthorized = error else {
                return XCTFail("应为 unauthorized, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }
}
