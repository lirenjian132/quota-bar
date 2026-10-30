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
    /// 非 nil = 读阶段出了问题(文件被占用/权限不足/内容损坏),进入只读模式:
    /// 拒绝一切写盘,防止"空缓存"把磁盘上其它账号的 key 整体覆盖掉.
    private let readOnlyReason: String?

    init(directory: URL? = nil, filename: String = "keys.json") {
        let dir = directory ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("QuotaBar", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
        fileURL = dir.appendingPathComponent(filename)

        // 读失败/解码失败 ≠ "文件不存在":前者若按空缓存继续跑,用户保存任意
        // 一个账号的 key 就会把整份只含该 key 的缓存写回,其它账号的凭据从磁盘
        // 永久消失(Windows 版已按只读模式处理,这里对齐).
        var readOnly: String? = nil
        if FileManager.default.fileExists(atPath: fileURL.path) {
            if let data = try? Data(contentsOf: fileURL) {
                if let dict = try? JSONDecoder().decode([String: String].self, from: data) {
                    cache = dict
                } else {
                    cache = [:]
                    readOnly = "decode"
                    // 留一份原始字节:现场损坏时还有证据可人工恢复
                    try? data.write(to: fileURL.appendingPathExtension("bak"), options: .atomic)
                }
            } else {
                cache = [:]
                readOnly = "read"
            }
        } else {
            cache = [:]
        }
        readOnlyReason = readOnly
        // 老文件权限校正 (可能以默认权限创建过)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
    }

    /// 当前是否处于只读模式(读失败/损坏);UI 可据此提示用户
    var isReadOnly: Bool { readOnlyReason != nil }

    func get(account: String) throws -> String? {
        lock.lock()
        defer { lock.unlock() }
        return cache[account]
    }

    func set(_ value: String, account: String) throws {
        lock.lock()
        defer { lock.unlock() }
        try ensureWritableLocked()
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
        try ensureWritableLocked()
        let previous = cache.removeValue(forKey: account)
        do {
            try persistLocked(cache)
        } catch {
            // 回滚到旧值(旧实现写 nil 等于没回滚:内存丢 key 而文件还在,
            // 之后任意一次成功写盘会把磁盘上的 key 静默删掉)
            cache[account] = previous
            throw error
        }
    }

    private func ensureWritableLocked() throws {
        if readOnlyReason != nil {
            // 读失败/损坏时拒绝写入:空缓存写回就是数据丢失
            throw CocoaError(.fileWriteUnknown)
        }
    }

    /// 调用方必须已持锁 (锁内写盘, 杜绝并发 snapshot 互相覆盖).
    private func persistLocked(_ dict: [String: String]) throws {
        let data = try JSONEncoder().encode(dict)
        // 不能用 Data.write(.atomic): 其临时文件按 umask (通常 0644) 创建,
        // rename 之后到 setAttributes 之前 keys.json 有一小段世界可读窗口,
        // 同机其他本地用户此刻可读到全部 API key/cookie. 这里自己落 0600 的
        // 临时文件再原子替换, 权限从文件创建那一刻就是对的.
        let tmp = fileURL.deletingLastPathComponent()
            // 用 ProcessInfo 而不是 getpid():本文件只 import Foundation,
            // getpid 来自 Darwin 模块,不能赌 SDK 会随 Foundation 透出
            .appendingPathComponent(".\(fileURL.lastPathComponent).\(ProcessInfo.processInfo.processIdentifier).tmp")
        guard FileManager.default.createFile(
            atPath: tmp.path,
            contents: data,
            attributes: [.posixPermissions: 0o600]
        ) else {
            throw CocoaError(.fileWriteUnknown)
        }
        do {
            var isDirectory: ObjCBool = false
            if FileManager.default.fileExists(atPath: fileURL.path, isDirectory: &isDirectory) {
                // 目录霸占路径时直接失败:replaceItemAt 能把目录静默替换成文件,
                // 会废掉"写失败 → 调用方回滚内存"的不变式, 也把故障变成未评审的行为
                guard !isDirectory.boolValue else {
                    throw CocoaError(.fileWriteUnknown)
                }
                // usingNewMetadataOnly: 默认行为会把**被替换旧文件**的权限重新贴到
                // 新文件上, 临时文件的 0600 会被丢弃 —— 旧文件若是 0644,
                // 每次原子替换都会忠实继承宽松权限. 只采用新文件的元数据.
                _ = try FileManager.default.replaceItemAt(
                    fileURL,
                    withItemAt: tmp,
                    options: [.usingNewMetadataOnly]
                )
            } else {
                try FileManager.default.moveItem(at: tmp, to: fileURL)
            }
        } catch {
            try? FileManager.default.removeItem(at: tmp)
            throw error
        }
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
            // 读错误与"条目不存在"都跳过该账号: 关键在于**任何非成功路径都不删
            // Keychain 条目**, 条目留到下次启动 (系统已解锁) 重试, 不会因为
            // 开机启动且未解锁时的 errSecInteractionNotAllowed 丢凭据.
            // 显式 do/catch 而非 try?: 让"读失败"成为代码里可见的分支, 防止
            // 后续修改把 guard 的 else 与删除逻辑接错.
            let key: String?
            do {
                key = try keychain.get(account: account)
            } catch {
                continue
            }
            guard let key, !key.isEmpty else { continue }
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
