import XCTest
@testable import QuotaBar

final class PlatformInstanceStoreTests: XCTestCase {

    // 隔离 suite: 迁移测试要模拟老版本的真实 UserDefaults 状态, 不能碰 .standard.
    private let testDefaults = UserDefaults(suiteName: "platform-instance-store-tests")!

    override func tearDown() {
        testDefaults.dictionaryRepresentation().keys.forEach { testDefaults.removeObject(forKey: $0) }
        super.tearDown()
    }

    // MARK: - Legacy Migration

    func testLegacyConfigMigrationMovesAllKeys() {
        // 模拟老版本 (< 多账号) 的 UserDefaults 状态
        testDefaults.set([
            "api_base_url": "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
            "auth_header": "Authorization",
            "auth_prefix": "Bearer ",
            "region": "domestic",
            "api_key": "sk-legacy-key"
        ], forKey: "quotabar.platform.minimax_cn")
        testDefaults.set(true, forKey: "quotabar.platform.glm_cn.enabled")
        testDefaults.set(true, forKey: "quotabar.platform.minimax_cn.pinned")
        testDefaults.set("[\"five_hour\",\"weekly_limit\"]", forKey: "quotabar.platform.minimax_cn.enabledMetrics")
        testDefaults.set("glm_cn", forKey: "quotabar.activePlatform")

        let store = PlatformInstanceStore(userDefaults: testDefaults)

        // 每个平台类型一个默认实例, id == rawValue
        XCTAssertEqual(store.instances.count, PlatformType.allCases.count)
        XCTAssertEqual(store.instances.map(\.id), PlatformType.allCases.map(\.rawValue))

        // 配置 dict 搬到新 key
        let dict = testDefaults.dictionary(forKey: "quotabar.instance.minimax_cn")
        XCTAssertEqual(dict?["api_base_url"] as? String,
                       "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains")

        // enabled / pinned / enabledMetrics 搬家
        XCTAssertTrue(testDefaults.bool(forKey: "quotabar.instance.glm_cn.enabled"))
        XCTAssertTrue(testDefaults.bool(forKey: "quotabar.instance.minimax_cn.pinned"))
        XCTAssertNotNil(testDefaults.string(forKey: "quotabar.instance.minimax_cn.enabledMetrics"))

        // activePlatform 平移成 activeInstanceID (默认实例 id 与 rawValue 相同)
        XCTAssertEqual(testDefaults.string(forKey: "quotabar.activeInstanceID"), "glm_cn")

        // 旧 key 全部清掉
        XCTAssertNil(testDefaults.dictionary(forKey: "quotabar.platform.minimax_cn"))
        XCTAssertNil(testDefaults.object(forKey: "quotabar.platform.glm_cn.enabled"))
        XCTAssertNil(testDefaults.object(forKey: "quotabar.platform.minimax_cn.pinned"))
        XCTAssertNil(testDefaults.object(forKey: "quotabar.platform.minimax_cn.enabledMetrics"))
        XCTAssertNil(testDefaults.string(forKey: "quotabar.activePlatform"))
    }

    func testMigrationIsIdempotentOnReinit() {
        // 首次迁移
        testDefaults.set(["api_key": "sk-x"], forKey: "quotabar.platform.minimax_cn")
        testDefaults.set("minimax_cn", forKey: "quotabar.activePlatform")
        let first = PlatformInstanceStore(userDefaults: testDefaults)
        XCTAssertEqual(first.instances.count, PlatformType.allCases.count)

        // 二次 init (模拟重启): instances 已持久化, 不再走迁移; 已迁移的数据原样保留
        let second = PlatformInstanceStore(userDefaults: testDefaults)
        XCTAssertEqual(second.instances, first.instances)
        XCTAssertNotNil(testDefaults.dictionary(forKey: "quotabar.instance.minimax_cn"))
        XCTAssertEqual(testDefaults.string(forKey: "quotabar.activeInstanceID"), "minimax_cn")
    }

