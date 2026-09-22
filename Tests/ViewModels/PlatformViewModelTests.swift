import XCTest
@testable import QuotaBar

@MainActor
final class PlatformViewModelTests: XCTestCase {
    func testDefaultActiveInstance() {
        let viewModel = PlatformViewModel()
        let instance = viewModel.activeInstance
        XCTAssertTrue(PlatformInstanceStore.shared.instances.contains(where: { $0.id == instance.id }))
    }

    func testAllInstancesReturnsEnabledInstances() {
        let viewModel = PlatformViewModel()
        let enabledCount = PlatformInstanceStore.shared.instances.filter { $0.isEnabled }.count
        XCTAssertEqual(viewModel.allInstances.count, enabledCount)
    }

    /// platformNavigator (tab 行) 的数据源保序 + 完整性: 装机实测 9 实例场景
    /// (MiniMax×2 + GLM + TokenRhythm×5 + StepFun). tab 行从横向滚动改为自动
    /// 换行网格后, 每个账号都必须出现在数据源里且顺序与实例列表一致, 否则尾部
    /// tab (如被挤走的 T2~T5) 仍然点不到. 禁用的实例不出现在 tab 行.
    func testAllInstancesWithNineAccounts() {
        let viewModel = PlatformViewModel()
        var addedIDs: [String] = []
        var disabledID: String?
        defer {
            addedIDs.forEach { PlatformInstanceStore.shared.removeInstance(id: $0) }
            if let disabledID { PlatformInstanceStore.shared.removeInstance(id: disabledID) }
        }

        // 新实例默认禁用 (isEnabled 默认策略只认默认实例), 菜单新增路径会置启用 —
        // 这里对齐生产路径显式启用, 否则 tab 行根本不显示它们.
        for type in [PlatformType.minimax_cn, .minimax_cn, .glm_cn,
                     .tokenrhythm, .tokenrhythm, .tokenrhythm, .tokenrhythm, .tokenrhythm,
                     .stepfun] {
            var instance = PlatformInstanceStore.shared.addInstance(of: type)
            instance.isEnabled = true
            addedIDs.append(instance.id)
        }
        // 一个未启用的实例: 必须被 allInstances 排除 (不进 tab 行)
        disabledID = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm).id

        let expected = PlatformInstanceStore.shared.instances.filter(\.isEnabled)
        let allInstances = viewModel.allInstances

