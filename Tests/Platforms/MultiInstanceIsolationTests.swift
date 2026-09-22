import XCTest
@testable import QuotaBar

/// 按 Authorization header 分流的 mock: 两个 MiniMax 账号 (不同 key) 从同一 mock
/// 拿到各自的响应, 用于验证多账号数据路径.
final class KeyedMockNetworkService: NetworkService {
    var responsesByAuthKey: [String: Data] = [:]
    var requests: [URLRequest] = []

    func data(from request: URLRequest) async throws -> (Data, URLResponse) {
        requests.append(request)
        let auth = request.value(forHTTPHeaderField: "Authorization") ?? ""
        guard let data = responsesByAuthKey[auth] else {
            throw URLError(.badServerResponse)
        }
        return (data, MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200))
    }
}

/// 多账号核心行为: 两个 MiniMax 实例各持独立 service, 同一网络层下各自拉各自的数据,
/// usage 缓存互不污染. 这正是 PlatformManager 按 instance id 建 service 的设计前提.
final class MultiInstanceIsolationTests: XCTestCase {

    private func remainJSON(fiveHour: Double, weekly: Double) -> String {
        """
        {"model_remains": [{"model_name": "general",
            "current_interval_remaining_percent": \(fiveHour),
            "current_weekly_remaining_percent": \(weekly),
            "current_weekly_status": 1}]}
        """
    }

    private func makeConfig(instance: PlatformInstance, apiKey: String) -> PlatformConfigData {
        PlatformConfigData(
            platformType: instance.platformType,
            instanceID: instance.id,
            displayName: instance.displayTitle,
            apiBaseURL: "https://test.com",
            authHeader: "Authorization",
            authPrefix: "Bearer ",
            apiKey: apiKey
        )
    }

    func testTwoMiniMaxInstancesFetchOwnData() async throws {
        let network = KeyedMockNetworkService()
        // 主号剩 90%, 小号剩 30%
        network.responsesByAuthKey["Bearer sk-main"] =
            remainJSON(fiveHour: 90, weekly: 80).data(using: .utf8)!
        network.responsesByAuthKey["Bearer sk-second"] =
            remainJSON(fiveHour: 30, weekly: 20).data(using: .utf8)!

        let main = PlatformInstance(id: "minimax_cn", platformType: .minimax_cn, displayName: "主号")
        let second = PlatformInstance(id: "minimax_cn-2", platformType: .minimax_cn, displayName: "小号")

        // 每个实例一个独立 service (PlatformManager 的实际做法)
        let mainService = MiniMaxPlatformAPIService()
        let secondService = MiniMaxPlatformAPIService()

        let mainData = try await mainService.fetchUsage(config: makeConfig(instance: main, apiKey: "sk-main"), network: network)
        let secondData = try await secondService.fetchUsage(config: makeConfig(instance: second, apiKey: "sk-second"), network: network)

        XCTAssertEqual(mainData.instanceID, "minimax_cn")
        XCTAssertEqual(mainData.displayName, "主号")
        XCTAssertEqual(mainData.metrics[0].currentValue, 90.0)

        XCTAssertEqual(secondData.instanceID, "minimax_cn-2")
        XCTAssertEqual(secondData.displayName, "小号")
        XCTAssertEqual(secondData.metrics[0].currentValue, 30.0)
    }

    func testInstanceUsageCacheDoesNotLeakAcrossServices() async throws {
        let network = KeyedMockNetworkService()
        network.responsesByAuthKey["Bearer sk-main"] =
            remainJSON(fiveHour: 90, weekly: 80).data(using: .utf8)!
        network.responsesByAuthKey["Bearer sk-second"] =
            remainJSON(fiveHour: 30, weekly: 20).data(using: .utf8)!

        let main = PlatformInstance(id: "minimax_cn", platformType: .minimax_cn, displayName: "")
        let second = PlatformInstance(id: "minimax_cn-2", platformType: .minimax_cn, displayName: "")
        let mainService = MiniMaxPlatformAPIService()
        let secondService = MiniMaxPlatformAPIService()

        // 先各自拉一次 (进入各自的 10 秒缓存)
        _ = try await mainService.fetchUsage(config: makeConfig(instance: main, apiKey: "sk-main"), network: network)
        _ = try await secondService.fetchUsage(config: makeConfig(instance: second, apiKey: "sk-second"), network: network)

        // 缓存窗口内再拉: 若缓存互相污染, 小号会拿到主号的 90%.
        let mainAgain = try await mainService.fetchUsage(config: makeConfig(instance: main, apiKey: "sk-main"), network: network)
        let secondAgain = try await secondService.fetchUsage(config: makeConfig(instance: second, apiKey: "sk-second"), network: network)

        XCTAssertEqual(mainAgain.metrics[0].currentValue, 90.0)
        XCTAssertEqual(secondAgain.metrics[0].currentValue, 30.0)
    }

