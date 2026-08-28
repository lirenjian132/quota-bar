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
    private let platformManager: PlatformManager
    private let configService: ConfigService
    private let instanceStore: PlatformInstanceStore

    init(platformManager: PlatformManager = .shared, configService: ConfigService = .shared, instanceStore: PlatformInstanceStore = .shared) {
        self.platformManager = platformManager
        self.configService = configService
        self.instanceStore = instanceStore
        self.activeInstance = configService.activeInstance

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
        fetchTask = Task {
            // 先标记所有已配置实例为加载中
            for instance in platformManager.configuredInstances() {
                isLoading[instance.id] = true
            }

            let results = await platformManager.fetchAllUsage()

            // 被新的 fetchAllUsage 取消时丢弃结果, 避免覆盖更新的数据
            // (PlatformManager 的 TaskGroup 不检查 cancellation, 网络请求会跑完,
            //  但结果不再写回, 防止定时刷新和手动刷新撞车时旧数据盖新数据)
            if Task.isCancelled { return }

            for (instanceID, result) in results {
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
        isLoading[instance.id] = true
        platformErrors[instance.id] = nil

        Task {
            do {
                let data = try await platformManager.fetchUsage(for: instance)
                platformData[instance.id] = data
                platformErrors[instance.id] = nil

                if instance.id == activeInstance.id {
                    delegate?.platformViewModel(self, didUpdateData: data)
                }
                delegate?.platformViewModel(self, didUpdateAllData: platformData)
            } catch {
                if let platformError = error as? PlatformError {
                    platformErrors[instance.id] = platformError
                } else {
                    platformErrors[instance.id] = .networkError(instance.platformType, error.localizedDescription)
                }
            }
            isLoading[instance.id] = false
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

    // MARK: - Computed

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
        stopAutoRefresh()
    }
}