        XCTAssertEqual(allInstances.count, expected.count, "allInstances 应恰好覆盖全部启用实例")
        XCTAssertEqual(allInstances.map(\.id), expected.map(\.id), "tab 顺序应与实例列表顺序一致")
        XCTAssertTrue(Set(allInstances.map(\.id)).isSuperset(of: Set(addedIDs)),
                      "9 个启用实例必须全部出现在 tab 数据源里")
        XCTAssertFalse(allInstances.contains(where: { $0.id == disabledID }),
                       "禁用的实例不应出现在 tab 数据源里")
    }

    func testConfiguredInstancesReturnsArray() {
        let viewModel = PlatformViewModel()
        let instances = viewModel.allConfiguredInstances
        XCTAssertNotNil(instances)
    }

    func testIsConfiguredReturnsBool() {
        let viewModel = PlatformViewModel()
        for instance in PlatformInstanceStore.shared.instances {
            let _ = viewModel.isConfigured(instance)
        }
    }

    func testInstanceDisplayName() {
        let viewModel = PlatformViewModel()
        let minimax = PlatformInstance(id: "minimax_cn", platformType: .minimax_cn, displayName: "")
        let glm = PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: "")
        XCTAssertEqual(viewModel.instanceDisplayName(minimax), "MiniMax")
        XCTAssertEqual(viewModel.instanceDisplayName(glm), "GLM")
        // 自定义名优先
        let named = PlatformInstance(id: "x", platformType: .minimax_cn, displayName: "小号")
        XCTAssertEqual(viewModel.instanceDisplayName(named), "小号")
    }

    func testConfigureAPIKey() {
        let viewModel = PlatformViewModel()
        let glm = PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: "")
        viewModel.configureAPIKey(for: glm)
        XCTAssertTrue(viewModel.showingConfig)
        XCTAssertEqual(viewModel.configInstance?.id, "glm_cn")
    }

    func testCancelConfig() {
        let viewModel = PlatformViewModel()
        viewModel.configureAPIKey(for: PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: ""))
        viewModel.cancelConfig()
        XCTAssertFalse(viewModel.showingConfig)
        XCTAssertNil(viewModel.configInstance)
    }

    func testCleanupDoesNotCrash() {
        let viewModel = PlatformViewModel()
        viewModel.startAutoRefresh()
        viewModel.cleanup()
    }

    func testSwitchActiveInstance() {
        let viewModel = PlatformViewModel()
        viewModel.switchActiveInstance(PlatformInstance(id: "glm_cn", platformType: .glm_cn, displayName: ""))
        XCTAssertEqual(viewModel.activeInstance.id, "glm_cn")
    }

    /// 轮询等待 mock 请求数达标 (saveAPIKey 内部的 fetchUsage 是不等待的 Task).
    private func waitForRequestCount(_ count: Int, _ mock: MockNetworkService) async {
        for _ in 0..<100 where mock.requestCount < count {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    /// 轮询等待条件满足 (saveAPIKey 内部的 fetchUsage 是不等待的 Task).
    private func waitUntil(_ condition: @MainActor () -> Bool) async {
        for _ in 0..<250 where !condition() {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    func testSaveAPIKeyClearsServiceCache() async throws {
        // 换 key 后 service 内的 usage 缓存 (300s 窗口) 必须清掉, 否则接下来
        // 5 分钟内拉到的仍是旧 key 的旧账号数据.
        let mock = MockNetworkService()
        let manager = PlatformManager(networkService: mock, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        let instance = PlatformInstance(id: "vm-cache-test", platformType: .tokenrhythm, displayName: "")

        // TokenRhythm summary 有效响应 (template base URL, 单值模式两次请求都返回它).
        mock.mockData = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"253.76250848"}}
        """.data(using: .utf8)
        mock.mockResponse = MockNetworkService.makeResponse(
            url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        // 第一次存 key: summary + expiring-credits 尝试 = 2 个请求.
        viewModel.configureAPIKey(for: instance)
        viewModel.apiKeyInput = "sess_1"
        viewModel.saveAPIKey()
        await waitForRequestCount(2, mock)
        XCTAssertEqual(mock.requestCount, 2)

        // 换 key: clearCache 生效 → 新 key 真实发起请求 (不清则命中缓存, 停在第 2 个).
        viewModel.configureAPIKey(for: instance)
        viewModel.apiKeyInput = "sess_2"
        viewModel.saveAPIKey()
        await waitForRequestCount(4, mock)
        XCTAssertGreaterThanOrEqual(mock.requestCount, 4, "换 key 后应清 service 缓存并重新请求")
        XCTAssertEqual(mock.lastRequest?.value(forHTTPHeaderField: "Cookie"), "tr_session=sess_2",
                       "重新请求应使用新 key")
    }

    // MARK: - P1-2 saveAPIKey 竞态

    /// 挂起型网络 mock: 每个请求都挂起, 测试方显式 release 才返回.
    /// 用于复现"换 key 时旧 key 的请求仍在途"的竞态窗口.
    /// 取消语义对齐 URLSession (A4-1): 调用方任务被取消时, 在途请求立即以
    /// URLError(.cancelled) 失败并从 pending 摘除. 生产环境里任务取消会中止
    /// URLSession 请求 (旧响应不到, service 的 cache.write 不会执行); mock 不
    /// 模拟这一点就会造出"取消后旧响应仍能晚到"的假窗口, 钉错修复目标.
    private final class GatedNetworkService: NetworkService {
        /// 一个在途请求的挂起句柄. 类引用做身份: 取消/释放/重复注册都以
        /// 引用同一性摘除, 不依赖 URLRequest 的 Equatable.
        private final class PendingRequest {
            let request: URLRequest
            var continuation: CheckedContinuation<(Data, URLResponse), Error>?
            init(request: URLRequest) { self.request = request }
        }

        private let lock = NSLock()
        private var pending: [PendingRequest] = []
        private var count = 0
        private var captured: [URLRequest] = []

        var requestCount: Int { lock.lock(); defer { lock.unlock() }; return count }
        var pendingCount: Int { lock.lock(); defer { lock.unlock() }; return pending.count }
        var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return captured }

        func data(from request: URLRequest) async throws -> (Data, URLResponse) {
            lock.lock()
            count += 1
            captured.append(request)
            lock.unlock()
            // box 先建, 两个闭包登记/中止操作的是同一个句柄.
            let box = PendingRequest(request: request)
            return try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<(Data, URLResponse), Error>) in
                    self.lock.lock()
                    box.continuation = continuation
                    self.pending.append(box)
                    self.lock.unlock()
                    // 注册前任务已被取消 (onCancel 已跑完, 没人会来中止):
                    // 自己中止, 否则这个请求永远没人 resume.
                    if Task.isCancelled { self.abort(box) }
                }
            } onCancel: {
                self.abort(box)
            }
        }

        /// 以取消错误中止 box (幂等: continuation 取出即置空, 全局只会 resume 一次).
        private func abort(_ box: PendingRequest) {
            lock.lock()
            let continuation = box.continuation
            box.continuation = nil
            pending.removeAll { $0 === box }
            lock.unlock()
            continuation?.resume(throwing: URLError(.cancelled))
        }

        /// 是否还有满足 matches 的在途请求 (尚未 release / 未被取消中止).
        func hasPending(where matches: (URLRequest) -> Bool) -> Bool {
            lock.lock()
            defer { lock.unlock() }
            return pending.contains { matches($0.request) }
        }

        /// 恢复第一个满足 matches 的挂起请求, 响应由 responder 按请求内容决定.
        /// 不匹配 (含已被取消中止) 时无操作 (调用方循环重试).
        @discardableResult
        func release(where matches: (URLRequest) -> Bool, responder: (URLRequest) -> (Data, URLResponse)) -> Bool {
            lock.lock()
            guard let index = pending.firstIndex(where: { matches($0.request) }) else {
                lock.unlock()
                return false
            }
            let box = pending.remove(at: index)
            let continuation = box.continuation
            lock.unlock()
            continuation?.resume(returning: responder(box.request))
            return continuation != nil
        }
    }

    func testSaveAPIKeyInFlightRequestDoesNotWriteStaleAccount() async throws {
        // P1-2 (DeepSeek 探针确定性复现): 旧 key 的请求在途时换 key, 旧响应完成后
        // 不得回写 platformData — 否则最长 300s (service 缓存窗口) 显示旧账号.
        // 时序设计: 新账号响应先恢复, 旧账号响应后恢复 → 无修复时旧数据盖新数据.
        let gated = GatedNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        let instance = PlatformInstance(id: "vm-race-test", platformType: .tokenrhythm, displayName: "")

        func balanceJSON(_ value: String) -> String {
            """
            {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"\(value)"}}
            """
        }
        // 旧账号余额 100, 新账号 999 — 数值必须可区分.
        let responder: (URLRequest) -> (Data, URLResponse) = { request in
            let url = request.url!.absoluteString
            if url.contains("/wallet/expiring-credits") {
                // 赠金到期查询失败 → 降级无到期信息 (不影响余额).
                return ("{}".data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 500))
            }
            let json = request.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_old"
                ? balanceJSON("100.00000000")
                : balanceJSON("999.00000000")
            return (json.data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 200))
        }

        // 第一次存 key (旧账号): 请求挂在 mock 上, 模拟在途.
        viewModel.configureAPIKey(for: instance)
        viewModel.apiKeyInput = "sess_old"
        viewModel.saveAPIKey()
        await waitUntil { gated.requestCount == 1 }
        XCTAssertEqual(gated.requests.first?.value(forHTTPHeaderField: "Cookie"), "tr_session=sess_old")

        // 换 key (新账号): 取消在途旧任务 + clearCache + 立刻发新请求.
        viewModel.configureAPIKey(for: instance)
        viewModel.apiKeyInput = "sess_new"
        viewModel.saveAPIKey()
        await waitUntil { gated.requestCount == 2 }
        XCTAssertEqual(gated.requests.last?.value(forHTTPHeaderField: "Cookie"), "tr_session=sess_new")

        // 新账号响应先恢复 → platformData 写入 999.
        XCTAssertTrue(gated.release(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_new" }, responder: responder))
        await waitUntil { (viewModel.platformData[instance.id]?.metrics[0].currentValue ?? -1) == 999 }

        // 旧账号请求在 fetchUsage 取消在途任务时已被 mock 按 URLSession 语义
        // 中止 (任务取消 → 请求中止), 不存在"旧响应晚到覆盖"的窗口 (A4-1).
        // 没有取消逻辑时这里才会 resume 成功并写回 100 — 即回归拦截点.
        XCTAssertFalse(gated.hasPending(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_old" }),
                       "旧账号的在途请求应已被取消中止")

        // 新任务刷新完余额后会补发 expiring 请求, 循环释放直到全部 settle.
        for _ in 0..<50 where gated.pendingCount > 0 {
            gated.release(where: { _ in true }, responder: responder)
            try? await Task.sleep(nanoseconds: 20_000_000)
        }

        XCTAssertEqual(viewModel.platformData[instance.id]?.metrics[0].currentValue ?? -1, 999, accuracy: 0.001,
                       "旧账号的在途响应不得覆盖新账号数据")
        XCTAssertEqual(viewModel.platformData[instance.id]?.metrics[0].unit, "CNY")
        XCTAssertFalse(viewModel.isLoading[instance.id] ?? true, "任务结束后加载状态应清除")
    }

    // MARK: - A4-1 反向竞态: fetchAllUsage 在途 → saveAPIKey 换 key (DeepSeek F1/F2)

    func testSaveAPIKeyCancelsInFlightFullFetch() async throws {
        // F1/F2 (DeepSeek 探针确定性复现, Round 4): fetchAllUsage 在途 (旧 key) 时
        // 换 key. 旧代码只覆盖"局部在途→全量启动"方向, 反方向裸奔: 新 key 数据
        // 999 先落地, 全量旧 key 结果后到覆盖成 100, 且旧值写进 service 缓存
        // (300s 窗口) 持续显示. 修复: saveAPIKey 先取消 fetchTask (全量任务) +
        // fetchAllUsage 写回逐项检查取消.
        let gated = GatedNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        var instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "全量竞态测试")
        instance.isEnabled = true
        // 直接配 key (不经 saveAPIKey, 避免它顺手触发一次局部 fetchUsage).
        ConfigService.shared.store(for: instance).setAPIKey("sess_old")

        // 全量刷新在途: 旧 key 请求挂在 mock 上.
        let fullTask = Task { await viewModel.fetchAllUsage() }
        await waitUntil { gated.hasPending(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_old" }) }

        // 换 key: saveAPIKey 必须取消在途全量任务, 再 clearCache + 起局部 fetch.
        viewModel.configureAPIKey(for: instance)
        viewModel.apiKeyInput = "sess_new"
        viewModel.saveAPIKey()

        // 取消传播到 task group 子任务 → mock 按 URLSession 语义中止旧请求:
        // pending 里旧 key 请求消失 (生产同理 — 旧响应不到, 不进 platformData,
        // 也到不了 service 的 cache.write).
        await waitUntil { !gated.hasPending(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_old" }) }
        // 等待本身超时只是静默返回 (waitUntil 无断言), 必须显式钉住结果 (M1 补强):
        // 取消逻辑失效时上面这行等满 5s 也不为真, 由本断言兜住失败.
        XCTAssertFalse(gated.hasPending(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_old" }),
                       "旧 key 的在途请求应已被取消中止")
        // 新 key 的局部 fetch 已发起.
        await waitUntil { gated.hasPending(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_new" }) }

        // 旧/新 key 的响应必须数值可区分 (旧 100 / 新 999): 旧请求的响应若晚到
        // 覆写, 数值差异立刻暴露; 旧代码两边都回 999 会把覆写掩盖掉 (M1 补强).
        func balanceJSON(_ value: String) -> String {
            """
            {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"\(value)"}}
            """
        }
        let responder: (URLRequest) -> (Data, URLResponse) = { request in
            let url = request.url!.absoluteString
            if url.contains("/wallet/expiring-credits") {
                return ("{}".data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 500))
            }
            let balance = request.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_old"
                ? "100.00000000" : "999.00000000"
            return (balanceJSON(balance).data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 200))
        }

        // 新 key 数据先落地 (999): summary 完成后 service 补发 expiring 请求, 一并释放.
        XCTAssertTrue(gated.release(where: { $0.value(forHTTPHeaderField: "Cookie") == "tr_session=sess_new" }, responder: responder))
        await waitUntil { gated.hasPending(where: { $0.url!.absoluteString.contains("/wallet/expiring-credits") }) }
        XCTAssertTrue(gated.release(where: { $0.url!.absoluteString.contains("/wallet/expiring-credits") }, responder: responder))
        await waitUntil { (viewModel.platformData[instance.id]?.metrics[0].currentValue ?? -1) == 999 }

        // 反向竞态兜底: 即便旧请求仍残留在 pending (取消逻辑失效的变异态), 显式
        // 放行后旧 key 的响应 (100) 也不得覆写新 key 数据 (999) — 数值差异保证
        // "覆写"可被暴露, 而不是两边同值互相掩盖.
        for _ in 0..<50 where gated.pendingCount > 0 {
            gated.release(where: { _ in true }, responder: responder)
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        try? await Task.sleep(nanoseconds: 50_000_000)

        // 全量任务被取消: 结果丢弃, 不落地也不留错误态; loading 清场.
        await fullTask.value
        XCTAssertEqual(viewModel.platformData[instance.id]?.metrics[0].currentValue ?? -1, 999, accuracy: 0.001,
                       "全量任务的旧 key 结果不得覆盖换 key 后的新数据")
        XCTAssertNil(viewModel.platformErrors[instance.id], "被取消的全量任务不得留下错误态")
        XCTAssertFalse(viewModel.isLoading[instance.id] ?? true, "取消后 loading 应清除 (A4-4)")

        // 缓存不得留存旧 key 数据: 再发一次局部 fetch — 若 service 缓存被旧值 (100)
        // 占用, 命中缓存会直接写回 100; 修复后缓存里是新值 999 (旧请求已随取消
        // 中止, 没到 cache.write), 命中写回仍是 999.
        let requestCountBefore = gated.requestCount
        viewModel.fetchUsage(for: instance)
        await waitUntil { viewModel.isLoading[instance.id] == false }
        XCTAssertEqual(viewModel.platformData[instance.id]?.metrics[0].currentValue ?? -1, 999, accuracy: 0.001,
                       "service 缓存不得留存旧 key 数据 (否则下一轮读到旧账号)")
        XCTAssertEqual(gated.requestCount, requestCountBefore, "缓存命中不应再发请求")

        PlatformInstanceStore.shared.removeInstance(id: instance.id)
    }

    // MARK: - A4-4 isLoading 永久残留 (GLM 新发现 + DeepSeek F4 双确认)

    /// Task 体落定信号: fetchAllUsage 的 Task 尾部必回调 didUpdateAllData.
    /// markLoading 在该回调之前执行 — 等到回调即可证明"标记阶段已跑过".
    /// 没有这个信号, `await viewModel.fetchAllUsage()` 后立即断言会跑在标记前:
    /// 恢复"全量标记"变异时禁用实例尚未被标 true, 断言恒真 (M4).
    @MainActor
    private final class FetchCompletionRecorder: PlatformViewModelDelegate {
        private(set) var allDataCount = 0
        func platformViewModel(_ viewModel: PlatformViewModel, didUpdateData data: PlatformUsageData?) {}
        func platformViewModel(_ viewModel: PlatformViewModel, didSwitchInstance instance: PlatformInstance) {}
        func platformViewModel(_ viewModel: PlatformViewModel, didUpdateAllData allData: [String: PlatformUsageData]) {
            allDataCount += 1
        }
    }

    func testFetchAllUsageDoesNotMarkDisabledInstanceLoading() async throws {
        // A4-4①: 已配置但禁用的实例被 PlatformManager 跳过不发请求. 旧代码
        // markLoading 标全部已配置实例 → 禁用实例被标 loading 且从不写回清除
        // → 永久转圈.
        let mock = MockNetworkService()
        let manager = PlatformManager(networkService: mock, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        // 落定信号 (M4): 必须等 fetchAllUsage 的 Task 体跑过 markLoading 再断言.
        let settled = FetchCompletionRecorder()
        viewModel.delegate = settled
        var instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "禁用实例 loading")
        // 显式禁用 (新实例默认即禁用: id 非平台 rawValue 时 isDefaultEnabled 策略返回 false).
        instance.isEnabled = false
        ConfigService.shared.store(for: instance).setAPIKey("sess_disabled")

        await viewModel.fetchAllUsage()
        await waitUntil { settled.allDataCount >= 1 }
        XCTAssertEqual(settled.allDataCount, 1, "Task 体应已跑完 (否则 markLoading 尚未执行, 后续断言无效)")

        XCTAssertNil(viewModel.isLoading[instance.id], "禁用实例不得被标记 loading (它不会被请求, 没人清 flag)")
        XCTAssertNil(viewModel.platformData[instance.id])
        XCTAssertEqual(mock.requestCount, 0, "禁用实例不得发起请求")

        PlatformInstanceStore.shared.removeInstance(id: instance.id)
    }

    func testCancelledFetchUsageClearsLoadingState() async throws {
        // A4-4②: fetchUsage 被取消时, 旧代码 (do/catch 的 isCancelled 提前 return +
        // 尾段 isCancelled 守卫) 两条出口都不清 isLoading → flag 永久 true.
        // 修复改 defer 退出即清, 覆盖成功/失败/取消所有路径.
        let gated = GatedNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        var instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "取消清 loading")
        instance.isEnabled = true
        ConfigService.shared.store(for: instance).setAPIKey("sess_cancel")

        // 第一次 fetch 在途 → loading 置 true.
        viewModel.fetchUsage(for: instance)
        await waitUntil { gated.hasPending(where: { _ in true }) }
        XCTAssertTrue(viewModel.isLoading[instance.id] == true, "在途 fetch 应置 loading")

        // 同实例第二次 fetch 取消第一次 (换 key / 全量刷新的同一机制):
        // 旧任务以 URLError(.cancelled) 中止, 尾段 defer 必须把 flag 清掉.
        viewModel.fetchUsage(for: instance)
        await waitUntil { gated.requestCount >= 2 }
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertFalse(viewModel.isLoading[instance.id] ?? true,
                       "被取消的 fetch 退出时必须清 loading, 否则永久转圈")

        // 收尾: 释放在途请求 (含 expiring 补充) 让新任务跑完, flag 保持 false.
        for _ in 0..<50 where gated.pendingCount > 0 {
            gated.release(where: { _ in true }, responder: balanceResponder("777.00000000"))
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(viewModel.isLoading[instance.id] ?? true)

        PlatformInstanceStore.shared.removeInstance(id: instance.id)
    }

    // MARK: - R3-1 实例删除清理在途任务 / fetchAllUsage 写回守卫

    private func balanceJSON(_ value: String) -> String {
        """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"\(value)"}}
        """
    }

    /// 按请求 URL 分流响应: summary 给 balanceJSON, expiring-credits 一律 500 (降级无到期信息).
    private func balanceResponder(_ balance: String) -> (URLRequest) -> (Data, URLResponse) {
        { request in
            let url = request.url!.absoluteString
            if url.contains("/wallet/expiring-credits") {
                return ("{}".data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 500))
            }
            return (self.balanceJSON(balance).data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 200))
        }
    }

    func testInstanceRemovalCancelsInFlightFetch() async throws {
        // R3-1: 删除实例时在途的 fetchUsage 必须取消并清出 fetchTasks 字典 —
        // 否则最长 300s 后旧账号响应到达, platformData 写回, "已删除的账号"复活.
        let gated = GatedNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        // 走 shared store 真删 (通知链: onPlatformInstanceRemoved 才会触发).
        let instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "删号测试")
        viewModel.configureAPIKey(for: instance)
        viewModel.apiKeyInput = "sess_del"
        viewModel.saveAPIKey()
        await waitUntil { gated.requestCount == 1 }
        XCTAssertNotNil(viewModel.fetchTasks[instance.id], "在途任务应登记在 fetchTasks 字典")

        // 删除: 三处 @Published 字典清理之外, fetchTasks 项必须一并取消清掉.
        viewModel.removeInstance(instance)
        XCTAssertNil(viewModel.fetchTasks[instance.id], "删除后 fetchTasks 字典项应被清掉")
        XCTAssertNil(viewModel.platformData[instance.id])

        // 旧账号响应恢复: 任务已取消, 结果必须被丢弃 (不复活).
        let responder = balanceResponder("100.00000000")
        for _ in 0..<50 where gated.pendingCount > 0 {
            gated.release(where: { _ in true }, responder: responder)
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        try? await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertNil(viewModel.platformData[instance.id], "已删账号的在途响应不得写回数据")
    }

    func testFetchAllUsageDiscardsResultForRemovedInstance() async throws {
        // R3-1 的另一半 (fetchAllUsage 路径): 结果写回前校验实例仍存在 —
        // 用户在全量刷新的在途期间从菜单删号, 响应到达不得让账号复活.
        let gated = GatedNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        let instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "全量守卫测试")
        var enabledInstance = instance
        enabledInstance.isEnabled = true
        // 直接配 key (不经 saveAPIKey, 避免它顺手触发一次 fetchUsage).
        ConfigService.shared.store(for: enabledInstance).setAPIKey("sess_guard")

        let allTask = Task { await viewModel.fetchAllUsage() }
        await waitUntil { gated.requestCount >= 1 }

        // 在途期间删除实例 (菜单删号路径: shared store 发通知).
        PlatformInstanceStore.shared.removeInstance(id: instance.id)
        XCTAssertNil(viewModel.platformData[instance.id])

        // 恢复在途响应 → fetchAllUsage 的结果循环必须跳过已删实例.
        let responder = balanceResponder("100.00000000")
        for _ in 0..<50 where gated.pendingCount > 0 {
            gated.release(where: { _ in true }, responder: responder)
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        await allTask.value
        try? await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertNil(viewModel.platformData[instance.id], "已删实例的 fetchAllUsage 结果不得写回 (防复活)")
    }

    func testFetchAllUsageCancelsPerInstanceInFlightTasks() async throws {
        // R3-4: fetchAllUsage (全量刷新) 启动时应取消所有 per-instance 在途任务 —
        // 全量替代局部, 局部旧响应不得在全量结果到达后覆写.
        let gated = GatedNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: .shared)
        let viewModel = PlatformViewModel(platformManager: manager)
        let instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "全量取消测试")
        var enabledInstance = instance
        enabledInstance.isEnabled = true
        ConfigService.shared.store(for: enabledInstance).setAPIKey("sess_race")

        // 局部刷新在途 (请求 1 = 旧数据 100).
        viewModel.fetchUsage(for: instance)
        await waitUntil { gated.requestCount == 1 }
        XCTAssertNotNil(viewModel.fetchTasks[instance.id])

        // 全量刷新启动 (请求 2 = 新数据 999): 在途局部任务应被取消.
        let allTask = Task { await viewModel.fetchAllUsage() }
        await waitUntil { gated.requestCount == 2 }
        XCTAssertTrue(viewModel.fetchTasks[instance.id]?.isCancelled == true,
                      "fetchAllUsage 启动应取消 per-instance 在途任务 (R3-4)")

        // 释放 summary 类在途请求: 按 URL 区分 (expiring 一律 500 降级).
        let releaseSummary: (String) -> Bool = { balance in
            gated.release(where: { !$0.url!.absoluteString.contains("/wallet/expiring-credits") }) { request in
                let url = request.url!.absoluteString
                if url.contains("/wallet/expiring-credits") {
                    return ("{}".data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 500))
                }
                return (self.balanceJSON(balance).data(using: .utf8)!, MockNetworkService.makeResponse(url: url, statusCode: 200))
            }
        }

        // 局部刷新的旧请求在 fetchAllUsage 取消在途任务时已被 mock 按 URLSession
        // 语义中止 (A4-1): pending 里剩下的就是全量的新请求 — 旧请求没有"晚到
        // 覆盖"窗口. (无取消逻辑时这里会 resume 成功并写回 100, 即回归拦截点.)
        await waitUntil { gated.pendingCount <= 1 }
        XCTAssertTrue(releaseSummary("999.00000000"), "应释放全量刷新的新请求")
        // 收尾: 新 fetch 的 expiring 补充请求是 summary 响应后才异步发出的,
        // 循环判 pendingCount 会在它发出前退出 — 改为轮询到结果写回为止,
        // 每轮把新出现的在途请求放掉.
        let responder = balanceResponder("888.00000000")
        for _ in 0..<100 where viewModel.platformData[instance.id] == nil {
            gated.release(where: { _ in true }, responder: responder)
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        await allTask.value

        XCTAssertEqual(viewModel.platformData[instance.id]?.metrics[0].currentValue ?? -1, 999, accuracy: 0.001,
                       "局部刷新的旧响应 (100) 不得覆写全量结果 (999)")

        // 收尾: 从 shared store 移除测试实例 (隔离 suite, 不影响真实配置).
        PlatformInstanceStore.shared.removeInstance(id: instance.id)
    }

    // MARK: - R-5 「重新登录」按钮仅 unauthorized 显示

    func testShowsRenewSessionButtonForUnauthorizedOnCookiePlatform() {
        // cookie 平台 (StepFun/TokenRhythm, 会话型凭据) + unauthorized (登录过期):
        // 唯一"重新登录能治好"的组合, 必须显示.
        let viewModel = PlatformViewModel()
        let instance = PlatformInstance(id: "r5-unauth-tr", platformType: .tokenrhythm, displayName: "")
        viewModel.platformErrors[instance.id] = .unauthorized(.tokenrhythm)
        XCTAssertTrue(viewModel.showsRenewSessionButton(for: instance))

        let stepfun = PlatformInstance(id: "r5-unauth-sf", platformType: .stepfun, displayName: "")
        viewModel.platformErrors[stepfun.id] = .unauthorized(.stepfun)
        XCTAssertTrue(viewModel.showsRenewSessionButton(for: stepfun))
    }

    func testShowsRenewSessionButtonHiddenForNonUnauthorizedErrors() {
        // 网络错误 / 业务错误 / 无错误: 重新登录治不好, 显示即误导 — 必须隐藏.
        let viewModel = PlatformViewModel()
        let instance = PlatformInstance(id: "r5-net-tr", platformType: .tokenrhythm, displayName: "")

        viewModel.platformErrors[instance.id] = .networkError(.tokenrhythm, "timeout")
        XCTAssertFalse(viewModel.showsRenewSessionButton(for: instance),
                       "网络错误不得显示重新登录 (重登治不好)")

        viewModel.platformErrors[instance.id] = .apiError(.tokenrhythm, "boom")
        XCTAssertFalse(viewModel.showsRenewSessionButton(for: instance),
                       "业务错误不得显示重新登录")

        viewModel.platformErrors[instance.id] = .decodingError(.tokenrhythm, "bad json")
        XCTAssertFalse(viewModel.showsRenewSessionButton(for: instance))

        viewModel.platformErrors[instance.id] = nil
        XCTAssertFalse(viewModel.showsRenewSessionButton(for: instance), "无错误不得显示")
    }

    func testShowsRenewSessionButtonHiddenForKeyPlatformEvenWhenUnauthorized() {
        // API key 平台 (MiniMax/GLM) 没有会话可续: 即使 unauthorized 也不显示.
        let viewModel = PlatformViewModel()
        let minimax = PlatformInstance(id: "r5-key-mm", platformType: .minimax_cn, displayName: "")
        viewModel.platformErrors[minimax.id] = .unauthorized(.minimax_cn)
        XCTAssertFalse(viewModel.showsRenewSessionButton(for: minimax))

        let glm = PlatformInstance(id: "r5-key-glm", platformType: .glm_cn, displayName: "")
        viewModel.platformErrors[glm.id] = .unauthorized(.glm_cn)
        XCTAssertFalse(viewModel.showsRenewSessionButton(for: glm))
    }
}
