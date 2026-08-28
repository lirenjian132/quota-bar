import Foundation

extension Notification.Name {
    /// 账号实例被删除. object 是被删实例的 id (String).
    /// 下游 (PlatformManager/ConfigService/PlatformViewModel) 监听清理各自的 per-实例 缓存.
    static let platformInstanceRemoved = Notification.Name("platformInstanceRemoved")
    /// 实例列表顺序变化 (左移/右移). 状态栏 item 位置 = 创建顺序, 控制器需全拆重建.
    static let platformInstancesReordered = Notification.Name("platformInstancesReordered")
}

/// 账号实例注册表: 持有全部 PlatformInstance, 列表持久化在 UserDefaults (JSON).
/// 首次启动 (或从按平台类型存储的老版本升级) 时, 把旧 key 结构迁移成每类型一个默认实例.
final class PlatformInstanceStore {
    static let shared = PlatformInstanceStore(userDefaults: AppEnvironment.defaults, keychain: AppEnvironment.makeKeychain())

    private var _instances: [PlatformInstance]
    private let instancesLock = NSLock()
    private let defaults: UserDefaults
    private let defaultsKey = "quotabar.instances"
    // 删除实例时清 Keychain 用. 默认真实实现, 测试可注入内存版.
    private let keychain: KeychainStoring

    init(userDefaults: UserDefaults = AppEnvironment.defaults, keychain: KeychainStoring = AppEnvironment.makeKeychain()) {
        self.defaults = userDefaults
        self.keychain = keychain
        if let data = defaults.data(forKey: defaultsKey),
           let list = try? JSONDecoder().decode([PlatformInstance].self, from: data),
           !list.isEmpty {
            _instances = list
        } else {
            _instances = Self.migrateLegacyPerTypeConfig(into: userDefaults)
            persistLocked()
        }
    }

    /// 实例列表 (注册顺序). 加锁拷贝, 跨线程读安全.
    var instances: [PlatformInstance] {
        instancesLock.lock()
        defer { instancesLock.unlock() }
        return _instances
    }

    // MARK: - Mutations

    /// 添加同平台的新账号实例, 返回创建的实例.displayName 默认为空 (显示时回退平台名).
    @discardableResult
    func addInstance(of type: PlatformType, displayName: String = "") -> PlatformInstance {
        let instance = PlatformInstance(
            id: PlatformInstance.nextID(for: type, existingIDs: Set(instances.map(\.id))),
            platformType: type,
            displayName: displayName
        )
        instancesLock.lock()
        _instances.append(instance)
        instancesLock.unlock()
        persistLocked()
        return instance
    }

    /// 删除实例: 清掉 UserDefaults 状态 + Keychain 里的 API key, 并发
    /// `.platformInstanceRemoved` (下游清 services/stores/字典缓存) 和
    /// `.platformEnabledChanged` (重建状态栏钉选 item).
    func removeInstance(id: String) {
        instancesLock.lock()
        let existed = _instances.contains { $0.id == id }
        _instances.removeAll { $0.id == id }
        instancesLock.unlock()
        guard existed else { return }
        persistLocked()

        let prefix = "quotabar.instance.\(id)"
        for suffix in ["enabled", "pinned", "enabledMetrics"] {
            defaults.removeObject(forKey: "\(prefix).\(suffix)")
        }
        defaults.removeObject(forKey: prefix)
        // 墓碑: 阻止 nextID 复用已删 id (复用会撞残留 Keychain 条目)
        defaults.set(true, forKey: "quotabar.instance.\(id).tombstone")
        // 清 Keychain 里的 API key (失败不阻塞删除, 残留只能手动清, 记日志也没出口—静默容错)
        _ = try? keychain.delete(account: id)

        NotificationCenter.default.post(name: .platformInstanceRemoved, object: id)
        NotificationCenter.default.post(name: .platformEnabledChanged, object: nil)
    }

    func renameInstance(id: String, to name: String) {
        instancesLock.lock()
        guard let idx = _instances.firstIndex(where: { $0.id == id }) else {
            instancesLock.unlock()
            return
        }
        _instances[idx].displayName = name
        instancesLock.unlock()
        persistLocked()
    }

