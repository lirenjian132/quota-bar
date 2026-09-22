import Foundation
import SwiftUI
import Combine

@MainActor
protocol PlatformViewModelDelegate: AnyObject {
    func platformViewModel(_ viewModel: PlatformViewModel, didUpdateData data: PlatformUsageData?)
    func platformViewModel(_ viewModel: PlatformViewModel, didSwitchInstance instance: PlatformInstance)
    // 全量数据更新 (所有实例). 默认空实现, 向后兼容.
    func platformViewModel(_ viewModel: PlatformViewModel, didUpdateAllData allData: [String: PlatformUsageData])
}

extension PlatformViewModelDelegate {
    func platformViewModel(_ viewModel: PlatformViewModel, didUpdateAllData allData: [String: PlatformUsageData]) {}
}

@MainActor
final class PlatformViewModel: ObservableObject {
    @Published var platformData: [String: PlatformUsageData] = [:]
    @Published var platformErrors: [String: PlatformError] = [:]
    @Published var isLoading: [String: Bool] = [:]
    @Published var activeInstance: PlatformInstance
    @Published var showingConfig: Bool = false
    @Published var configInstance: PlatformInstance?
    @Published var apiKeyInput: String = ""
    @Published var regionInput: String = "domestic"
    @Published var showingAPIKey: Bool = false
    /// 刚点「添加账号」尚未保存 key 的实例 id; 取消配置时自动回收.
    @Published private(set) var pendingNewInstanceID: String?

    weak var delegate: PlatformViewModelDelegate?

    private var timer: Timer?
    private var fetchTask: Task<Void, Never>?
    /// 每实例在途的 fetchUsage 任务. 换 key / 手动刷新 / 删除实例 / 全量刷新时
    /// 取消在途任务, 避免旧响应完成后把旧账号数据写回来.
    /// internal (非 private): 单测要钉住"删除/全量刷新时字典项被取消"这条回归.
    var fetchTasks: [String: Task<Void, Never>] = [:]
    private let platformManager: PlatformManager
    private let configService: ConfigService
    private let instanceStore: PlatformInstanceStore
    /// 登录窗工厂: 生产默认建真窗 (LoginWindowCoordinator), 测试注入 fake.
    typealias LoginWindowFactory = @MainActor (PlatformInstance) -> (any LoginWindowControlling)?
    private let loginWindowFactory: LoginWindowFactory
    /// 持有在途的登录窗防 GC (NSWindowController 没了引用窗口就关); 回调完成后置 nil.
    private var loginWindow: (any LoginWindowControlling)?

    init(platformManager: PlatformManager = .shared, configService: ConfigService = .shared, instanceStore: PlatformInstanceStore = .shared, loginWindowFactory: @escaping LoginWindowFactory = { instance in
        guard let controller = LoginWindowCoordinator.makeController(for: instance.platformType) else { return nil }
        return controller
    }) {
        self.platformManager = platformManager
        self.configService = configService
        self.instanceStore = instanceStore
        self.activeInstance = configService.activeInstance
        self.loginWindowFactory = loginWindowFactory

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(onPlatformEnabledChanged),
            name: .platformEnabledChanged,
            object: nil
        )

        // 实例被删除时清掉它的数据/错误/加载状态, 避免残留.
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(onPlatformInstanceRemoved(_:)),
            name: .platformInstanceRemoved,
            object: nil
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    // MARK: - Observers

    @objc private func onPlatformInstanceRemoved(_ note: Notification) {
        guard let id = note.object as? String else { return }
        platformData.removeValue(forKey: id)
        platformErrors.removeValue(forKey: id)
        isLoading.removeValue(forKey: id)
        // 删除实例时把它在途的 fetch 一并取消: 不取消的话, 最长 300s 后旧账号的
        // 响应到达, platformData 会被写回 — "已删除的账号"在状态栏复活 (R3-1).
        fetchTasks.removeValue(forKey: id)?.cancel()
        // 若删的是当前激活实例, 切到第一个可用实例 (enabled observer 会再校验一次)
        if activeInstance.id == id {
            if let first = configService.allEnabledInstances.first {
                switchActiveInstance(first)
            }
        }
    }

