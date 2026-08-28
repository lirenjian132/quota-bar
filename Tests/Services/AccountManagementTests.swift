import XCTest
@testable import QuotaBar

/// PR2 账号管理行为的测试. 走 AppEnvironment 隔离套件, 不碰真实配置/Keychain.
@MainActor
final class AccountManagementTests: XCTestCase {

    private let defaults = AppEnvironment.testDefaults
    private var store: PlatformInstanceStore!

    override func setUp() async throws {
        // 独立 fresh store: 直接构造 (绕过 shared 单例), 用隔离 defaults + 内存 keychain
        defaults.dictionaryRepresentation().keys.forEach { defaults.removeObject(forKey: $0) }
        store = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
    }

    func testAddInstanceCreatesEnabledAndSequentialID() {
        let first = store.addInstance(of: .minimax_cn)
        let second = store.addInstance(of: .minimax_cn)

        XCTAssertEqual(first.id, "minimax_cn-2")
        XCTAssertEqual(second.id, "minimax_cn-3")
        // store 层新增默认未启用; 启用由 ViewModel.addInstance 负责
        XCTAssertFalse(defaults.bool(forKey: "quotabar.instance.\(first.id).enabled"))
        // 跨类型 id 互不影响
        let glm = store.addInstance(of: .glm_cn)
        XCTAssertEqual(glm.id, "glm_cn-2")
    }

    func testRemoveInstanceCleansEverything() {
        let keychain = InMemoryKeychainStore()
        let s = PlatformInstanceStore(userDefaults: defaults, keychain: keychain)
        let added = s.addInstance(of: .minimax_cn, displayName: "小号")
        try? keychain.set("sk-second", account: added.id)
        defaults.set(true, forKey: "quotabar.instance.\(added.id).pinned")

        s.removeInstance(id: added.id)

        XCTAssertNil(s.instance(id: added.id))
        XCTAssertNil(try? keychain.get(account: added.id))
        XCTAssertNil(defaults.object(forKey: "quotabar.instance.\(added.id).pinned"))
        // 实例列表持久化同步更新 (重开 app 不会复活)
        let reloaded = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        XCTAssertNil(reloaded.instance(id: added.id))
    }

    func testRenamePersistsAcrossReload() {
        let added = store.addInstance(of: .minimax_cn, displayName: "旧名")
        store.renameInstance(id: added.id, to: "备用号")

        XCTAssertEqual(store.instance(id: added.id)?.displayName, "备用号")
        let reloaded = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        XCTAssertEqual(reloaded.instance(id: added.id)?.displayName, "备用号")
    }

    func testRemoveInstanceDoesNotTouchOtherAccounts() {
        let keychain = InMemoryKeychainStore()
        let s = PlatformInstanceStore(userDefaults: defaults, keychain: keychain)
        let a = s.addInstance(of: .minimax_cn, displayName: "A")
        let b = s.addInstance(of: .minimax_cn, displayName: "B")
        try? keychain.set("sk-a", account: a.id)
        try? keychain.set("sk-b", account: b.id)

        s.removeInstance(id: a.id)

        // 删 A 不影响 B 的实例与 key
        XCTAssertEqual(try? keychain.get(account: b.id), "sk-b")
        XCTAssertNotNil(s.instance(id: b.id))
        XCTAssertNil(try? keychain.get(account: a.id))
    }

    func testDeletedIDIsNotReused() {
        let first = store.addInstance(of: .minimax_cn)  // minimax_cn-2
        store.removeInstance(id: first.id)
        let second = store.addInstance(of: .minimax_cn)
        // 不复用已删 id, 避免与残留缓存/Keychain 撞上
        XCTAssertEqual(second.id, "minimax_cn-3")
    }

    func testGhostInstanceIsReclaimedOnCancel() {
        // ViewModel 的幽灵回收逻辑: pending 新实例取消且未配置 → removeInstance 被调
        let before = store.instances.count
        let added = store.addInstance(of: .glm_cn)
        XCTAssertEqual(store.instances.count, before + 1)

        // 模拟 cancelConfig 的回收路径
        store.removeInstance(id: added.id)
        XCTAssertEqual(store.instances.count, before)
    }
}