    /// 左移/右移调整实例在列表 (及状态栏/菜单) 中的顺序. offset 越界时不动.
    func moveInstance(id: String, offset: Int) {
        instancesLock.lock()
        guard let idx = _instances.firstIndex(where: { $0.id == id }) else {
            instancesLock.unlock()
            return
        }
        let target = idx + offset
        guard target >= 0, target < _instances.count else {
            instancesLock.unlock()
            return
        }
        let instance = _instances.remove(at: idx)
        _instances.insert(instance, at: target)
        instancesLock.unlock()
        persistLocked()
        NotificationCenter.default.post(name: .platformInstancesReordered, object: nil)
    }

    func instance(id: String) -> PlatformInstance? {
        instancesLock.lock()
        defer { instancesLock.unlock() }
        return _instances.first { $0.id == id }
    }

    /// 调用方需已持锁 (内部写盘).
    private func persistLocked() {
        instancesLock.lock()
        let data = try? JSONEncoder().encode(_instances)
        instancesLock.unlock()
        guard let data else { return }
        defaults.set(data, forKey: defaultsKey)
    }

    // MARK: - Legacy Migration

    /// 老版本 (< 多账号) 按平台类型存配置:
    ///   quotabar.platform.<type>              → 配置 dict
    ///   quotabar.platform.<type>.enabled/pinned/enabledMetrics → 状态
    ///   quotabar.activePlatform               → 激活平台 rawValue
    /// 迁移成每类型一个默认实例 (id == rawValue, Keychain account 恰好不变), 旧 key 搬完即清.
    /// 迁移按 key 逐条"先写新后删旧", 中途被杀后重跑能续搬剩余 key, 幂等.
    static func migrateLegacyPerTypeConfig(into defaults: UserDefaults) -> [PlatformInstance] {
        var migrated: [PlatformInstance] = []
        for type in PlatformType.allCases {
            let instance = PlatformInstance(id: type.rawValue, platformType: type, displayName: "")
            let legacyPrefix = "quotabar.platform.\(type.rawValue)"
            let newPrefix = "quotabar.instance.\(type.rawValue)"

            if let dict = defaults.dictionary(forKey: legacyPrefix) {
                defaults.set(dict, forKey: newPrefix)
                defaults.removeObject(forKey: legacyPrefix)
            }
            for suffix in ["enabled", "pinned", "enabledMetrics"] {
                let legacyKey = "\(legacyPrefix).\(suffix)"
                if let value = defaults.object(forKey: legacyKey) {
                    defaults.set(value, forKey: "\(newPrefix).\(suffix)")
                    defaults.removeObject(forKey: legacyKey)
                }
            }
            migrated.append(instance)
        }
        // activePlatform 的 rawValue 与默认实例 id 相同, 直接平移.
        if let raw = defaults.string(forKey: "quotabar.activePlatform") {
            defaults.set(raw, forKey: "quotabar.activeInstanceID")
            defaults.removeObject(forKey: "quotabar.activePlatform")
        }
        return migrated
    }
}

extension PlatformType {
    /// 无用户显式选择时的默认启用策略 (延续老版本: 仅 MiniMax CN).
    var isDefaultEnabled: Bool { self == .minimax_cn }
}

extension PlatformInstance {
    private var statePrefix: String { "quotabar.instance.\(id)" }

    var isEnabled: Bool {
        get {
            let key = "\(statePrefix).enabled"
            if AppEnvironment.defaults.object(forKey: key) == nil {
                // 无显式状态时用平台默认策略 (默认实例延续老版本行为)
                return platformType.isDefaultEnabled && id == platformType.rawValue
            }
            return AppEnvironment.defaults.bool(forKey: key)
        }
        set { AppEnvironment.defaults.set(newValue, forKey: "\(statePrefix).enabled") }
    }

    // 钉选到状态栏: pinned 的实例会常驻状态栏, 各占一块独立显示.
    var isPinned: Bool {
        get { AppEnvironment.defaults.bool(forKey: "\(statePrefix).pinned") }
        set { AppEnvironment.defaults.set(newValue, forKey: "\(statePrefix).pinned") }
    }

    /// 所有已钉选且已启用的实例, 按注册表顺序.
    /// 禁用的实例即使 isPinned=true 也不会显示 (避免状态不一致).
    static var allPinned: [PlatformInstance] {
        PlatformInstanceStore.shared.instances.filter { $0.isPinned && $0.isEnabled }
    }
}
