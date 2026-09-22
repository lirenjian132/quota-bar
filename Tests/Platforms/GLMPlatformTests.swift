import XCTest
@testable import QuotaBar

final class GLMPlatformTests: XCTestCase {
    var mockNetwork: MockNetworkService!
    var service: GLMPlatformAPIService!

    override func setUp() {
        super.setUp()
        mockNetwork = MockNetworkService()
        service = GLMPlatformAPIService()
    }

    private func makeConfig(apiKey: String = "test-key") -> PlatformConfigData {
        PlatformConfigData(
            platformType: .glm_cn,
            apiBaseURL: "https://test.com",
            authHeader: "Authorization",
            authPrefix: "Bearer ",
            apiKey: apiKey
        )
    }

    func testFetchUsageSuccess() async throws {
        // success=true: TOKENS_LIMIT(5h + weekly) + TIME_LIMIT(MCP 月度)
        let json = """
        {
            "code": 200,
            "msg": "success",
            "success": true,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20, "nextResetTime": 1780329600000},
                    {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 10, "nextResetTime": 1780848000000},
                    {"type": "TIME_LIMIT", "usage": 1000, "currentValue": 68, "remaining": 932, "nextResetTime": 1780848000000}
                ],
                "level": "v1"
            }
        }
        """
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)

