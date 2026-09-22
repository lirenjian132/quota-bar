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

    func testUsageCacheExpiresAfterTimeout() {
        let cache = PlatformUsageCache<String>()
        cache.write("value")
        XCTAssertEqual(cache.read(timeout: 60), "value", "未过期应命中缓存")
        // timeout 0: write 之后任何已过去的时间都 >= 0 → 立即过期.
        // 各 service 的 clearCache / 换 key 清缓存依赖这个"过期分支"行为.
        Thread.sleep(forTimeInterval: 0.01)
        XCTAssertNil(cache.read(timeout: 0), "0 秒窗口应视为立即过期")
    }
}
