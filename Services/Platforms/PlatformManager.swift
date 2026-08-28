import Foundation

extension Notification.Name {
    static let platformEnabledChanged = Notification.Name("platformEnabledChanged")
}

final class PlatformManager {
    static let shared = PlatformManager()

    // 每个 instance id 一个独立 service 对象: service 内部的 usage 缓存因此按账号隔离,
    // 两个 MiniMax 账号不会互相命中对方的 10 秒缓存.
    private var services: [String: PlatformAPIService] = [:]
    private let serviceLock = NSLock()
    private let serviceFactory: (PlatformType) -> PlatformAPIService
    let networkService: NetworkService
    private let configService: ConfigService
    private let instanceStore: PlatformInstanceStore

    init(networkService: NetworkService = URLSessionNetworkService(),
         configService: ConfigService = .shared,
         instanceStore: PlatformInstanceStore = .shared,
         serviceFactory: @escaping (PlatformType) -> PlatformAPIService = { type in
             switch type {
             case .minimax_cn: return MiniMaxPlatformAPIService()
             case .glm_cn: return GLMPlatformAPIService()
             }
         }) {
        self.networkService = networkService
        self.configService = configService
        self.instanceStore = instanceStore
        self.serviceFactory = serviceFactory

        // 实例被删除时清掉它缓存的 service, 避免字典残留.
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
        serviceLock.lock()
        services.removeValue(forKey: id)
        serviceLock.unlock()
    }

    /// 按 instance 惰性创建并缓存 service (并发安全).
    private func service(for instance: PlatformInstance) -> PlatformAPIService {
        serviceLock.lock()
        defer { serviceLock.unlock() }
        if let existing = services[instance.id] {
            return existing
        }
        let service = serviceFactory(instance.platformType)
        services[instance.id] = service
        return service
    }

    func fetchUsage(for instance: PlatformInstance) async throws -> PlatformUsageData {
        let store = configService.store(for: instance)
        guard store.isConfigured else {
            throw PlatformError.notConfigured(instance.platformType)
        }
        return try await service(for: instance).fetchUsage(config: store.toConfigData(), network: networkService)
    }

    func fetchAllUsage() async -> [String: Result<PlatformUsageData, Error>] {
        var results: [String: Result<PlatformUsageData, Error>] = [:]

        await withTaskGroup(of: (String, Result<PlatformUsageData, Error>).self) { group in
            for instance in instanceStore.instances {
                // 只请求已启用且已配置的实例 (禁用的实例不浪费请求)
                guard instance.isEnabled else { continue }
                let store = configService.store(for: instance)
                guard store.isConfigured else { continue }

                let instanceID = instance.id
                group.addTask { [weak self] in
                    do {
                        let data = try await self?.fetchUsage(for: instance)
                        if let data {
                            return (instanceID, .success(data))
                        } else {
                            return (instanceID, .failure(PlatformError.notConfigured(instance.platformType)))
                        }
                    } catch {
                        return (instanceID, .failure(error))
                    }
                }
            }

            for await (instanceID, result) in group {
                results[instanceID] = result
            }
        }

        return results
    }

    func configuredInstances() -> [PlatformInstance] {
        configService.configuredInstances()
    }

    func clearCache(for instance: PlatformInstance) {
        serviceLock.lock()
        let service = services[instance.id]
        serviceLock.unlock()
        service?.clearCache()
    }

    func clearAllCaches() {
        serviceLock.lock()
        let all = Array(services.values)
        serviceLock.unlock()
        all.forEach { $0.clearCache() }
    }

    func setPlatformEnabled(_ enabled: Bool, for instance: PlatformInstance) {
        var instance = instance
        // Prevent disabling the last enabled instance
        if !enabled && instance.isEnabled && isLastEnabledInstance(instance) {
            return
        }

        instance.isEnabled = enabled
        NotificationCenter.default.post(name: .platformEnabledChanged, object: nil)
    }

    func isLastEnabledInstance(_ instance: PlatformInstance) -> Bool {
        instanceStore.instances.filter { $0.isEnabled }.count <= 1 && instance.isEnabled
    }
}
