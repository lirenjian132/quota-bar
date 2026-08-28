import Foundation

enum DisplayMode: String, Codable {
    case used
    case remaining
}

final class ConfigService {
    static let shared = ConfigService()

    private var cachedDisplayMode: DisplayMode = .used
    private var cachedActiveInstanceID: String = PlatformType.minimax_cn.rawValue
    private var cachedRefreshInterval: RefreshInterval = .default
    private var platformStores: [String: PlatformConfigStore] = [:]
    // platformStores 被 fetchAllUsage 的并发任务同时读写, 必须加锁保护字典结构.
    private let storesLock = NSLock()
    // cached 全局配置可能在多线程下读写 (UI 主线程 + 切换平台), 加锁保护.
    private let configLock = NSLock()

    private let defaults: UserDefaults
    private let keychain: KeychainStoring

    private init(defaults: UserDefaults = AppEnvironment.defaults,
                 keychain: KeychainStoring = AppEnvironment.makeKeychain()) {
        self.defaults = defaults
        self.keychain = keychain
        loadGlobalConfig()
        cleanupLegacyPlatformKeys()

        // 实例被删除时清掉它缓存的 ConfigStore, 避免字典残留.
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleInstanceRemoved(_:)),
            name: .platformInstanceRemoved,
            object: nil
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    @objc private func handleInstanceRemoved(_ note: Notification) {
        guard let id = note.object as? String else { return }
        storesLock.lock()
        platformStores.removeValue(forKey: id)
        storesLock.unlock()
    }

    // MARK: - Global Config

    var displayMode: DisplayMode {
        get { configLock.lock(); defer { configLock.unlock() }; return cachedDisplayMode }
        set {
            configLock.lock()
            cachedDisplayMode = newValue
            configLock.unlock()
            saveGlobalConfig()
        }
    }

    /// 激活的账号实例. 读时若 id 已不存在 (实例被删), 回退到第一个可用实例.
    var activeInstance: PlatformInstance {
        get {
            configLock.lock()
            let id = cachedActiveInstanceID
            configLock.unlock()
            let store = PlatformInstanceStore.shared
            if let instance = store.instance(id: id) { return instance }
            let fallback = store.instances.first ?? PlatformInstance(id: PlatformType.minimax_cn.rawValue, platformType: .minimax_cn, displayName: "")
            return fallback
        }
        set {
            configLock.lock()
            cachedActiveInstanceID = newValue.id
            configLock.unlock()
            saveGlobalConfig()
        }
    }

    var refreshInterval: RefreshInterval {
        get { configLock.lock(); defer { configLock.unlock() }; return cachedRefreshInterval }
        set {
            configLock.lock()
            cachedRefreshInterval = newValue
            configLock.unlock()
            saveGlobalConfig()
        }
    }

    // MARK: - Platform Stores

    func store(for instance: PlatformInstance) -> PlatformConfigStore {
        storesLock.lock()
        defer { storesLock.unlock() }
        if let existing = platformStores[instance.id] {
            return existing
        }
        let store = PlatformConfigStore(instance: instance, keychain: keychain, userDefaults: defaults)
        platformStores[instance.id] = store
        return store
    }

    func configuredInstances() -> [PlatformInstance] {
        PlatformInstanceStore.shared.instances.filter { store(for: $0).isConfigured }
    }

    var allEnabledInstances: [PlatformInstance] {
        PlatformInstanceStore.shared.instances.filter { $0.isEnabled }
    }

    // MARK: - Private

