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
        // 注意 (A4-3): setter 过滤不在 availableMetricLabels 清单内的 label,
        // 这里必须用 minimax 可勾选的 label (mcp_monthly 是 GLM 的, 对 minimax
        // 是死 label, 写入会被拒).
        service.setEnabledMetrics(["weekly_limit"], for: Self.minimaxInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["weekly_limit"])
        // 模拟重启: 验证 UserDefaults 里写入了 JSON
        let key = "quotabar.instance.minimax_cn.enabledMetrics"
        let stored = defaults.string(forKey: key)
        XCTAssertNotNil(stored)
        XCTAssertTrue(stored!.contains("weekly_limit"))
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

    func testRejectedSetLeavesPersistedStateForMenuRefresh() {
        // R3-6: setEnabledMetrics 长度 > 2 时静默拒绝不落盘. 菜单 toggle 后必须以
        // enabledMetrics 的实际返回值刷新 item.state (以落盘状态为准), 否则用户
        // 看到"临时勾选", 重开菜单却消失. 这里钉住拒绝后读回的是旧值这个契约.
        service.setEnabledMetrics(["five_hour", "weekly_limit"], for: Self.minimaxInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour", "weekly_limit"])

        // 模拟菜单第 3 次勾选 (count 3 > 上限 2): 被拒, 落盘状态保持 2 项.
        service.setEnabledMetrics(["five_hour", "weekly_limit", "mcp_monthly"], for: Self.minimaxInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour", "weekly_limit"],
                       "被拒的写入不得改变落盘状态, 菜单据此刷新即不会出现幻觉勾选")
    }

    // MARK: - A4-3 死 label 过滤 (miniMax P1-3 + DeepSeek B3/B4)

    func testGetterFiltersRetiredLabelsFromPersistedValue() {
        // 老用户落盘的 enabledMetrics 含已下架 label (minimax 的 mcp_monthly):
        // 不过滤会占满 2 个勾选名额, 菜单其余项全灰, 且与产出永无交集被
        // prefix(2) 防呆掩盖, 用户无法清理.
        let key = "quotabar.instance.minimax_cn.enabledMetrics"
        defaults.set("[\"mcp_monthly\",\"five_hour\"]", forKey: key)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour"],
                       "死 label 必须从读回值里剔除, 不得占用名额")
    }

    func testGetterFallsBackToDefaultsWhenAllLabelsRetired() {
        // 落盘值全部是死 label (mcp_monthly 对 minimax): 过滤后为空 → 回退平台默认值.
        let key = "quotabar.instance.minimax_cn.enabledMetrics"
        defaults.set("[\"mcp_monthly\"]", forKey: key)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ConfigService.defaultEnabledMetrics(for: .minimax_cn))
    }

    func testSetterRejectsUnknownLabels() {
        // setter 同样过滤: 纯死 label 的写入被拒 (不落盘), 保留旧值 → 最终回退默认值.
        service.setEnabledMetrics(["five_hour"], for: Self.minimaxInstance)  // baseline
        service.setEnabledMetrics(["mcp_monthly"], for: Self.minimaxInstance)  // 死 label, 拒
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["five_hour"])

        // 混合写入: 丢掉死 label, 只落有效部分 (不超过上限).
        service.setEnabledMetrics(["weekly_limit", "mcp_monthly"], for: Self.minimaxInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.minimaxInstance), ["weekly_limit"])

        // 清单因平台而异: mcp_monthly 对 GLM 是合法 label, 必须可写.
        service.setEnabledMetrics(["mcp_monthly"], for: Self.glmInstance)
        XCTAssertEqual(service.enabledMetrics(for: Self.glmInstance), ["mcp_monthly"])
    }

    func testDefaultAndAvailableLabelsStayAligned() {
        // 默认勾选必须是可勾选清单的子集 — 否则新用户首次打开就踩死 label.
        for type in PlatformType.allCases {
            let available = Set(ConfigService.availableMetricLabels(for: type))
            for label in ConfigService.defaultEnabledMetrics(for: type) {
                XCTAssertTrue(available.contains(label),
                              "\(type.rawValue) 默认勾选的 \(label) 必须在可勾选清单内")
            }
        }
    }

    // MARK: - A4-6 拒写也发通知 (miniMax P2-7)

    /// 注册 .enabledMetricsChanged 观察者 (queue: nil 同步投递, 与生产
    /// StatusBarController 的 selector 监听同语义), 返回 (expectation, 注销闭包).
    private func observeEnabledMetricsChanged(object: Any) -> (XCTestExpectation, () -> Void) {
        let received = expectation(description: "enabledMetricsChanged posted")
        let observer = NotificationCenter.default.addObserver(
            forName: .enabledMetricsChanged, object: object, queue: nil
        ) { _ in received.fulfill() }
        return (received, { NotificationCenter.default.removeObserver(observer) })
    }

    func testRejectedSetPostsEnabledMetricsChanged() {
        // 拒写 (空数组) 也要发通知: 监听方 (菜单刷新勾选态) 要能感知"设置被拒",
        // 否则用户看到临时勾选, 重开菜单才消失.
        let (received, remove) = observeEnabledMetricsChanged(object: Self.minimaxInstance.id)
        defer { remove() }
        service.setEnabledMetrics([], for: Self.minimaxInstance)
        // 通知是同步投递 (observer queue: nil), 但全量负载 (并行测试类 × 满载 CPU)
        // 下等待线程被唤醒的延迟可能远超 1s — 13 轮全量中 flake 过 1 次.
        // 放宽到 5s 消除负载噪声, 不改变断言语义.
        wait(for: [received], timeout: 5)
    }

    func testRejectedSetTooManyPostsEnabledMetricsChanged() {
        // 超上限拒写同样发通知 (object 仍是 instance id, 与成功写入一致).
        let (received, remove) = observeEnabledMetricsChanged(object: Self.glmInstance.id)
        defer { remove() }
        service.setEnabledMetrics(["five_hour", "weekly_limit", "mcp_monthly"], for: Self.glmInstance)
        // 同上: 同步投递 + 全量负载唤醒延迟, 超时放宽到 5s (F5-4 flake 修复).
        wait(for: [received], timeout: 5)
    }
}