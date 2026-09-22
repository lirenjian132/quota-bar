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

    /// 只清已删除平台的残留 UserDefaults 配置 (含老版本明文 api_key).
    /// 这些 key 是历史版本写入的, enum 里已无对应 case, 留着是无害的死数据, 顺手清掉.
    ///
    /// 只清已删除平台的残留; 现役平台即使曾在这个列表里也不在此清 (stepfun 曾被删过
    /// 又加回): 老用户的老配置由 PlatformInstanceStore.migrateLegacyPerTypeConfig
    /// 接管 (生成默认禁用实例 + 搬 key). ConfigService.init 的清理早于 instanceStore
    /// 的迁移 (AppDelegate 先建 PlatformViewModel → 先触达 ConfigService.shared),
    /// 先清会让 2.0.x 直升级用户的老 stepfun 配置搬不到.
    /// 注意只清 quotabar.platform.* 前缀, 不影响当前实例 quotabar.instance.* 前缀的配置.
    /// internal (非 private): 单测要钉住"现役平台不在清理列表"这条回归 (P1-5).
    static let cleanedLegacyPlatforms = ["minimax_en", "glm_en", "kimi", "deepseek", "mimo"]

    private func cleanupLegacyPlatformKeys() {
        for legacy in Self.cleanedLegacyPlatforms {
            let prefix = "quotabar.platform.\(legacy)"
            defaults.removeObject(forKey: prefix)
            defaults.removeObject(forKey: "\(prefix).enabled")
            defaults.removeObject(forKey: "\(prefix).pinned")
            defaults.removeObject(forKey: "\(prefix).enabledMetrics")
        }
    }

    // MARK: - Enabled Metrics

    /// 右键菜单「显示指标」里每个平台可勾选的 metric label (A4-3: 从 StatusBarController 上移).
    /// 清单必须与该平台 service 实际产出的 label 对齐 (R3-2): "服务可产出但不可勾"
    /// 的项会被 visibleMetrics 过滤掉, 用户状态栏恒显 "--". 同族匹配见
    /// StatusBarViewHelper (周额度三态在族内互相替代).
    /// 勾选数量上限 (2) 由菜单构造处的 atLimit 逻辑控制, 这里只列全集.
    /// static 纯查表: 单测无需 MainActor 即可验证 (P0-1 回归钉住).
    static func availableMetricLabels(for type: PlatformType) -> [String] {
        switch type {
        case .minimax_cn:
            // MiniMax: 5 小时窗口 + 周限额三态 (标准 / 加成 / 无限). 不产 mcp_monthly.
            return ["five_hour", "weekly_limit", "weekly_limit_boosted", "weekly_limit_unlimited"]
        case .glm_cn:
            // GLM: 5 小时 + 周限额 + MCP 月度次数. 无 boosted/unlimited 态.
            return ["five_hour", "weekly_limit", "mcp_monthly"]
        case .tokenrhythm:
            return ["balance"]
        case .stepfun:
            // credits 是 credit 套餐主指标 (defaultEnabledMetrics 只勾它);
            // 非 credit 套餐族降级产出 five_hour/weekly_limit (见 StepFunPlatformService),
            // 这里必须一并列出 — 否则降级用户状态栏恒显 "--" 且右键菜单无可选项.
            return ["credits", "five_hour", "weekly_limit"]
        }
    }

    /// 每个账号实例用户勾选要在菜单栏显示的 metric label 列表. 顺序即显示顺序.
    /// getter 优先读 UserDefaults, 无值时返回平台默认值 (首次安装 / 老用户升级).
    /// 死 label 过滤 (A4-3): 老版本落盘的 enabledMetrics 可能含已下架 label (如
    /// minimax 的 mcp_monthly), 不过滤会占满 2 个勾选名额、菜单其余项全灰、
    /// 且与产出永无交集被 prefix(2) 防呆掩盖, 用户无法清理. 全部 label 均已
    /// 下架时回退平台默认值.
    func enabledMetrics(for instance: PlatformInstance) -> [String] {
        configLock.lock()
        defer { configLock.unlock() }
        let key = "quotabar.instance.\(instance.id).enabledMetrics"
        if let raw = defaults.string(forKey: key),
           let data = raw.data(using: .utf8),
           let labels = try? JSONDecoder().decode([String].self, from: data),
           !labels.isEmpty, labels.count <= 2 {
            let allowed = Self.availableMetricLabels(for: instance.platformType)
            let filtered = labels.filter { allowed.contains($0) }
            if !filtered.isEmpty { return filtered }
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
        case .tokenrhythm:
            return ["balance"]
        case .stepfun:
            return ["credits"]
        }
    }

    /// 设置实例启用的 metric label 列表. 拒绝空数组 (保留旧值) 和长度 > 2 的数组.
    /// 写入成功时发 `.enabledMetricsChanged` 通知; 被拒时也发 (A4-6) — 监听方
    /// (菜单据此刷新勾选态/防幻觉勾选, 状态栏据此重绘) 要能感知"设置被拒".
    /// 死 label 过滤 (A4-3): 不在 availableMetricLabels 清单内的 label 静默丢弃,
    /// 过滤后为空的写入视为拒绝 (不落盘).
    func setEnabledMetrics(_ labels: [String], for instance: PlatformInstance) {
        // 原始名单先过空/上限检查 (R3-6 契约不变: 3 个 label 一律拒, 不因 sanitize 缩水).
        if rejectIfInvalid(labels, for: instance) { return }
        // 死 label 过滤 (A4-3): 不在 availableMetricLabels 清单内的 label 静默丢弃.
        let allowed = Self.availableMetricLabels(for: instance.platformType)
        let sanitized = labels.filter { allowed.contains($0) }
        // 过滤后为空 (全是死 label) 同样视为拒绝.
        if rejectIfInvalid(sanitized, for: instance) { return }

        configLock.lock()
        let key = "quotabar.instance.\(instance.id).enabledMetrics"
        let encoded = (try? JSONEncoder().encode(sanitized)).flatMap { String(data: $0, encoding: .utf8) }
        configLock.unlock()

        guard let encoded else { return }
        defaults.set(encoded, forKey: key)
        NotificationCenter.default.post(name: .enabledMetricsChanged, object: instance.id)
    }

    /// 拒写统一出口: 空 / 超上限 → 不落盘, 仅发通知让监听方感知 (A4-6).
    /// 返回 true 表示调用方应中断写入 (保持旧值).
    @discardableResult
    private func rejectIfInvalid(_ labels: [String], for instance: PlatformInstance) -> Bool {
        guard labels.isEmpty || labels.count > 2 else { return false }
        NotificationCenter.default.post(name: .enabledMetricsChanged, object: instance.id)
        return true
    }
}

extension Notification.Name {
    static let enabledMetricsChanged = Notification.Name("enabledMetricsChanged")
}
