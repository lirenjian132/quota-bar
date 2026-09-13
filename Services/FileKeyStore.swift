import Foundation

/// API key 的文件存储: ~/Library/Application Support/QuotaBar/keys.json (0600).
///
/// 为什么不用 Keychain: 本 app 是 ad-hoc 签名 (无稳定代码身份), macOS 钥匙串无法
/// 跨构建/跨启动持久记住授权 — 每次启动甚至每次重新构建安装都弹登录密码.
/// 对个人 Mac 上的菜单栏工具, 0600 私有文件 (仅当前用户可读) 的保护粒度足够.
final class FileKeyStore: KeychainStoring {
    let fileURL: URL
    private let lock = NSLock()
    private var cache: [String: String]

    init(directory: URL? = nil, filename: String = "keys.json") {
        let dir = directory ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("QuotaBar", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
        fileURL = dir.appendingPathComponent(filename)

        if let data = try? Data(contentsOf: fileURL),
           let dict = try? JSONDecoder().decode([String: String].self, from: data) {
            cache = dict
        } else {
            cache = [:]
        }
        // 老文件权限校正 (可能以默认权限创建过)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
    }

    func get(account: String) throws -> String? {
        lock.lock()
        defer { lock.unlock() }
        return cache[account]
    }

    func set(_ value: String, account: String) throws {
        lock.lock()
        defer { lock.unlock() }
        let previous = cache[account]
        cache[account] = value
        do {
            try persistLocked(cache)
        } catch {
            cache[account] = previous  // 写盘失败回滚: 内存与文件保持一致, 不静默丢输入
            throw error
        }
    }

    func delete(account: String) throws {
        lock.lock()
        defer { lock.unlock() }
        cache.removeValue(forKey: account)
        do {
            try persistLocked(cache)
        } catch {
            cache[account] = nil  // 删除失败回滚同上
            throw error
        }
    }

    /// 调用方必须已持锁 (锁内写盘, 杜绝并发 snapshot 互相覆盖).
    private func persistLocked(_ dict: [String: String]) throws {
        let data = try JSONEncoder().encode(dict)
        try data.write(to: fileURL, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
    }
}

/// 一次性迁移: 把 Keychain (旧存储) 里的各账号 API key 搬进 FileKeyStore,
/// 搬完删除 Keychain 条目 — 此后 app 不再触碰 Keychain, 密码弹窗绝迹.
/// 读 Keychain 的那一下可能弹最后一次授权 (ad-hoc 签名无法持久授权), 点允许即可.
/// 幂等: 文件里已有的账号跳过; Keychain 条目不存在则无感.
enum KeychainToFileMigration {
    /// 生产入口: 全部走真实存储. 测试宿主进程禁入 (AppDelegate 也挡了一层).
    static func run() {
        guard !AppEnvironment.isRunningTests else { return }
        run(keychain: KeychainStore(),
            file: AppEnvironment.makeKeychain(),
            accounts: PlatformInstanceStore.shared.instances.map(\.id))
    }

    /// 注入式核心, 单元测试直调 (不受测试环境守卫限制).
    static func run(keychain: KeychainStoring, file: KeychainStoring, accounts: [String]) {
        for account in accounts {
            guard let key = try? keychain.get(account: account), !key.isEmpty else { continue }
            if let existing = try? file.get(account: account), !existing.isEmpty {
                // 文件里已有 → Keychain 条目是残留, 清掉
                try? keychain.delete(account: account)
                continue
            }
            do {
                try file.set(key, account: account)
            } catch {
                // 写文件失败: 保留 Keychain 条目, 下次启动再试.
                // (旧版 try? 吞掉写失败仍继续删 Keychain, 会直接销毁 key)
                continue
            }
            try? keychain.delete(account: account)
        }
    }
}
