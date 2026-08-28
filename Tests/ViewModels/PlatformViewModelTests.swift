import XCTest
@testable import QuotaBar

@MainActor
final class PlatformViewModelTests: XCTestCase {
    func testDefaultActiveInstance() {
        let viewModel = PlatformViewModel()
        let instance = viewModel.activeInstance
        XCTAssertTrue(PlatformInstanceStore.shared.instances.contains(where: { $0.id == instance.id }))
    }

    func testAllInstancesReturnsEnabledInstances() {
        let viewModel = PlatformViewModel()
        let enabledCount = PlatformInstanceStore.shared.instances.filter { $0.isEnabled }.count
        XCTAssertEqual(viewModel.allInstances.count, enabledCount)
    }

    func testConfiguredInstancesReturnsArray() {
        let viewModel = PlatformViewModel()
        let instances = viewModel.allConfiguredInstances
        XCTAssertNotNil(instances)
    }

    func testIsConfiguredReturnsBool() {
        let viewModel = PlatformViewModel()
        for instance in PlatformInstanceStore.shared.instances {
            let _ = viewModel.isConfigured(instance)
        }
    }

    func testInstanceDisplayName() {
        let viewModel = PlatformViewModel()
        let minimax = PlatformInstance(id: "minimax_cn", platformType: .minimax_cn, displayName: "")
        let glm = PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: "")
        XCTAssertEqual(viewModel.instanceDisplayName(minimax), "MiniMax")
        XCTAssertEqual(viewModel.instanceDisplayName(glm), "GLM")
        // 自定义名优先
        let named = PlatformInstance(id: "x", platformType: .minimax_cn, displayName: "小号")
        XCTAssertEqual(viewModel.instanceDisplayName(named), "小号")
    }

    func testConfigureAPIKey() {
        let viewModel = PlatformViewModel()
        let glm = PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: "")
        viewModel.configureAPIKey(for: glm)
        XCTAssertTrue(viewModel.showingConfig)
        XCTAssertEqual(viewModel.configInstance?.id, "glm_cn")
    }

    func testCancelConfig() {
        let viewModel = PlatformViewModel()
        viewModel.configureAPIKey(for: PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: ""))
        viewModel.cancelConfig()
        XCTAssertFalse(viewModel.showingConfig)
        XCTAssertNil(viewModel.configInstance)
    }

    func testCleanupDoesNotCrash() {
        let viewModel = PlatformViewModel()
        viewModel.startAutoRefresh()
        viewModel.cleanup()
    }

    func testSwitchActiveInstance() {
        let viewModel = PlatformViewModel()
        viewModel.switchActiveInstance(PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: ""))
        XCTAssertEqual(viewModel.activeInstance.id, "glm_cn")
    }
}