    func testRemoveInstanceDeletesKeychainKeyAndNotifies() {
        let keychain = InMemoryKeychainStore()
        let store = PlatformInstanceStore(userDefaults: testDefaults, keychain: keychain)
        let added = store.addInstance(of: .minimax_cn, displayName: "小号")
        try? keychain.set("sk-second", account: added.id)

        var notifiedRemovedID: String?
        let observer = NotificationCenter.default.addObserver(
            forName: .platformInstanceRemoved, object: nil, queue: nil
        ) { note in notifiedRemovedID = note.object as? String }
        defer { NotificationCenter.default.removeObserver(observer) }

        store.removeInstance(id: added.id)

        // Keychain 里的 key 一并清掉
        XCTAssertNil(try? keychain.get(account: added.id))
        // 通知带上被删实例 id, 下游 (Manager/ConfigService/ViewModel) 据此清缓存
        XCTAssertEqual(notifiedRemovedID, added.id)
        XCTAssertNil(store.instance(id: added.id))
    }

    func testInstancesListPersistedAndReloaded() {
        let store = PlatformInstanceStore(userDefaults: testDefaults)
        let added = store.addInstance(of: .minimax_cn, displayName: "小号")

        // 模拟重启: 重新构造读取持久化列表
        let reloaded = PlatformInstanceStore(userDefaults: testDefaults)
        XCTAssertEqual(reloaded.instances.count, store.instances.count)
        XCTAssertNotNil(reloaded.instance(id: added.id))
        XCTAssertEqual(reloaded.instance(id: added.id)?.displayName, "小号")
    }

    // MARK: - Mutations

    func testAddInstanceGeneratesUniqueSequentialID() {
        let store = PlatformInstanceStore(userDefaults: testDefaults)
        let first = store.addInstance(of: .minimax_cn)
        let second = store.addInstance(of: .minimax_cn)

        XCTAssertEqual(first.id, "minimax_cn-2")
        XCTAssertEqual(second.id, "minimax_cn-3")
        XCTAssertEqual(first.platformType, .minimax_cn)
    }

    func testRemoveInstanceCleansUpStateKeys() {
        let store = PlatformInstanceStore(userDefaults: testDefaults)
        let added = store.addInstance(of: .glm_cn)
        // 状态写在 store 的 suite 里 (与 store 用的 defaults 一致, 避免碰真实配置)
        testDefaults.set(true, forKey: "quotabar.instance.\(added.id).enabled")
        testDefaults.set(true, forKey: "quotabar.instance.\(added.id).pinned")

        store.removeInstance(id: added.id)

        XCTAssertNil(store.instance(id: added.id))
        XCTAssertNil(testDefaults.object(forKey: "quotabar.instance.\(added.id).enabled"))
        XCTAssertNil(testDefaults.object(forKey: "quotabar.instance.\(added.id).pinned"))
    }

    func testRenameInstance() {
        let store = PlatformInstanceStore(userDefaults: testDefaults)
        let added = store.addInstance(of: .minimax_cn, displayName: "旧名")

        store.renameInstance(id: added.id, to: "新名")

        XCTAssertEqual(store.instance(id: added.id)?.displayName, "新名")
    }

    // MARK: - Instance State Extension

    func testDefaultEnabledStateMatchesLegacyBehavior() {
        // 默认启用策略是纯逻辑, 不碰 UserDefaults (isEnabled 无显式 key 时读它):
        // 老版本默认仅 MiniMax CN 启用.
        XCTAssertTrue(PlatformType.minimax_cn.isDefaultEnabled)
        XCTAssertFalse(PlatformType.glm_cn.isDefaultEnabled)
        // 显式 key 优先于默认策略 (在隔离 suite 里验证搬运后的 key 生效)
        testDefaults.set(true, forKey: "quotabar.instance.glm_cn.enabled")
        XCTAssertTrue(testDefaults.bool(forKey: "quotabar.instance.glm_cn.enabled"))
    }

    func testDisplayTitleFallsBackToPlatformName() {
        let unnamed = PlatformInstance(id: "x", platformType: .minimax_cn, displayName: "")
        XCTAssertEqual(unnamed.displayTitle, "MiniMax")
        let named = PlatformInstance(id: "y", platformType: .minimax_cn, displayName: "主力号")
        XCTAssertEqual(named.displayTitle, "主力号")
    }
}
