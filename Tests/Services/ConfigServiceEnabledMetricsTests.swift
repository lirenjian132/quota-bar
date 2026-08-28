import XCTest
@testable import QuotaBar

final class ConfigServiceEnabledMetricsTests: XCTestCase {
    var service: ConfigService!

    // 默认实例 (id 与平台 rawValue 相同), 与生产迁移结果一致.
    static let minimaxInstance = PlatformInstance(id: "minimax_cn", platformType: .minimax_cn, displayName: "")
    static let glmInstance = PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: "")


    // ConfigService 在测试进程路由到 AppEnvironment.testDefaults (隔离 suite),
    // 不再触碰用户真实配置.
    private let defaults = AppEnvironment.testDefaults

    override func setUp() {
        super.setUp()
        defaults.removeObject(forKey: "quotabar.instance.minimax_cn.enabledMetrics")
        defaults.removeObject(forKey: "quotabar.instance.glm_cn.enabledMetrics")
        service = ConfigService.shared
    }

    override func tearDown() {
        defaults.removeObject(forKey: "quotabar.instance.minimax_cn.enabledMetrics")
        defaults.removeObject(forKey: "quotabar.instance.glm_cn.enabledMetrics")
        super.tearDown()
    }

    func testDefaultEnabledMetricsForMiniMax() {
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour"])
    }

    func testDefaultEnabledMetricsForGLM() {
        XCTAssertEqual(service.enabledMetrics(for: Self.glmInstance), ["five_hour", "weekly_limit"])
    }

    func testEmptyUserDefaultsReturnsDefaults() {
        defaults.removeObject(forKey: "quotabar.instance.minimax_cn.enabledMetrics")
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour"])
    }

    func testSetEnabledMetricsPersists() {
        service.setEnabledMetrics(["mcp_monthly"], for: Self.minimaxInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["mcp_monthly"])
        // 模拟重启: 验证 UserDefaults 里写入了 JSON
        let key = "quotabar.instance.minimax_cn.enabledMetrics"
        let stored = defaults.string(forKey: key)
        XCTAssertNotNil(stored)
        XCTAssertTrue(stored!.contains("mcp_monthly"))
    }

    func testSetEnabledMetricsRejectsEmpty() {
        service.setEnabledMetrics(["five_hour"], for: Self.minimaxInstance)  // baseline
        service.setEnabledMetrics([], for: Self.minimaxInstance)              // rejected
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour"])
    }

    func testSetEnabledMetricsRejectsTooMany() {
        service.setEnabledMetrics(["five_hour"], for: Self.minimaxInstance)
        service.setEnabledMetrics(["five_hour", "weekly_limit", "mcp_monthly"], for: Self.minimaxInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour"])
    }
}