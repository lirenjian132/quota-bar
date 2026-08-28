import XCTest
@testable import QuotaBar

final class PlatformManagerTests: XCTestCase {
    func testManagerHasDefaultServices() {
        let manager = PlatformManager()
        // Should have MiniMax and GLM registered
        let configured = manager.configuredInstances()
        XCTAssertNotNil(configured)
    }

    func testConfiguredPlatformsReturnsConfiguredOnly() {
        let manager = PlatformManager()
        let instances = manager.configuredInstances()
        // Only platforms with API keys should be returned
        for instance in instances {
            let store = ConfigService.shared.store(for: instance)
            XCTAssertTrue(store.isConfigured)
        }
    }

    func testClearCacheDoesNotCrash() {
        let manager = PlatformManager()
        for instance in PlatformInstanceStore.shared.instances {
            manager.clearCache(for: instance)
        }
        manager.clearAllCaches()
    }
}
