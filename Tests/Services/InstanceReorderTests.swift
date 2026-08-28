
import XCTest
@testable import QuotaBar

/// 账号排序 (左移/右移) 行为测试.
final class InstanceReorderTests: XCTestCase {

    private let defaults = AppEnvironment.testDefaults

    override func setUp() {
        super.setUp()
        defaults.dictionaryRepresentation().keys.forEach { defaults.removeObject(forKey: $0) }
    }

    func testMoveRightSwapsOrder() {
        let store = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        let a = store.addInstance(of: .minimax_cn, displayName: "A")
        let b = store.addInstance(of: .minimax_cn, displayName: "B")
        // 初始: [默认..., A, B]
        XCTAssertEqual(store.instances.map(\.displayName).suffix(2), ["A", "B"])

        store.moveInstance(id: a.id, offset: 1)
        XCTAssertEqual(store.instances.map(\.displayName).suffix(2), ["B", "A"])

        // 顺序持久化 (重启后保持)
        let reloaded = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        XCTAssertEqual(reloaded.instances.map(\.displayName).suffix(2), ["B", "A"])
    }

    func testMoveBoundsIgnored() {
        let store = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        let first = store.instances.first!.id
        store.moveInstance(id: first, offset: -1)  // 已是最左, 不动
        XCTAssertEqual(store.instances.first?.id, first)
    }

    func testMoveUnknownIDIgnored() {
        let store = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        let before = store.instances
        store.moveInstance(id: "no-such-id", offset: 1)
        XCTAssertEqual(store.instances, before)
    }

    func testReorderPostsNotification() {
        let store = PlatformInstanceStore(userDefaults: defaults, keychain: InMemoryKeychainStore())
        let a = store.addInstance(of: .glm_cn, displayName: "A")
        var fired = false
        let obs = NotificationCenter.default.addObserver(
            forName: .platformInstancesReordered, object: nil, queue: nil
        ) { _ in fired = true }
        defer { NotificationCenter.default.removeObserver(obs) }

        store.moveInstance(id: a.id, offset: -1)
        XCTAssertTrue(fired)
    }
}
