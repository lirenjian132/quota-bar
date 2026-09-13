import XCTest
@testable import QuotaBar

/// FileKeyStore: 持久化 / 权限 / 跨实例读取 (生产 key 存储的正确性前提).
final class FileKeyStoreTests: XCTestCase {

    private func makeTempDir() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("file-key-store-\(UUID().uuidString)", isDirectory: true)
    }

    func testSetGetDeleteRoundTrip() throws {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = FileKeyStore(directory: dir)

        try store.set("sk-abc", account: "minimax_cn")
        XCTAssertEqual(try store.get(account: "minimax_cn"), "sk-abc")
        XCTAssertNil(try store.get(account: "other"))

        try store.delete(account: "minimax_cn")
        XCTAssertNil(try store.get(account: "minimax_cn"))
    }

    func testPersistsAcrossInstances() throws {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }

        try FileKeyStore(directory: dir).set("sk-1", account: "a")
        try FileKeyStore(directory: dir).set("sk-2", account: "b")

        // 模拟重启: 新实例能读回全部 (后写的不冲掉先写的)
        let reloaded = FileKeyStore(directory: dir)
        XCTAssertEqual(try reloaded.get(account: "a"), "sk-1")
        XCTAssertEqual(try reloaded.get(account: "b"), "sk-2")
    }

    func testFilePermissionsOwnerOnly() throws {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = FileKeyStore(directory: dir)
        try store.set("sk-secret", account: "a")

        let attrs = try FileManager.default.attributesOfItem(atPath: store.fileURL.path)
        let perms = (attrs[.posixPermissions] as? NSNumber)?.int16Value ?? 0
        XCTAssertEqual(perms, 0o600, "key 文件必须仅当前用户可读写")
    }

    func testMigrationCopiesFromKeychainAndDeletesSource() throws {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let old = InMemoryKeychainStore()
        let file = FileKeyStore(directory: dir)
        try old.set("sk-legacy", account: "minimax_cn")
        try old.set("sk-keep", account: "glm_cn")

        KeychainToFileMigration.run(keychain: old, file: file, accounts: ["minimax_cn", "glm_cn", "minimax_cn-2"])

        // 搬进文件 + 源删除
        XCTAssertEqual(try file.get(account: "minimax_cn"), "sk-legacy")
        XCTAssertNil(try old.get(account: "minimax_cn"))
        XCTAssertEqual(try file.get(account: "glm_cn"), "sk-keep")
        XCTAssertNil(try old.get(account: "glm_cn"))
    }

    func testMigrationDoesNotOverwriteExistingFileKey() throws {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let old = InMemoryKeychainStore()
        let file = FileKeyStore(directory: dir)
        try file.set("sk-file", account: "minimax_cn")
        try old.set("sk-stale", account: "minimax_cn")

        KeychainToFileMigration.run(keychain: old, file: file, accounts: ["minimax_cn"])

        // 文件已有 → 保留文件的, 清掉 Keychain 残留
        XCTAssertEqual(try file.get(account: "minimax_cn"), "sk-file")
        XCTAssertNil(try old.get(account: "minimax_cn"))
    }
}

/// 2026-09-14 事故回归: 测试宿主曾以真实 Keychain 执行迁移销毁用户 key.
final class KeychainMigrationSafetyTests: XCTestCase {

    /// 文件写失败时, Keychain 条目必须保留 (下次再试), 不能删了完事.
    func testMigrationKeepsKeychainWhenFileWriteFails() throws {
        let old = InMemoryKeychainStore()
        try old.set("sk-precious", account: "minimax_cn")

        struct WriteBoom: Error {}
        final class BoomFileStore: KeychainStoring {
            func get(account: String) throws -> String? { nil }
            func set(_ value: String, account: String) throws { throw WriteBoom() }
            func delete(account: String) throws {}
        }

        KeychainToFileMigration.run(keychain: old, file: BoomFileStore(), accounts: ["minimax_cn"])

        XCTAssertEqual(try old.get(account: "minimax_cn"), "sk-precious")
    }
}

final class FileKeyStoreRollbackTests: XCTestCase {

    private func makeTempDir() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("file-key-store-rollback-\(UUID().uuidString)", isDirectory: true)
    }

    /// 写盘失败时内存回滚: 读到的仍是旧值, 且文件内容未被破坏 (不出现"看似生效重启即丢").
    func testSetRollsBackOnWriteFailure() throws {
        let dir = makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = FileKeyStore(directory: dir)
        try store.set("sk-old", account: "a")
        let original = try Data(contentsOf: store.fileURL)

        // 把文件换成目录 → Data.write 必然失败
        try FileManager.default.removeItem(at: store.fileURL)
        try FileManager.default.createDirectory(at: store.fileURL, withIntermediateDirectories: false)

        XCTAssertThrowsError(try store.set("sk-new", account: "a"))
        XCTAssertEqual(try store.get(account: "a"), "sk-old")  // 内存回滚

        // 恢复原文件后新实例重载, 仍是旧值 (内存与文件一致, 无静默丢失)
        try FileManager.default.removeItem(at: store.fileURL)
        try original.write(to: store.fileURL)
        let reloaded = FileKeyStore(directory: dir)
        XCTAssertEqual(try reloaded.get(account: "a"), "sk-old")
    }
}
