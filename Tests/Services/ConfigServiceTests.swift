import XCTest
@testable import QuotaBar

final class ConfigServiceTests: XCTestCase {
    func testDefaultActiveInstance() {
        let service = ConfigService.shared
        // Should have a valid active instance
        let instance = service.activeInstance
        XCTAssertTrue(PlatformInstanceStore.shared.instances.contains(where: { $0.id == instance.id }))
    }

    func testDefaultDisplayMode() {
        let service = ConfigService.shared
        XCTAssertNotNil(service.displayMode)
    }

    func testConfiguredInstancesReturnsArray() {
        let service = ConfigService.shared
        let instances = service.configuredInstances()
        XCTAssertNotNil(instances)
    }

    func testStoreForPlatformReturnsSameInstance() {
        let service = ConfigService.shared
        let instance = PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: "")
        let store1 = service.store(for: instance)
        let store2 = service.store(for: instance)
        XCTAssertTrue(store1 === store2)
    }

    func testStoreForDifferentPlatformsReturnsDifferentInstances() {
        let service = ConfigService.shared
        let store1 = service.store(for: PlatformInstance(id: "minimax_cn", platformType: .minimax_cn, displayName: ""))
        let store2 = service.store(for: PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: ""))
        XCTAssertTrue(store1 !== store2)
    }

    // MARK: - Legacy Cleanup List (P1-5)

    func testCleanupListExcludesCurrentPlatforms() {
        // P1-5 (miniMax Round 2): ConfigService.init 的 legacy 清理早于
        // PlatformInstanceStore 的迁移, 清理列表若含现役平台, 2.0.x 直升级用户的
        // 老配置会被先删后搬不到. stepfun 曾被删过又加回, 是回归重灾区.
        for type in PlatformType.allCases {
            XCTAssertFalse(
                ConfigService.cleanedLegacyPlatforms.contains(type.rawValue),
                "现役平台 \(type.rawValue) 不在清理列表 (老配置由 migrate 接管)"
            )
        }
        XCTAssertFalse(ConfigService.cleanedLegacyPlatforms.contains("stepfun"))
    }

    func testCleanupListStillContainsRemovedPlatforms() {
        // 已删除平台的残留仍要清 (死数据 + 老版本明文 api_key).
        for legacy in ["minimax_en", "glm_en", "kimi", "deepseek", "mimo"] {
            XCTAssertTrue(ConfigService.cleanedLegacyPlatforms.contains(legacy), "\(legacy) 应仍在清理列表")
        }
    }
}
