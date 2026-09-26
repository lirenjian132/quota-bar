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

    // MARK: - P0-2: setAPIKey 线程安全 (writeLock)

    /// 并发 setAPIKey (多线程同时写不同 key): 四步 (keychain.set → 内存 apiKey
    /// → save defaults → clearPlaintext) 被 writeLock 串成原子操作, 任意一轮结束
    /// 内存值与 keychain 必须一致, defaults 不得残留明文. 无锁时四步可被交错成
    /// "keychain 是新值 / 内存是旧值" 的分裂态. N 轮重复跑.
    func testConcurrentSetAPIKeyKeepsKeychainAndMemoryConsistent() throws {
        let store = makeStore(.glm_cn)
        let keys = (0..<32).map { "sk-concurrent-\($0)" }
        // keychain account = 实例 id (与 setAPIKey 内部一致; UserDefaults key 是另一个).
        let account = store.instance.id

        for round in 0..<8 {
            DispatchQueue.concurrentPerform(iterations: keys.count) { index in
                store.setAPIKey(keys[index])
            }
            let memory = try XCTUnwrap(store.apiKey, "第 \(round) 轮: 内存应有 key")
            let stored = try XCTUnwrap(keychain.get(account: account), "第 \(round) 轮: keychain 应有 key")
            XCTAssertEqual(memory, stored, "第 \(round) 轮: 内存与 keychain 必须一致 (锁内原子写)")
            XCTAssertFalse(memory.isEmpty)
            let dict = testDefaults.dictionary(forKey: glmKey)
            XCTAssertEqual(dict?["api_key"] as? String, "", "第 \(round) 轮: defaults 不得残留明文")
        }
    }

    /// set / reset 交错并发 (空串走 resetAPIKey): 终态必须自洽 — 内存有 key
    /// ⇔ keychain 有 key. 无锁时可能剩"内存有值但 keychain 已删"的分裂态,
    /// 用户重启后凭据凭空消失.
    func testConcurrentSetAndResetStayConsistent() throws {
        let store = makeStore(.glm_cn)
        let account = store.instance.id

        for round in 0..<8 {
            DispatchQueue.concurrentPerform(iterations: 16) { index in
                // 偶数写 key, 奇数传空白串 → resetAPIKey 路径.
                store.setAPIKey(index % 2 == 0 ? "sk-set-\(index)" : "   ")
            }
            let memory = store.apiKey
            let stored = try keychain.get(account: account)
            XCTAssertEqual(memory, stored, "第 \(round) 轮: 内存与 keychain 必须同为 nil 或同值")
            XCTAssertEqual(store.isConfigured, stored != nil, "第 \(round) 轮: isConfigured 与 keychain 一致")
            let dict = testDefaults.dictionary(forKey: glmKey)
            XCTAssertEqual(dict?["api_key"] as? String, "", "第 \(round) 轮: defaults 不得残留明文")
        }
    }
}