    // MARK: - TokenRhythm 多实例隔离 (DeepSeek 盲区补测, R3)

    /// 按 Cookie 头 (tr_session=...) 分流的 mock: 两个 TokenRhythm 账号 (不同 session)
    /// 从同一网络层拿到各自的余额, 用于验证多账号数据路径不串号.
    private final class CookieKeyedMockNetworkService: NetworkService {
        var responsesBySession: [String: Data] = [:]
        var requests: [URLRequest] = []

        func data(from request: URLRequest) async throws -> (Data, URLResponse) {
            requests.append(request)
            let cookie = request.value(forHTTPHeaderField: "Cookie") ?? ""
            guard let data = responsesBySession[cookie] else {
                throw URLError(.badServerResponse)
            }
            return (data, MockNetworkService.makeResponse(url: "https://test.com", statusCode: 200))
        }
    }

    private func tokenRhythmConfig(instance: PlatformInstance, session: String) -> PlatformConfigData {
        PlatformConfigData(
            platformType: .tokenrhythm,
            instanceID: instance.id,
            displayName: instance.displayTitle,
            apiBaseURL: "https://tokenrhythm.studio/api/wallet/summary",
            authHeader: "Cookie",
            authPrefix: "tr_session=",
            apiKey: session
        )
    }

    func testTwoTokenRhythmInstancesFetchOwnBalance() async throws {
        // 薅羊毛多账号场景: 两个 TokenRhythm 账号不同 session, 余额不得串号.
        let network = CookieKeyedMockNetworkService()
        network.responsesBySession["tr_session=sess_main"] = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"253.76250848"}}
        """.data(using: .utf8)!
        network.responsesBySession["tr_session=sess_small"] = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"8.50000000"}}
        """.data(using: .utf8)!

        let main = PlatformInstance(id: "tokenrhythm", platformType: .tokenrhythm, displayName: "主号")
        let small = PlatformInstance(id: "tokenrhythm-2", platformType: .tokenrhythm, displayName: "小号")

        // 每个实例一个独立 service (PlatformManager 的实际做法): 300s 缓存按账号隔离.
        let mainService = TokenRhythmPlatformAPIService()
        let smallService = TokenRhythmPlatformAPIService()

        let mainData = try await mainService.fetchUsage(config: tokenRhythmConfig(instance: main, session: "sess_main"), network: network)
        let smallData = try await smallService.fetchUsage(config: tokenRhythmConfig(instance: small, session: "sess_small"), network: network)

        XCTAssertEqual(mainData.instanceID, "tokenrhythm")
        XCTAssertEqual(mainData.displayName, "主号")
        XCTAssertEqual(mainData.metrics[0].currentValue, 253.76250848, accuracy: 0.000001)
        XCTAssertTrue(mainData.isHealthy)

        XCTAssertEqual(smallData.instanceID, "tokenrhythm-2")
        XCTAssertEqual(smallData.displayName, "小号")
        XCTAssertEqual(smallData.metrics[0].currentValue, 8.5, accuracy: 0.000001)
        XCTAssertFalse(smallData.isHealthy, "低余额 (<10 元) 按账号各自判定, 不互串")
    }

    func testTokenRhythmUsageCacheDoesNotLeakAcrossServices() async throws {
        // 缓存窗口内重复拉取: 若两实例 service 的缓存互染, 小号会拿到主号余额.
        let network = CookieKeyedMockNetworkService()
        network.responsesBySession["tr_session=sess_main"] = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"253.76250848"}}
        """.data(using: .utf8)!
        network.responsesBySession["tr_session=sess_small"] = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"8.50000000"}}
        """.data(using: .utf8)!

        let main = PlatformInstance(id: "tokenrhythm", platformType: .tokenrhythm, displayName: "")
        let small = PlatformInstance(id: "tokenrhythm-2", platformType: .tokenrhythm, displayName: "")
        let mainService = TokenRhythmPlatformAPIService()
        let smallService = TokenRhythmPlatformAPIService()

        _ = try await mainService.fetchUsage(config: tokenRhythmConfig(instance: main, session: "sess_main"), network: network)
        _ = try await smallService.fetchUsage(config: tokenRhythmConfig(instance: small, session: "sess_small"), network: network)
        let requestCountAfterFirstRound = network.requests.count

        let mainAgain = try await mainService.fetchUsage(config: tokenRhythmConfig(instance: main, session: "sess_main"), network: network)
        let smallAgain = try await smallService.fetchUsage(config: tokenRhythmConfig(instance: small, session: "sess_small"), network: network)

        XCTAssertEqual(mainAgain.metrics[0].currentValue, 253.76250848, accuracy: 0.000001)
        XCTAssertEqual(smallAgain.metrics[0].currentValue, 8.5, accuracy: 0.000001)
        XCTAssertEqual(network.requests.count, requestCountAfterFirstRound,
                       "缓存窗口内再拉不应发新请求 (各自 service 各自缓存)")
    }
}
