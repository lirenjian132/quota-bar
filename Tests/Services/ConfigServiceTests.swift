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
        XCTAssertFalse(store1 === store2)
    }
}
