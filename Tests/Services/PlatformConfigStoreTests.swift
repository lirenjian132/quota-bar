import XCTest
@testable import QuotaBar

final class PlatformConfigStoreTests: XCTestCase {

    private var keychain: InMemoryKeychainStore!
    private let glmKey = "quotabar.instance.glm_cn"
    private let minimaxKey = "quotabar.instance.minimax_cn"

    // 隔离的 UserDefaults suite, 避免测试 fixture 污染真实用户配置 (.standard).
    // 之前用 .standard 导致 setAPIKey("sk-minimax") 覆盖了用户的真实 MiniMax token.
    private let testDefaults = UserDefaults(suiteName: "platform-config-store-tests")!

    override func setUp() {
        super.setUp()
        keychain = InMemoryKeychainStore()
        [glmKey, minimaxKey].forEach { testDefaults.removeObject(forKey: $0) }
    }

    override func tearDown() {
        [glmKey, minimaxKey].forEach { testDefaults.removeObject(forKey: $0) }
        keychain = nil
        super.tearDown()
    }

    private func makeStore(_ type: PlatformType) -> PlatformConfigStore {
        makeStore(id: type.rawValue, type: type)
    }

    private func makeStore(id: String, type: PlatformType) -> PlatformConfigStore {
        PlatformConfigStore(instance: PlatformInstance(id: id, platformType: type, displayName: ""),
                            keychain: keychain, userDefaults: testDefaults)
    }

    func testNewStoreIsNotConfigured() {
        let store = makeStore(.glm_cn)
        XCTAssertFalse(store.isConfigured)
        XCTAssertNil(store.apiKey)
    }

    func testSetAPIKeyWritesKeychainAndClearsDefaults() throws {
        let store = makeStore(.glm_cn)
        store.setAPIKey("sk-test123")
        XCTAssertTrue(store.isConfigured)
        XCTAssertEqual(store.apiKey, "sk-test123")
        XCTAssertEqual(try keychain.get(account: "glm_cn"), "sk-test123")
        let dict = testDefaults.dictionary(forKey: glmKey)
        XCTAssertEqual(dict?["api_key"] as? String, "")
    }

    func testResetAPIKey() throws {
        let store = makeStore(.glm_cn)
        store.setAPIKey("sk-test123")
        store.resetAPIKey()
        XCTAssertFalse(store.isConfigured)
        XCTAssertNil(store.apiKey)
        XCTAssertNil(try keychain.get(account: "glm_cn"))
        let dict = testDefaults.dictionary(forKey: glmKey)
        XCTAssertEqual(dict?["api_key"] as? String, "")
    }

    func testPersistence() {
        let store1 = makeStore(.glm_cn)
        store1.setAPIKey("sk-persist-test")
        let store2 = makeStore(.glm_cn)
        XCTAssertEqual(store2.apiKey, "sk-persist-test")
        XCTAssertTrue(store2.isConfigured)
    }

    func testMigrateFromUserDefaultsThenClearPlaintext() throws {
        testDefaults.set([
            "api_base_url": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
            "auth_header": "Authorization",
            "auth_prefix": "",
            "region": "domestic",
            "api_key": "sk-legacy"
        ], forKey: glmKey)

        let store = makeStore(.glm_cn)
        XCTAssertEqual(store.apiKey, "sk-legacy")
        XCTAssertEqual(try keychain.get(account: "glm_cn"), "sk-legacy")
        let dict = testDefaults.dictionary(forKey: glmKey)
        XCTAssertEqual(dict?["api_key"] as? String, "")
    }

    func testMigrationKeepsPlaintextIfKeychainSetFails() throws {
        struct Boom: Error {}
        keychain.setError = Boom()
        testDefaults.set([
            "api_base_url": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
            "auth_header": "Authorization",
            "auth_prefix": "",
            "region": "domestic",
            "api_key": "sk-legacy"
        ], forKey: glmKey)

        let store = makeStore(.glm_cn)
        // Keychain 写失败时保留 plist 明文, 本会话仍可用 (降级策略).
        XCTAssertEqual(store.apiKey, "sk-legacy")
        let dict = testDefaults.dictionary(forKey: glmKey)
        XCTAssertEqual(dict?["api_key"] as? String, "sk-legacy")
    }

    func testToConfigData() {
        let store = makeStore(.glm_cn)
        store.setAPIKey("sk-test")

        let configData = store.toConfigData()
        XCTAssertEqual(configData.platformType, .glm_cn)
        XCTAssertEqual(configData.apiKey, "sk-test")
        XCTAssertEqual(configData.authHeader, "Authorization")
        // GLM 鉴权不带 Bearer 前缀 (Authorization: {api_key}), 模板 auth_prefix 为空串是故意的
        XCTAssertEqual(configData.authPrefix, "")
    }

    func testDefaultValues() {
        let store = makeStore(.minimax_cn)
        XCTAssertEqual(store.authHeader, "Authorization")
        XCTAssertEqual(store.authPrefix, "Bearer ")
    }

    func testWhitespaceOnlyKeyIsNotConfigured() {
        let store = makeStore(.glm_cn)
        store.setAPIKey("   ")
        XCTAssertFalse(store.isConfigured)
    }

    func testDifferentPlatformsAreIndependent() {
        let glm = makeStore(.glm_cn)
        glm.setAPIKey("sk-glm")
        let minimax = makeStore(.minimax_cn)
        minimax.setAPIKey("sk-minimax")

        XCTAssertEqual(glm.apiKey, "sk-glm")
        XCTAssertEqual(minimax.apiKey, "sk-minimax")
    }
}
