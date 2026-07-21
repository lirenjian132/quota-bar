import Foundation

enum DisplayMode: String, Codable {
    case used
    case remaining
}

final class ConfigService {
    static let shared = ConfigService()

    private var cachedDisplayMode: DisplayMode = .used
    private var cachedActivePlatform: PlatformType = .minimax_cn
    private var cachedRefreshInterval: RefreshInterval = .default
    private var platformStores: [PlatformType: PlatformConfigStore] = [:]
    // platformStores 被 fetchAllUsage 的并发任务同时读写, 必须加锁保护字典结构.
    private let storesLock = NSLock()
    // cached 全局配置可能在多线程下读写 (UI 主线程 + 切换平台), 加锁保护.
    private let configLock = NSLock()

    private init() {
        loadGlobalConfig()
        cleanupLegacyPlatformKeys()
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

    var activePlatform: PlatformType {
        get { configLock.lock(); defer { configLock.unlock() }; return cachedActivePlatform }
        set {
            configLock.lock()
            cachedActivePlatform = newValue
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

    func store(for platform: PlatformType) -> PlatformConfigStore {
        storesLock.lock()
        defer { storesLock.unlock() }
        if let existing = platformStores[platform] {
            return existing
        }
        let store = PlatformConfigStore(platformType: platform)
        platformStores[platform] = store
        return store
    }

    func configuredPlatforms() -> [PlatformType] {
        PlatformType.allCases.filter { store(for: $0).isConfigured }
    }

    var allEnabledPlatforms: [PlatformType] {
        PlatformType.allCases.filter { $0.isEnabled }
    }

    // MARK: - Private

    private func loadGlobalConfig() {
        if let raw = UserDefaults.standard.string(forKey: "quotabar.displayMode"),
           let mode = DisplayMode(rawValue: raw) {
            cachedDisplayMode = mode
        }
        if let raw = UserDefaults.standard.string(forKey: "quotabar.activePlatform"),
           let platform = PlatformType(rawValue: raw) {
            cachedActivePlatform = platform
        }
        if let raw = UserDefaults.standard.string(forKey: "quotabar.refreshInterval"),
           let interval = RefreshInterval(rawValue: raw) {
            cachedRefreshInterval = interval
        }
    }

    private func saveGlobalConfig() {
        // 加锁读快照再放锁写盘: 避免 setter 放锁后被另一线程插队改 cached,
        // 导致写到盘上的是混合状态 (CodeRabbit 指出的竞态).
        configLock.lock()
        let displayModeRaw = cachedDisplayMode.rawValue
        let activePlatformRaw = cachedActivePlatform.rawValue
        let refreshIntervalRaw = cachedRefreshInterval.rawValue
        configLock.unlock()

        UserDefaults.standard.set(displayModeRaw, forKey: "quotabar.displayMode")
        UserDefaults.standard.set(activePlatformRaw, forKey: "quotabar.activePlatform")
        UserDefaults.standard.set(refreshIntervalRaw, forKey: "quotabar.refreshInterval")
    }

    /// 清理已从 PlatformType 删除的平台的残留 UserDefaults 配置 (minimax_en / glm_en / kimi).
    /// 这些 key 是历史版本写入的, enum 里已无对应 case, 留着是无害的死数据, 顺手清掉.
    private func cleanupLegacyPlatformKeys() {
        for legacy in ["minimax_en", "glm_en", "kimi"] {
            let prefix = "quotabar.platform.\(legacy)"
            UserDefaults.standard.removeObject(forKey: prefix)
            UserDefaults.standard.removeObject(forKey: "\(prefix).enabled")
            UserDefaults.standard.removeObject(forKey: "\(prefix).pinned")
        }
    }

    // MARK: - Enabled Metrics

    /// 每个平台用户勾选要在菜单栏显示的 metric label 列表. 顺序即显示顺序.
    /// getter 优先读 UserDefaults, 无值时返回平台默认值 (首次安装 / 老用户升级).
    func enabledMetrics(for platform: PlatformType) -> [String] {
        configLock.lock()
        defer { configLock.unlock() }
        let key = "quotabar.platform.\(platform.rawValue).enabledMetrics"
        if let raw = UserDefaults.standard.string(forKey: key),
           let data = raw.data(using: .utf8),
           let labels = try? JSONDecoder().decode([String].self, from: data),
           !labels.isEmpty, labels.count <= 2 {
            return labels
        }
        return Self.defaultEnabledMetrics(for: platform)
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

    /// 设置平台启用的 metric label 列表. 拒绝空数组 (保留旧值) 和长度 > 2 的数组.
    /// 写入成功时发 `.enabledMetricsChanged` 通知.
    func setEnabledMetrics(_ labels: [String], for platform: PlatformType) {
        guard !labels.isEmpty, labels.count <= 2 else { return }

        configLock.lock()
        let key = "quotabar.platform.\(platform.rawValue).enabledMetrics"
        let encoded = (try? JSONEncoder().encode(labels)).flatMap { String(data: $0, encoding: .utf8) }
        configLock.unlock()

        guard let encoded else { return }
        UserDefaults.standard.set(encoded, forKey: key)
        NotificationCenter.default.post(name: .enabledMetricsChanged, object: platform)
    }
}

extension Notification.Name {
    static let enabledMetricsChanged = Notification.Name("enabledMetricsChanged")
}
