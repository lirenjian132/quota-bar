import Foundation

/// KeychainStoring 的内存实现: 单元测试 / 测试宿主进程用, 见 AppEnvironment.
final class InMemoryKeychainStore: KeychainStoring {
    private var storage: [String: String] = [:]
    var setError: Error?
    var getError: Error?
    var deleteError: Error?

    func get(account: String) throws -> String? {
        if let getError { throw getError }
        return storage[account]
    }

    func set(_ value: String, account: String) throws {
        if let setError { throw setError }
        storage[account] = value
    }

    func delete(account: String) throws {
        if let deleteError { throw deleteError }
        storage.removeValue(forKey: account)
    }
}
