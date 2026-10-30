import Foundation

extension Notification.Name {
    static let platformEnabledChanged = Notification.Name("platformEnabledChanged")
}

/// 在途刷新任务的 class 盒: Task 是 struct 无身份, 包一层才能用 `===`
/// 确认"字典里这一项仍是我注册的那一个" (见 PlatformManager.inFlightRefreshes).
private final class InFlightRefreshBox {
    let task: Task<String?, Never>
    init(_ task: Task<String?, Never>) {
        self.task = task
    }
}

final class PlatformManager {
    static let shared = PlatformManager()

    // 每个 instance id 一个独立 service 对象: service 内部的 usage 缓存因此按账号隔离,
    // 两个 MiniMax 账号不会互相命中对方的 10 秒缓存.
    private var services: [String: PlatformAPIService] = [:]
    // StepFun 预刷新单飞门控 (P1-1): instance id → (凭据 → 在途刷新 Task).
    // 同实例同一凭据已有刷新在途时 await 复用它其结果, 不重复发 RefreshToken POST
    // — saveAPIKey 后的新 fetch 与 fetchAllUsage 在途可对同一账号叠加.
    // 键带凭据维度: 只复用"刷的就是我这个凭据"的在途任务; 用户在途期间换了新
    // 凭据 (新 fetch 读到的串与在途刷新用的不同) 时必须独立刷新, 否则复用结果
    // 会把用户新凭据覆盖成旧凭据刷出的串 (P0-1 的同族变种).
    // 与 services 共用 serviceLock: 各字典生命周期一致 (实例删除时一起清).
    // Task 是 struct 无身份可比, 用 class 盒包装: 任务结束时摘字典项要能确认
    // "摘的还是自己注册的那一个" (期间可能已被 handleInstanceRemoved 清空并重注册).
    private var inFlightRefreshes: [String: [String: InFlightRefreshBox]] = [:]
    private let serviceLock = NSLock()
    // 刷新失败冷却 (每实例): refresh token 失效后不再每 60s 白打一次 RefreshToken
    private var refreshCooldownUntil: [String: Date] = [:]
    // 冷却世代:用户保存新凭据时递增,在途刷新失败不再重新武装冷却
    // (否则 t0 刷新 → t1 用户换 key → t2 刷新失败,刚存的凭据又被冷却 5 分钟)
    private var refreshCooldownGeneration: [String: Int] = [:]
    private static let refreshCooldownInterval: TimeInterval = 300
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
             case .tokenrhythm: return TokenRhythmPlatformAPIService()
             case .stepfun: return StepFunPlatformAPIService()
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
        // 刷新在途的 Task 不禁掉 (非结构化, 停不下来); 摘掉字典项即可 —
        // 结果仍会被 await 它的调用方正常消费后丢弃.
        inFlightRefreshes.removeValue(forKey: id)
        refreshCooldownUntil.removeValue(forKey: id)
        refreshCooldownGeneration.removeValue(forKey: id)
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
        // StepFun 会话预刷新: access token 距过期 < 300s 时先换新凭据再发主请求
        // (纯本地解析 JWT exp 判断, 不发探测请求). 刷新失败不阻断 — 主请求照常
        // 发出, 旧凭据若已失效走既有 401 错误链 (提示重新登录粘贴).
        // setAPIKey 同步落 keychain (FileKeyStore), 随后 toConfigData 拿到新凭据,
        // 本次主请求与后续刷新周期都用它.
        // 失败冷却: refresh token 失效时, 没有冷却的话每个刷新周期 (默认 60s)
        // 都会白打一次 RefreshToken POST, 直到用户重登 —— 对平台接口不友好.
        // 失败冷却期内不再尝试刷新 (冷却按实例记, 与单飞门控共用一把锁)
        if instance.platformType == .stepfun,
           !refreshOnCooldown(instanceID: instance.id),
           let credential = store.apiKey,
           let parsed = StepFunSessionRefresher.parse(credential),
           StepFunSessionRefresher.needsRefresh(parsed) {
            let generation = refreshGeneration(instanceID: instance.id)
            if let fresh = await refreshStepFunSession(credential, parsed: parsed, instanceID: instance.id) {
                // CAS 写回 (P0-1): 比较与写入在 store 的写锁内原子完成.
                // 用户在刷新在途期间保存了新 key (主线程 saveAPIKey / renewSession),
                // 刷新此刻才完成 → CAS 不匹配 → 放弃写回, 别人的更新优先;
                // 否则用户的新凭据会被旧凭据刷出的串覆盖 (表现为"改完 key 自己变回去").
                store.casAPIKey(expected: credential, fresh: fresh)
            } else {
                markRefreshCooldown(instanceID: instance.id, generation: generation)
            }
        }
        return try await service(for: instance).fetchUsage(config: store.toConfigData(), network: networkService)
    }

    /// 刷新失败冷却: 失败后 5 分钟内不再发 RefreshToken, 到点自动恢复尝试
    private func refreshOnCooldown(instanceID: String) -> Bool {        serviceLock.lock()
        defer { serviceLock.unlock() }
        guard let until = refreshCooldownUntil[instanceID] else { return false }
        if Date() >= until {
            refreshCooldownUntil.removeValue(forKey: instanceID)
            return false
        }
        return true
    }

    private func refreshGeneration(instanceID: String) -> Int {
        serviceLock.lock()
        defer { serviceLock.unlock() }
        return refreshCooldownGeneration[instanceID, default: 0]
    }

    /// 只对"发起刷新时那个世代"武装冷却:期间用户保存过新凭据(世代已变)
    /// 则不武装,避免刚换的 key 被在途失败拖进 5 分钟冷却
    private func markRefreshCooldown(instanceID: String, generation: Int) {
        serviceLock.lock()
        defer { serviceLock.unlock() }
        guard refreshCooldownGeneration[instanceID, default: 0] == generation else { return }
        refreshCooldownUntil[instanceID] = Date().addingTimeInterval(Self.refreshCooldownInterval)
    }

    /// 用户保存/续期了新凭据:解除该实例的刷新失败冷却.
    /// 不清的话:刚重登成功 (access token 剩余往往已 <300s) 会在冷却期内跳过
    /// 预刷新, token 一过期主请求就 401, 用户被引导再重登一次.
    func clearRefreshCooldown(instanceID: String) {
        serviceLock.lock()
        refreshCooldownUntil.removeValue(forKey: instanceID)
        refreshCooldownGeneration[instanceID, default: 0] += 1
        serviceLock.unlock()
    }

    /// StepFun 预刷新 + 单飞门控 (P1-1). 同实例同一凭据已有在途刷新 Task 则
    /// await 复用其复用结果, 不新发 POST (预刷新原本无门控, saveAPIKey 新 fetch
    /// 与 fetchAllUsage 在途可对同实例双发). 任务结束清字典项.
    ///
    /// 在途 Task 是非结构化 Task: 不随调用方 (fetchAllUsage / fetchUsage) 取消
    /// 而取消 — 调用方取消只是丢弃等不到的结果, POST 照常完成.
    private func refreshStepFunSession(_ credential: String,
                                       parsed: (webid: String, accessExpiry: Date?),
                                       instanceID: String) async -> String? {
        serviceLock.lock()
        if let existing = inFlightRefreshes[instanceID]?[credential] {
            serviceLock.unlock()
            return await existing.task.value
        }
        // 捕获 network 后立刻放锁: 锁内不起 await.
        let network = networkService
        let task = Task<String?, Never> {
            await StepFunSessionRefresher(network: network).refresh(credential, parsed: parsed)
        }
        let box = InFlightRefreshBox(task)
        inFlightRefreshes[instanceID, default: [String: InFlightRefreshBox]()][credential] = box
        serviceLock.unlock()

        let result = await task.value

        serviceLock.lock()
        // 只摘自己那一项: 若期间实例被删 (handleInstanceRemoved 已清) 或
        // 新任务已注册, 不能误删别人的.
        if inFlightRefreshes[instanceID]?[credential] === box {
            inFlightRefreshes[instanceID]?[credential] = nil
            if inFlightRefreshes[instanceID]?.isEmpty == true {
                inFlightRefreshes[instanceID] = nil
            }
        }
        serviceLock.unlock()
        return result
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
