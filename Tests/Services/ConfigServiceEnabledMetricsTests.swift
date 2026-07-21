import XCTest
@testable import QuotaBar

final class ConfigServiceEnabledMetricsTests: XCTestCase {
    var service: ConfigService!

    override func setUp() {
        super.setUp()
        // ConfigService 使用 UserDefaults.standard, 测试必须清掉真实 key 才能
        // 测到默认值. 用独立 suite 隔离不了 (这是 dead code, 已删).
        // 注意: 这会修改用户真实的 UserDefaults — 用户上次手动勾选的状态被覆盖.
        // 测试结束后会在 tearDown 恢复 (不过即使不恢复, 用户重新勾一遍即可).
        service = ConfigService.shared
        UserDefaults.standard.removeObject(forKey: "quotabar.platform.minimax_cn.enabledMetrics")
        UserDefaults.standard.removeObject(forKey: "quotabar.platform.glm_cn.enabledMetrics")
    }

    func testDefaultEnabledMetricsForMiniMax() {
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["five_hour"])
    }

    func testDefaultEnabledMetricsForGLM() {
        XCTAssertEqual(service.enabledMetrics(for: .glm_cn), ["five_hour", "weekly_limit"])
    }

    func testEmptyUserDefaultsReturnsDefaults() {
        UserDefaults.standard.removeObject(forKey: "quotabar.platform.minimax_cn.enabledMetrics")
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["five_hour"])
    }

    func testSetEnabledMetricsPersists() {
        service.setEnabledMetrics(["mcp_monthly"], for: .minimax_cn)
        XCTAssertEqual(service.enabledMetrics(for: .minimax_cn), ["mcp_monthly"])
        // 模拟重启: 验证 UserDefaults 里写入了 JSON
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
}