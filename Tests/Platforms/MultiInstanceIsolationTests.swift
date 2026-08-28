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
}