    private func loadGlobalConfig() {
        if let raw = defaults.string(forKey: "quotabar.displayMode"),
           let mode = DisplayMode(rawValue: raw) {
            cachedDisplayMode = mode
        }
        // 老版本把激活平台存在 quotabar.activePlatform; 默认实例 id 与平台 rawValue
        // 相同, 两者可无缝互认, 新 key 优先.
        if let raw = defaults.string(forKey: "quotabar.activeInstanceID")
            ?? defaults.string(forKey: "quotabar.activePlatform") {
            cachedActiveInstanceID = raw
        } else {
            cachedActiveInstanceID = PlatformType.minimax_cn.rawValue
        }
        if let raw = defaults.string(forKey: "quotabar.refreshInterval"),
           let interval = RefreshInterval(rawValue: raw) {
            cachedRefreshInterval = interval
        }
    }

    private func saveGlobalConfig() {
        // 加锁读快照再放锁写盘: 避免 setter 放锁后被另一线程插队改 cached,
        // 导致写到盘上的是混合状态.
        configLock.lock()
        let displayModeRaw = cachedDisplayMode.rawValue
        let activeInstanceRaw = cachedActiveInstanceID
        let refreshIntervalRaw = cachedRefreshInterval.rawValue
        configLock.unlock()

        defaults.set(displayModeRaw, forKey: "quotabar.displayMode")
        defaults.set(activeInstanceRaw, forKey: "quotabar.activeInstanceID")
        defaults.set(refreshIntervalRaw, forKey: "quotabar.refreshInterval")
    }

    /// 清理已从 PlatformType 删除的平台的残留 UserDefaults 配置 (含老版本明文 api_key).
    /// 这些 key 是历史版本写入的, enum 里已无对应 case, 留着是无害的死数据, 顺手清掉.
    private func cleanupLegacyPlatformKeys() {
        for legacy in ["minimax_en", "glm_en", "kimi", "deepseek", "mimo", "stepfun"] {
            let prefix = "quotabar.platform.\(legacy)"
            defaults.removeObject(forKey: prefix)
            defaults.removeObject(forKey: "\(prefix).enabled")
            defaults.removeObject(forKey: "\(prefix).pinned")
            defaults.removeObject(forKey: "\(prefix).enabledMetrics")
        }
    }

    // MARK: - Enabled Metrics

    /// 每个账号实例用户勾选要在菜单栏显示的 metric label 列表. 顺序即显示顺序.
    /// getter 优先读 UserDefaults, 无值时返回平台默认值 (首次安装 / 老用户升级).
    func enabledMetrics(for instance: PlatformInstance) -> [String] {
        configLock.lock()
        defer { configLock.unlock() }
        let key = "quotabar.instance.\(instance.id).enabledMetrics"
        if let raw = defaults.string(forKey: key),
           let data = raw.data(using: .utf8),
           let labels = try? JSONDecoder().decode([String].self, from: data),
           !labels.isEmpty, labels.count <= 2 {
            return labels
        }
        return Self.defaultEnabledMetrics(for: instance.platformType)
    }

    /// 平台首次安装的默认勾选. 改了这里会改变新用户体验, 不影响已配置的用户.
    static func defaultEnabledMetrics(for platform: PlatformType) -> [String] {
        switch platform {
        case .minimax_cn:
            return ["five_hour"]
        case .glm_cn:
            return ["five_hour", "weekly_limit"]
        }
    }

    /// 设置实例启用的 metric label 列表. 拒绝空数组 (保留旧值) 和长度 > 2 的数组.
    /// 写入成功时发 `.enabledMetricsChanged` 通知.
    func setEnabledMetrics(_ labels: [String], for instance: PlatformInstance) {
        guard !labels.isEmpty, labels.count <= 2 else { return }

        configLock.lock()
        let key = "quotabar.instance.\(instance.id).enabledMetrics"
        let encoded = (try? JSONEncoder().encode(labels)).flatMap { String(data: $0, encoding: .utf8) }
        configLock.unlock()

        guard let encoded else { return }
        defaults.set(encoded, forKey: key)
        NotificationCenter.default.post(name: .enabledMetricsChanged, object: instance.id)
    }
}

extension Notification.Name {
    static let enabledMetricsChanged = Notification.Name("enabledMetricsChanged")
}