        XCTAssertEqual(result.platform, .glm_cn)
        XCTAssertEqual(result.metrics.count, 3)
        // TOKENS_LIMIT unit=3 → five_hour, used 20% → remaining 80%
        XCTAssertEqual(result.metrics[0].label, "five_hour")
        XCTAssertEqual(result.metrics[0].currentValue, 80.0)
        XCTAssertEqual(result.metrics[0].totalValue, 100)
        // TOKENS_LIMIT unit=6 → weekly_limit, used 10% → remaining 90%
        XCTAssertEqual(result.metrics[1].label, "weekly_limit")
        XCTAssertEqual(result.metrics[1].currentValue, 90.0)
        // TIME_LIMIT → mcp_monthly, remaining/total 次数
        XCTAssertEqual(result.metrics[2].label, "mcp_monthly")
        XCTAssertEqual(result.metrics[2].currentValue, 932.0)
        XCTAssertEqual(result.metrics[2].totalValue, 1000.0)
    }

    // GLM 业务码错误 (success=false) 必须抛 apiError, 不能静默走空 metrics.
    // 本次修复新增的检查 — 防 key 失效/账户异常时用户看到"无数据"却不知原因.
    func testFetchUsageBusinessError() async {
        let json = #"{"code": 401, "msg": "invalid api key", "success": false, "data": null}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("Should throw apiError when success=false")
        } catch {
            XCTAssertEqual(error as? PlatformError, PlatformError.apiError(.glm_cn, "invalid api key"))
        }
    }

    func testFetchUsageBusinessErrorEmptyMsg() async {
        // success=false 但 msg 空: 应该用兜底文案, 不崩
        let json = #"{"code": 500, "msg": "", "success": false, "data": null}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("Should throw apiError")
        } catch let error as PlatformError {
            XCTAssertEqual(error, PlatformError.apiError(.glm_cn, "GLM request failed"))
        } catch {
            XCTFail("Wrong error type: \(error)")
        }
    }

    func testFetchUsageNotConfigured() async {
        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: ""), network: mockNetwork)
            XCTFail("Should throw notConfigured")
        } catch {
            XCTAssertEqual(error as? PlatformError, PlatformError.notConfigured(.glm_cn))
        }
    }

    func testFetchUsageUnauthorized() async {
        mockNetwork.mockData = Data()
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 401)

        do {
            _ = try await service.fetchUsage(config: makeConfig(apiKey: "bad-key"), network: mockNetwork)
            XCTFail("Should throw unauthorized")
        } catch {
            XCTAssertEqual(error as? PlatformError, PlatformError.unauthorized(.glm_cn))
        }
    }

    // MARK: - DeepSeek 盲区补测 (R3)

    func testServerError5xxThrowsNetworkError() async {
        mockNetwork.mockData = #"{"code":"INTERNAL","message":"server error"}"#.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 500)

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
        // mockError 也不能吞掉请求现场 (lastRequest 记在 error 检查之前).
        XCTAssertEqual(mockNetwork.lastRequest?.url?.absoluteString, "https://test.com")
    }

    func testInvalidJSONThrowsDecodingError() async {
        mockNetwork.mockData = "invalid json".data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("坏 JSON 应抛 decodingError")
        } catch let error as PlatformError {
            guard case .decodingError = error else {
                return XCTFail("应为 decodingError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testNullLimitsThrowsInvalidResponse() async {
        // limits = null: 无任何额度信息, 按无效响应处理 (不静默显示"无数据").
        let json = #"{"code":200,"msg":"ok","success":true,"data":{"limits":null,"level":"v1"}}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("limits=null 应抛 invalidResponse")
        } catch let error as PlatformError {
            guard case .invalidResponse = error else {
                return XCTFail("应为 invalidResponse, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testEmptyLimitsThrowsInvalidResponse() async {
        // limits = []: 同上.
        let json = #"{"code":200,"msg":"ok","success":true,"data":{"limits":[],"level":"v1"}}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("limits 空数组应抛 invalidResponse")
        } catch let error as PlatformError {
            guard case .invalidResponse = error else {
                return XCTFail("应为 invalidResponse, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testCacheHitAndClearCache() async throws {
        let json = """
        {
            "code": 200, "msg": "success", "success": true,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20},
                    {"type": "TIME_LIMIT", "usage": 1000, "currentValue": 68, "remaining": 932}
                ],
                "level": "v1"
            }
        }
        """
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(mockNetwork.requestCount, 1)

        // 缓存命中 (10s 窗口): 换响应为 500, 若真发请求会抛 networkError.
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 500)
        let cached = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(mockNetwork.requestCount, 1, "缓存命中不应再发请求")
        XCTAssertEqual(cached.metrics[0].label, "five_hour")

        // clearCache 后重新发请求, 此时 500 生效 (换 key 路径依赖这个行为).
        service.clearCache()
        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("清缓存后应发起真实请求并抛 networkError")
        } catch let error as PlatformError {
            guard case .networkError = error else {
                return XCTFail("应为 networkError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
        XCTAssertEqual(mockNetwork.requestCount, 2)
    }

    // MARK: - A4-5 GLM limits 静默路径 (GLM P1-5 + P2-4, DeepSeek C1-C4 探针)

    func testAllUnknownLimitTypesThrowsAPIError() async {
        // limits 有元素但全未知 type: R3 只挡 nil/[]. 处理后 metrics 为空时改报
        // apiError (error.glm.emptyLimits) — 静默走空 metrics 会让 UI 显示"无数据",
        // 用户分不清"没额度"和"接口/账号异常" (DeepSeek C1 探针).
        let json = #"{"code":200,"msg":"ok","success":true,"data":{"limits":[{"type":"UNKNOWN_A"},{"type":"UNKNOWN_B"}],"level":"v1"}}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("全未知 type 应抛 apiError")
        } catch let error as PlatformError {
            guard case .apiError(_, let msg) = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
            XCTAssertEqual(msg, I18nService.shared.translate("error.glm.emptyLimits"),
                           "文案应走 i18n key error.glm.emptyLimits")
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testAllLimitsSkippedForMissingFieldsThrowsAPIError() async {
        // 对称面: 已知 type 但关键字段全缺失 → 逐条跳过 → metrics 仍为空 → apiError.
        let json = #"{"code":200,"msg":"ok","success":true,"data":{"limits":[{"type":"TOKENS_LIMIT","unit":3},{"type":"TIME_LIMIT"}],"level":"v1"}}"#
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        do {
            _ = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTFail("处理后为空应抛 apiError")
        } catch let error as PlatformError {
            guard case .apiError = error else {
                return XCTFail("应为 apiError, 实际: \(error)")
            }
        } catch {
            XCTFail("应为 PlatformError, 实际: \(error)")
        }
    }

    func testTokensLimitMissingPercentageIsSkipped() async throws {
        // percentage 缺失的 TOKENS_LIMIT 跳过该条 — 不得默认 0 谎报 100% 剩余
        // (DeepSeek C2 探针). 混一条有效数据证明"跳过单条"而非"整体报错".
        let json = """
        {
            "code": 200, "msg": "success", "success": true,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20},
                    {"type": "TOKENS_LIMIT", "unit": 6}
                ],
                "level": "v1"
            }
        }
        """
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(result.metrics.count, 1, "缺 percentage 的条目必须跳过")
        XCTAssertEqual(result.metrics[0].label, "five_hour")
        XCTAssertEqual(result.metrics[0].currentValue, 80.0, "剩余 = 100 - 已用, 不得因缺字段谎报 100%")
    }

    func testTimeLimitMissingUsageOrRemainingIsSkipped() async throws {
        // TIME_LIMIT 缺 usage/remaining 时跳过 — 默认 0 会谎报"0 次可用" (C3 探针).
        let json = """
        {
            "code": 200, "msg": "success", "success": true,
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20},
                    {"type": "TIME_LIMIT", "usage": 1000},
                    {"type": "TIME_LIMIT", "remaining": 932}
                ],
                "level": "v1"
            }
        }
        """
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)

        let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
        XCTAssertEqual(result.metrics.count, 1, "缺 usage/remaining 的 TIME_LIMIT 必须跳过")
        XCTAssertEqual(result.metrics[0].label, "five_hour")
    }

    func testIsHealthyAllSatisfyAnyMetricBelow15MarksUnhealthy() async throws {
        // isHealthy = allSatisfy 复合判定: 任一 metric 剩余 < 15% → 不健康.
        func glmJSON(_ fiveHourUsedPct: Int, _ weeklyUsedPct: Int, _ mcpRemaining: Int) -> String {
            """
            {
                "code": 200, "msg": "success", "success": true,
                "data": {
                    "limits": [
                        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": \(fiveHourUsedPct)},
                        {"type": "TOKENS_LIMIT", "unit": 6, "percentage": \(weeklyUsedPct)},
                        {"type": "TIME_LIMIT", "usage": 1000, "currentValue": \(1000 - mcpRemaining), "remaining": \(mcpRemaining)}
                    ],
                    "level": "v1"
                }
            }
            """
        }
        let cases: [(json: String, expectedHealthy: Bool, name: String)] = [
            (glmJSON(20, 10, 932), true, "全部充足 (80%/90%/932次)"),
            (glmJSON(88, 10, 932), false, "5 小时剩 12% → 不健康"),
            (glmJSON(20, 86, 932), false, "周限额剩 14% → 不健康"),
            (glmJSON(20, 10, 100), false, "MCP 月度剩 10% → 不健康"),
            (glmJSON(20, 86, 100), false, "两项低于 15% → 不健康")
        ]
        for tc in cases {
            service.clearCache()
            mockNetwork.mockData = tc.json.data(using: .utf8)
            mockNetwork.mockResponse = MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200)
            let result = try await service.fetchUsage(config: makeConfig(), network: mockNetwork)
            XCTAssertEqual(result.isHealthy, tc.expectedHealthy, "\(tc.name)")
        }
    }
}