    @objc private func onPlatformEnabledChanged() {
        // When instance enabled state changes, ensure active instance is still valid
        let enabledInstances = configService.allEnabledInstances

        if !activeInstance.isEnabled {
            // Current active instance was disabled, switch to first enabled instance
            if let firstEnabled = enabledInstances.first {
                switchActiveInstance(firstEnabled)
            }
        } else if !enabledInstances.contains(where: { $0.id == activeInstance.id }) {
            // Active instance not in enabled list, switch to first enabled
            if let firstEnabled = enabledInstances.first {
                switchActiveInstance(firstEnabled)
            }
        }
        // If newly enabled instance is not the active one, switch to it
        // This handles the case where user enables a new instance via checkbox
        objectWillChange.send()
    }

    // MARK: - Auto Refresh

    func startAutoRefresh() {
        stopAutoRefresh()
        let interval = configService.refreshInterval.timeInterval
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.fetchAllUsage()
            }
        }
        Task {
            await fetchAllUsage()
        }
    }

    func stopAutoRefresh() {
        timer?.invalidate()
        timer = nil
    }

    func restartAutoRefresh() {
        startAutoRefresh()
    }

    // MARK: - Fetch

    func fetchAllUsage() async {
        fetchTask?.cancel()
        // 全量刷新替代局部刷新: 取消所有 per-instance 在途任务, 防止局部 fetch
        // 的旧响应覆写全量结果 (竞态方向: fetchUsage 在途 → fetchAllUsage 启动).
        fetchTasks.values.forEach { $0.cancel() }
        fetchTask = Task {
            // 只标记"实际会请求"的实例 (已启用且已配置). 已配置但禁用的实例被
            // PlatformManager 跳过不发请求, 标了 loading 也没人写回清除 — 永久
            // 转圈 (A4-4). 名单即写回循环的目标集, 取消时也靠它清场.
            let targets = platformManager.configuredInstances().filter(\.isEnabled)
            for instance in targets {
                isLoading[instance.id] = true
            }

            let results = await platformManager.fetchAllUsage()

            // 被新的 fetchAllUsage / saveAPIKey 换 key 取消时丢弃结果, 避免覆盖
            // 更新的数据 (A4-1, 竞态方向: fetchAllUsage 在途 → saveAPIKey 启动).
            // 旧 key 的全量结果若落地, 会把刚换的新 key 数据 (如 999) 覆写回旧值
            // (100), 且旧值还会随响应写进 service 缓存 (生产环境请求随任务取消
            // 中止, 到不了 cache.write). 取消也清 loading, 否则本轮标记的实例
            // 永久转圈 (A4-4).
            if Task.isCancelled {
                for instance in targets {
                    isLoading[instance.id] = false
                }
                return
            }

            for (instanceID, result) in results {
                // 逐项检查取消 (A4-1): 不含循环外一次的总检查兜不住"循环中途又来
                // 一个新全量/换 key"的窗口 — 对每项写 platformData 前都查一次.
                if Task.isCancelled { break }
                // 实例在途期间可能被删除 (用户从菜单删账号): store 里已不存在时
                // 丢弃结果, 防止"已删除的账号"数据复活 (R3-1, 与 onPlatformInstanceRemoved
                // 的取消在途任务互补 — 那条覆盖 fetchUsage 路径, 这条覆盖本路径).
                guard instanceStore.instance(id: instanceID) != nil else { continue }
                switch result {
                case .success(let data):
                    platformData[instanceID] = data
                    platformErrors[instanceID] = nil
                case .failure(let error):
                    let errorPlatform = instanceStore.instance(id: instanceID)?.platformType ?? activeInstance.platformType
                    if let platformError = error as? PlatformError {
                        platformErrors[instanceID] = platformError
                    } else {
                        platformErrors[instanceID] = .networkError(errorPlatform, error.localizedDescription)
                    }
                }
                isLoading[instanceID] = false
            }

            // Notify delegate for active instance + 全量数据 (钉选多实例状态栏需要)
            delegate?.platformViewModel(self, didUpdateData: platformData[activeInstance.id])
            delegate?.platformViewModel(self, didUpdateAllData: platformData)
        }
    }

    func fetchUsage(for instance: PlatformInstance) {
        platformErrors[instance.id] = nil

        // 取消该实例在途的旧 fetch (典型: saveAPIKey 换 key 后立刻重新拉取):
        // 旧请求用的是旧 key, 不清缓存 + 不取消的话, 旧响应仍会写回数据,
        // 最长 300s (service 缓存窗口) 显示的是旧账号.
        fetchTasks[instance.id]?.cancel()

        isLoading[instance.id] = true

        // 任务完成后不必清字典: 对已结束的 Task 再 cancel 是 no-op,
        // 字典规模以实例数为上界. (若清字典需防"旧任务尾段清掉新任务引用"的竞态, 不值当)
        fetchTasks[instance.id] = Task {
            // 退出即清 loading (A4-4): 旧代码在 do/catch 的 Task.isCancelled 分支
            // 提前 return, 尾段又有一道 isCancelled 守卫 — 取消路径两条出口都不清,
            // flag 永久残留 true, 弹窗一直转圈. defer 覆盖成功/失败/取消所有路径.
            // 已知取舍: 被新 fetch 取代的旧任务尾段可能清掉新任务的 flag (新任务
            // 完成时最终仍置 false, 只多一次转圈闪烁) — 换 permanent spinner 值得.
            defer { isLoading[instance.id] = false }
            do {
                let data = try await platformManager.fetchUsage(for: instance)
                // 被同实例的新 fetch 取消时丢弃结果, 避免旧数据盖新数据
                // (与 fetchAllUsage 的既有模式一致; PlatformManager 内的请求不会被
                //  取消中断, 但结果不再写回).
                if Task.isCancelled { return }
                platformData[instance.id] = data
                platformErrors[instance.id] = nil

                if instance.id == activeInstance.id {
                    delegate?.platformViewModel(self, didUpdateData: data)
                }
                delegate?.platformViewModel(self, didUpdateAllData: platformData)
            } catch {
                if Task.isCancelled { return }
                if let platformError = error as? PlatformError {
                    platformErrors[instance.id] = platformError
                } else {
                    platformErrors[instance.id] = .networkError(instance.platformType, error.localizedDescription)
                }
            }
        }
    }

    // MARK: - Instance Switching

    func switchActiveInstance(_ instance: PlatformInstance) {
        activeInstance = instance
        configService.activeInstance = instance
        delegate?.platformViewModel(self, didSwitchInstance: instance)
        delegate?.platformViewModel(self, didUpdateData: platformData[instance.id])
    }

    // MARK: - Instance Management

    /// 添加新账号实例: 创建即启用, 切为激活并打开配置面板.
    /// 记录 pendingNewInstanceID — 用户取消配置且从未填过 key 时自动回收实例, 不留幽灵账号.
    @discardableResult
    func addInstance(of type: PlatformType) -> PlatformInstance {
        var instance = PlatformInstanceStore.shared.addInstance(of: type)
        instance.isEnabled = true
        pendingNewInstanceID = instance.id
        switchActiveInstance(instance)
        configureAPIKey(for: instance)
        // 菜单/钉选栏/启用列表刷新
        NotificationCenter.default.post(name: .platformEnabledChanged, object: nil)
        return instance
    }

    /// 重命名账号 (菜单/弹窗里的显示名). 调用方负责弹输入框.
    func renameInstance(_ instance: PlatformInstance, to name: String) {
        PlatformInstanceStore.shared.renameInstance(id: instance.id, to: name)
        objectWillChange.send()
    }

    /// 删除账号实例 (连带 Keychain key 与各处缓存, 由 store 发通知联动清理).
    func removeInstance(_ instance: PlatformInstance) {
        if pendingNewInstanceID == instance.id { pendingNewInstanceID = nil }
        PlatformInstanceStore.shared.removeInstance(id: instance.id)
    }

    // MARK: - Config

    func configureAPIKey(for instance: PlatformInstance) {
        configInstance = instance
        let store = configService.store(for: instance)
        apiKeyInput = store.isConfigured ? (store.apiKey ?? "") : ""
        regionInput = store.region
        showingAPIKey = false
        showingConfig = true
    }

    func saveAPIKey() {
        guard let instance = configInstance else { return }
        let store = configService.store(for: instance)

        let trimmedKey = apiKeyInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedKey.isEmpty else { return }
        store.setAPIKey(trimmedKey)
        store.setRegion(regionInput)
        // 反向竞态 (A4-1, DeepSeek 探针 F1/F2): fetchAllUsage 在途 (旧 key) 时换 key,
        // 旧 key 的全量结果后到会把新 key 数据覆写回旧账号, 旧值还会写进 service
        // 缓存持续显示. 先取消全量任务 (fetchUsage 内的 per-instance 取消只覆盖
        // "局部在途→全量启动"方向), 再清缓存 + 起局部 fetch.
        fetchTask?.cancel()
        // 换 key 后 service 内的 usage 缓存还是旧账号数据 (缓存窗口 300s):
        // 不清则接下来 5 分钟内拉取到的仍是旧 key 的结果. 新 key 要让随后的
        // fetchUsage 真正发请求 — 新 key 无效时应立刻报错而不是显示旧账号的余量.
        platformManager.clearCache(for: instance)

        showingConfig = false
        configInstance = nil
        pendingNewInstanceID = nil
        apiKeyInput = ""
        regionInput = "domestic"

        fetchUsage(for: instance)
    }

    func cancelConfig() {
        showingConfig = false
        // 新建实例一路取消且从未填 key → 回收, 避免菜单里堆积没配置的幽灵账号
        if let pendingID = pendingNewInstanceID,
           let pending = configInstance, pending.id == pendingID,
           !configService.store(for: pending).isConfigured {
            PlatformInstanceStore.shared.removeInstance(id: pendingID)
        }
        pendingNewInstanceID = nil
        configInstance = nil
        apiKeyInput = ""
        regionInput = "domestic"
        showingAPIKey = false
    }

    // MARK: - Session Renewal

    /// 一键续期: 弹内嵌登录窗让用户重新登录官网, app 自动提取 cookie 写入凭据存储.
    /// 非 cookie 型平台 (MiniMax/GLM — API key 鉴权, 没有会话可续) no-op.
    func renewSession(for instance: PlatformInstance) {
        guard WebLoginRenewalConfig.platform(for: instance.platformType) != nil else { return }
        // 防双窗 (R-4): 已有在途登录窗时 no-op — 连点菜单/按钮只弹一扇.
        // 窗口由 onComplete/onCancel 置 nil 后释放, 那之后才允许再次唤起.
        guard loginWindow == nil else { return }
        guard let controller = loginWindowFactory(instance) else { return }
        controller.onComplete = { [weak self] credential in
            guard let self else { return }
            self.loginWindow = nil
            self.applyRenewedCredential(credential, for: instance)
        }
        controller.onCancel = { [weak self] in
            self?.loginWindow = nil
        }
        // 持有引用防 GC: NSWindowController 没了引用窗口就关.
        loginWindow = controller
        controller.present()
    }

    /// 续期成功回调: 写凭据 + 清 service usage 缓存 + 局部重新拉取.
    /// 与 saveAPIKey 的写入侧效果对齐 (清缓存否则 300s 窗口内刷新看到的还是旧
    /// 账号数据), 但不碰配置面板 UI 状态 — 续期不经配置面板.
    private func applyRenewedCredential(_ credential: String, for instance: PlatformInstance) {
        guard let platform = WebLoginRenewalConfig.platform(for: instance.platformType) else { return }
        let store = configService.store(for: instance)
        store.setAPIKey(WebLoginRenewalConfig.storageCredential(from: credential, platform: platform))
        platformManager.clearCache(for: instance)
        fetchUsage(for: instance)
    }

    // MARK: - Computed

    /// 错误区是否显示「重新登录」按钮 (R-5).
    ///
    /// 两个条件同时满足才显示:
    ///   1. cookie 型平台 (API key 型平台没有会话可续);
    ///   2. 当前错误是 unauthorized (会话过期) — 唯一"重新登录能治好"的错误.
    /// 网络错误 / 业务错误显示续期按钮是误导: 重登解决不了, 用户点了更懵.
    func showsRenewSessionButton(for instance: PlatformInstance) -> Bool {
        guard WebLoginRenewalConfig.platform(for: instance.platformType) != nil else { return false }
        guard case .unauthorized = platformErrors[instance.id] else { return false }
        return true
    }

    var activePlatformData: PlatformUsageData? {
        platformData[activeInstance.id]
    }

    var activePlatformError: PlatformError? {
        platformErrors[activeInstance.id]
    }

    var isActivePlatformLoading: Bool {
        isLoading[activeInstance.id] ?? false
    }

    var allConfiguredInstances: [PlatformInstance] {
        platformManager.configuredInstances()
    }

    var allInstances: [PlatformInstance] {
        configService.allEnabledInstances
    }

    func isConfigured(_ instance: PlatformInstance) -> Bool {
        configService.store(for: instance).isConfigured
    }

    func instanceDisplayName(_ instance: PlatformInstance) -> String {
        instance.displayTitle
    }

    // MARK: - Cleanup

    func cleanup() {
        fetchTask?.cancel()
        fetchTasks.values.forEach { $0.cancel() }
        stopAutoRefresh()
    }
}
