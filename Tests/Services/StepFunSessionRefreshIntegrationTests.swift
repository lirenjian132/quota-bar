import XCTest
@testable import QuotaBar

/// PlatformManager × StepFunSessionRefresher 集成: 预刷新在主请求前一步发生,
/// 新凭据经 setAPIKey 落库后主请求/后续周期都用它; 刷新失败/不需刷新时
/// 主流程不受任何影响. 全部走 AppEnvironment 隔离 defaults + 内存 Keychain,
/// 不触碰真实配置.
final class StepFunSessionRefreshIntegrationTests: XCTestCase {
    private var mockNetwork: MockNetworkService!
    private let dashboardURL = "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard"
    private let webid = "2c321138f52a7d858f2d9eab619a357038d0f66c"
    // 会话内创建的实例 id, tearDown 清掉各自的 defaults/keychain 残留.
    private var createdInstanceIDs: [String] = []

    override func setUp() {
        super.setUp()
        mockNetwork = MockNetworkService()
        createdInstanceIDs = []
    }

    override func tearDown() {
        for id in createdInstanceIDs {
            AppEnvironment.defaults.removeObject(forKey: "quotabar.instance.\(id)")
            _ = try? AppEnvironment.makeKeychain().delete(account: id)
        }
        createdInstanceIDs = []
        super.tearDown()
    }

    // MARK: - fixture

    private func base64url(_ string: String) -> String {
        Data(string.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private func jwt(payload: [String: Any], signature: String = "fakesig") -> String {
        let header = base64url("{\"alg\":\"EdDSA\"}")
        let payloadData = try! JSONSerialization.data(withJSONObject: payload)
        let body = base64url(String(data: payloadData, encoding: .utf8)!)
        return "\(header).\(body).\(signature)"
    }

    private func nowPlus(_ offset: TimeInterval) -> Int {
        Int(Date().timeIntervalSince1970) + Int(offset)
    }

    /// access token 在 offset 秒后过期的合法 StepFun 凭据串.
    private func stepfunCredential(expOffset: TimeInterval) -> String {
        let access = jwt(payload: ["exp": nowPlus(expOffset), "mode": 2, "oasis_id": "u-1"])
        let refresh = jwt(payload: ["exp": nowPlus(86400 * 30), "app_id": 10300, "device_id": webid])
        return "Oasis-Webid=\(webid); Oasis-Token=\(access)...\(refresh)"
    }

    /// 建一个已配置的 StepFun 实例: api_base_url 直接预置 (不依赖 Bundle template),
    /// apiKey 走 setAPIKey 落隔离 keychain.
    @discardableResult
    private func makeConfiguredStepFunInstance(credential: String) -> (instance: PlatformInstance, store: PlatformConfigStore) {
        let instance = PlatformInstance(id: "stepfun-it-\(UUID().uuidString)", platformType: .stepfun, displayName: "")
        AppEnvironment.defaults.set([
            "api_base_url": dashboardURL,
            "auth_header": "Cookie",
            "auth_prefix": "",
            "region": "domestic"
        ], forKey: "quotabar.instance.\(instance.id)")
        createdInstanceIDs.append(instance.id)
        let store = ConfigService.shared.store(for: instance)
        store.setAPIKey(credential)
        return (instance, store)
    }

    private func refreshResponse(statusCode: Int = 200,
                                 rawAccess: String = "aaa.bbb.ccc",
                                 rawRefresh: String = "ddd.eee.fff") -> (Data, HTTPURLResponse) {
        let json = #"{"accessToken":{"raw":"\#(rawAccess)","duration":1800,"mode":2},"refreshToken":{"raw":"\#(rawRefresh)"}}"#
        return (json.data(using: .utf8)!,
                MockNetworkService.makeResponse(url: StepFunSessionRefresher.refreshURL, statusCode: statusCode))
    }

    private func dashboardResponse(_ method: String, json: String) -> (Data, HTTPURLResponse) {
        (json.data(using: .utf8)!,
         MockNetworkService.makeResponse(url: "\(dashboardURL)/\(method)", statusCode: 200))
    }

    private let rateLimitJSON = #"{"status":1,"desc":"","plan_credit_rate_limit":{"subscription_credit_left_rate":0.96886694,"subscription_credit_reset_time":"1792502883"}}"#
    private let planStatusJSON = #"{"status":1,"desc":"","subscription":{"plan_type":2,"name":"Pro","status":1,"expired_at":"1825171280"}}"#

    private func makeManager() -> PlatformManager {
        PlatformManager(networkService: mockNetwork, configService: .shared, instanceStore: PlatformInstanceStore.shared)
    }

    // MARK: - 门控 mock + 轮询帮助 (P0-1 / P1-1 竞态时序)

    /// 轮询等待条件满足 (竞态测试不能靠固定 sleep 时序).
    private func waitUntil(_ condition: @escaping () -> Bool, attempts: Int = 250) async {
        for _ in 0..<attempts where !condition() {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    private func isRefreshURL(_ request: URLRequest) -> Bool {
        request.url?.absoluteString == StepFunSessionRefresher.refreshURL
    }

    private func isDashboard(_ request: URLRequest, method: String) -> Bool {
        request.url?.absoluteString.hasSuffix(method) == true
    }

    // MARK: - 刷新成功路径

    func testExpiringSessionRefreshesBeforeMainRequestAndPersistsNewCredential() async throws {
        let oldCredential = stepfunCredential(expOffset: 240) // 4 分钟后过期 → 需刷新
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)

        mockNetwork.responseSequence = [
            refreshResponse(),
            dashboardResponse("QueryStepPlanRateLimit", json: rateLimitJSON),
            dashboardResponse("GetStepPlanStatus", json: planStatusJSON)
        ]

        let data = try await makeManager().fetchUsage(for: instance)

        XCTAssertEqual(mockNetwork.requestCount, 3, "1 次刷新 + 2 次主请求")
        XCTAssertEqual(data.metrics.first?.label, "credits", "主流程应正常拿到用量")

        // 请求1 = 刷新, 契约头全部就位.
        let refreshRequest = mockNetwork.sequenceCapture[0]
        XCTAssertEqual(refreshRequest.url?.absoluteString, StepFunSessionRefresher.refreshURL)
        XCTAssertEqual(refreshRequest.httpMethod, "POST")
        XCTAssertEqual(refreshRequest.value(forHTTPHeaderField: "oasis-webid"), webid)
        XCTAssertEqual(refreshRequest.value(forHTTPHeaderField: "Cookie"), oldCredential, "刷新请求带旧凭据 (含旧 refresh JWT)")

        // 请求2 = 主请求, Cookie 已是新凭据 — 证明 setAPIKey 后 toConfigData 拿到新串.
        let mainRequest = mockNetwork.sequenceCapture[1]
        XCTAssertTrue(mainRequest.url?.absoluteString.hasSuffix("QueryStepPlanRateLimit") == true)
        let expectedNewCredential = "Oasis-Webid=\(webid); Oasis-Token=aaa.bbb.ccc...ddd.eee.fff"
        XCTAssertEqual(mainRequest.value(forHTTPHeaderField: "Cookie"), expectedNewCredential)

        // 落库: 内存 store + keychain 都已是新凭据 (下次启动同样用新凭据).
        XCTAssertEqual(store.apiKey, expectedNewCredential)
        XCTAssertEqual(try AppEnvironment.makeKeychain().get(account: instance.id), expectedNewCredential)
    }

    // MARK: - 不需刷新路径

    func testFreshSessionSkipsRefreshRequest() async throws {
        // access token 还有 1 小时 → 纯本地判断不发刷新请求.
        let credential = stepfunCredential(expOffset: 3600)
        let (instance, store) = makeConfiguredStepFunInstance(credential: credential)

        mockNetwork.responseSequence = [
            dashboardResponse("QueryStepPlanRateLimit", json: rateLimitJSON),
            dashboardResponse("GetStepPlanStatus", json: planStatusJSON)
        ]

        _ = try await makeManager().fetchUsage(for: instance)

        XCTAssertEqual(mockNetwork.requestCount, 2, "只应有 2 次主请求, 无刷新")
        for request in mockNetwork.sequenceCapture {
            XCTAssertTrue(request.url?.absoluteString.contains("Dashboard") == true,
                          "不应出现刷新端点请求: \(request.url?.absoluteString ?? "")")
        }
        XCTAssertEqual(store.apiKey, credential, "凭据保持不变")
    }

    // MARK: - 刷新失败不阻断

    func testRefreshFailureDoesNotBlockMainRequest() async throws {
        // 刷新被拒 (401): 不写库, 主请求照常发出 (此后由既有 401 错误链兜底).
        let oldCredential = stepfunCredential(expOffset: 240)
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)

        mockNetwork.responseSequence = [
            refreshResponse(statusCode: 401),
            dashboardResponse("QueryStepPlanRateLimit", json: rateLimitJSON),
            dashboardResponse("GetStepPlanStatus", json: planStatusJSON)
        ]

        let data = try await makeManager().fetchUsage(for: instance)

        XCTAssertEqual(mockNetwork.requestCount, 3, "刷新失败后主请求仍继续")
        XCTAssertEqual(data.metrics.first?.label, "credits", "主流程不受刷新失败影响")
        XCTAssertEqual(store.apiKey, oldCredential, "刷新失败不得写入任何凭据")
        XCTAssertEqual(mockNetwork.sequenceCapture[1].value(forHTTPHeaderField: "Cookie"), oldCredential)
    }

    // MARK: - 平台边界

    func testNonStepFunPlatformNeverTriggersRefresh() async throws {
        // MiniMax 实例即使配了"即将过期"的串也不刷新: 预刷新只对 stepfun 生效.
        let instance = PlatformInstance(id: "minimax-it-\(UUID().uuidString)", platformType: .minimax_cn, displayName: "")
        AppEnvironment.defaults.set([
            "api_base_url": "https://api.minimaxi.com",
            "auth_header": "Authorization",
            "auth_prefix": "Bearer ",
            "region": "domestic"
        ], forKey: "quotabar.instance.\(instance.id)")
        createdInstanceIDs.append(instance.id)
        let store = ConfigService.shared.store(for: instance)
        // 形似 StepFun 双 JWT 且"4 分钟后过期"的 key: 若平台判断漏了会被当会话刷掉.
        let credential = stepfunCredential(expOffset: 240)
        store.setAPIKey(credential)

        let remainJSON = #"{"model_remains":[{"model_name":"general","current_interval_remaining_percent":90,"current_weekly_remaining_percent":80,"current_weekly_status":1}]}"#
        mockNetwork.responseSequence = [
            (remainJSON.data(using: .utf8)!,
             MockNetworkService.makeResponse(url: "https://api.minimaxi.com/v1/api/openapi/coding_usage/remains", statusCode: 200))
        ]

        let data = try await makeManager().fetchUsage(for: instance)

        XCTAssertEqual(mockNetwork.requestCount, 1, "MiniMax 只发自己的 1 次请求, 不应触发刷新")
        XCTAssertEqual(data.metrics.first?.label, "five_hour")
        XCTAssertEqual(store.apiKey, credential, "凭据保持不变")
    }

    // MARK: - P0-1: 刷新在途期间用户保存新凭据 (CAS)

    func testRefreshInFlightUserSaveWinsOverRefreshedCredential() async throws {
        // 竞态时序: 预刷新 POST 在途 → 用户 (主线程 saveAPIKey / renewSession) 保存
        // 新凭据 → 刷新响应才回到 PlatformManager. 旧写法此时无条件 setAPIKey,
        // 把用户新 key 覆盖成旧凭据刷出的串 (表现为"刚改的 key 自己变回去").
        // 修复: 写回前 CAS — store.apiKey 仍是本次刷新用的旧串才落库.
        let oldCredential = stepfunCredential(expOffset: 240)
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)

        let gated = GatedRefreshNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: PlatformInstanceStore.shared)

        let fetchTask = Task { try await manager.fetchUsage(for: instance) }
        // 刷新请求已到达并挂起 (在途窗口打开).
        await waitUntil { gated.hasPending { self.isRefreshURL($0) } }

        // 用户在刷新在途期间保存新凭据 (主线程写库的等效动作).
        let userNewCredential = "Oasis-Webid=\(webid); Oasis-Token=user.new.access...user.new.refresh"
        store.setAPIKey(userNewCredential)

        // 放行在途刷新: 旧凭据 (oldCredential) 刷出的新串此刻才回来.
        gated.release(where: isRefreshURL) { _ in refreshResponse() }
        // 主请求应带用户新凭据 (toConfigData 在刷新块之后读 store).
        await waitUntil { gated.hasPending { self.isDashboard($0, method: "QueryStepPlanRateLimit") } }
        let mainRequest = gated.requests.first { self.isDashboard($0, method: "QueryStepPlanRateLimit") }
        XCTAssertEqual(mainRequest?.value(forHTTPHeaderField: "Cookie"), userNewCredential,
                       "主请求必须用用户新凭据 (刷新写回已被 CAS 放弃)")
        gated.release(where: { self.isDashboard($0, method: "QueryStepPlanRateLimit") }) { _ in
            self.dashboardResponse("QueryStepPlanRateLimit", json: self.rateLimitJSON)
        }
        await waitUntil { gated.hasPending { self.isDashboard($0, method: "GetStepPlanStatus") } }
        gated.release(where: { self.isDashboard($0, method: "GetStepPlanStatus") }) { _ in
            self.dashboardResponse("GetStepPlanStatus", json: self.planStatusJSON)
        }

        let data = try await fetchTask.value

        XCTAssertEqual(data.metrics.first?.label, "credits", "放弃写回不阻断主流程")
        XCTAssertEqual(gated.refreshRequestCount, 1)
        // CAS 生效: store 与 keychain 都保留用户新值, 不被刷新值覆盖.
        XCTAssertEqual(store.apiKey, userNewCredential)
        XCTAssertEqual(try AppEnvironment.makeKeychain().get(account: instance.id), userNewCredential)
    }

    func testRefreshedCredentialWrittenBackWhenStoreUnchanged() async throws {
        // 对照面: 无并发改库时写回正常 (CAS 不能把正常路径也挡掉).
        let oldCredential = stepfunCredential(expOffset: 240)
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)

        let gated = GatedRefreshNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: PlatformInstanceStore.shared)

        let fetchTask = Task { try await manager.fetchUsage(for: instance) }
        await waitUntil { gated.hasPending { self.isRefreshURL($0) } }
        gated.release(where: isRefreshURL) { _ in refreshResponse() }
        await waitUntil { gated.hasPending { self.isDashboard($0, method: "QueryStepPlanRateLimit") } }
        gated.release(where: { self.isDashboard($0, method: "QueryStepPlanRateLimit") }) { _ in
            self.dashboardResponse("QueryStepPlanRateLimit", json: self.rateLimitJSON)
        }
        await waitUntil { gated.hasPending { self.isDashboard($0, method: "GetStepPlanStatus") } }
        gated.release(where: { self.isDashboard($0, method: "GetStepPlanStatus") }) { _ in
            self.dashboardResponse("GetStepPlanStatus", json: self.planStatusJSON)
        }
        _ = try await fetchTask.value

        XCTAssertEqual(store.apiKey, "Oasis-Webid=\(webid); Oasis-Token=aaa.bbb.ccc...ddd.eee.fff")
    }

    // MARK: - P1-1: 同实例并发双 fetch 只发一次刷新

    func testConcurrentFetchUsageSharesSingleRefreshPost() async throws {
        // 预刷新原本无单飞门控: saveAPIKey 后的新 fetch 与 fetchAllUsage 在途可对
        // 同一实例双发 RefreshToken POST. 修复: per-instance in-flight 刷新 Task,
        // 已有在途则 await 复用其结果.
        let oldCredential = stepfunCredential(expOffset: 240)
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)

        let gated = GatedRefreshNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: PlatformInstanceStore.shared)

        // 第一个 fetch 的刷新在途 (门控保证它不完成, 第二个 fetch 一定能撞上门控).
        let task1 = Task { try await manager.fetchUsage(for: instance) }
        await waitUntil { gated.refreshRequestCount >= 1 }

        // 刷新在途期间发起第二次 fetch (saveAPIKey 后新 fetch 的等效叠加).
        let task2 = Task { try await manager.fetchUsage(for: instance) }
        // task2 应命中在途刷新直接复用: 轮询窗口内不得出现第二个刷新 POST.
        var duplicateRefreshSeen = false
        for _ in 0..<250 {
            if gated.refreshRequestCount >= 2 { duplicateRefreshSeen = true; break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertFalse(duplicateRefreshSeen, "同实例在途刷新必须复用, 不得重复 POST")

        // 放行刷新: task1 CAS 写回新凭据; task2 复用同一结果, CAS 自然失配不重写.
        gated.release(where: isRefreshURL) { _ in refreshResponse() }
        await waitUntil { store.apiKey != oldCredential }

        // 主请求链路本测试不关心 (放行只会拖长时序), 取消两个 fetch 收尾.
        task1.cancel()
        task2.cancel()

        XCTAssertEqual(gated.refreshRequestCount, 1, "刷新 POST 只发一次")
        XCTAssertEqual(store.apiKey, "Oasis-Webid=\(webid); Oasis-Token=aaa.bbb.ccc...ddd.eee.fff",
                       "单飞 + CAS 叠加后落库值仍是那一次刷新的新凭据")
    }

    /// P0-1 同族变种: 在途刷新期间用户换成新凭据 → 新 fetch 不得复用旧凭据的
    /// 在途刷新结果 (复用会把用户新凭据覆盖成旧凭据刷出的串, CAS 放行).
    /// 门控键带凭据维度: 不同凭据 → 各自独立刷新, 最终落库是新凭据刷出的值.
    func testInFlightRefreshWithDifferentCredentialIsNotReused() async throws {
        let oldCredential = stepfunCredential(expOffset: 240)
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)
        // 新凭据同样"即将过期" (210s < 300s 阈值), 但 exp 不同 → 字符串与旧凭据
        // 必然不同 (同秒生成会拿到同一串, 门控按凭据键复用就测不到想测的分支).
        let userNewCredential = stepfunCredential(expOffset: 210)

        let gated = GatedRefreshNetworkService()
        let manager = PlatformManager(networkService: gated, configService: .shared, instanceStore: PlatformInstanceStore.shared)

        let task1 = Task { try await manager.fetchUsage(for: instance) }
        await waitUntil { gated.hasPending { self.isRefreshURL($0) } }

        // 用户在途期间保存新凭据.
        store.setAPIKey(userNewCredential)
        // 新 fetch: 它与在途刷新用的凭据不同 → 必须自己发刷新, 不得复用.
        let task2 = Task { try await manager.fetchUsage(for: instance) }
        await waitUntil { gated.refreshRequestCount >= 2 }

        // 分别放行两个刷新 (按请求 Cookie 区分刷的是哪个凭据), 响应是不同的 token.
        gated.release(where: { $0.value(forHTTPHeaderField: "Cookie") == oldCredential }) { _ in refreshResponse() }
        gated.release(where: { $0.value(forHTTPHeaderField: "Cookie") == userNewCredential }) { _ in
            self.refreshResponse(rawAccess: "ggg.hhh.iii", rawRefresh: "jjj.kkk.lll")
        }

        let expected = "Oasis-Webid=\(webid); Oasis-Token=ggg.hhh.iii...jjj.kkk.lll"
        await waitUntil { store.apiKey == expected }
        // task1 的刷新结果 (旧凭据刷出 aaa.bbb.ccc) 被 CAS 挡下; task2 的写回生效.
        XCTAssertEqual(store.apiKey, expected, "落库必须是用户新凭据刷出的值, 不被旧凭据刷新结果覆盖")

        task1.cancel()
        task2.cancel()
    }

    // MARK: - P2-1 盲区: HTTP 200 但响应体不全

    func testRefresh200WithoutAccessTokenDoesNotBlockMainRequest() async throws {
        // 契约漂移: HTTP 200 但体里没有 accessToken → 刷新判失败, 不写凭据,
        // 主请求照常发出 (此后由既有 401 错误链兜底).
        let oldCredential = stepfunCredential(expOffset: 240)
        let (instance, store) = makeConfiguredStepFunInstance(credential: oldCredential)

        mockNetwork.responseSequence = [
            (Data(#"{"refreshToken":{"raw":"ddd.eee.fff"}}"#.utf8),
             MockNetworkService.makeResponse(url: StepFunSessionRefresher.refreshURL, statusCode: 200)),
            dashboardResponse("QueryStepPlanRateLimit", json: rateLimitJSON),
            dashboardResponse("GetStepPlanStatus", json: planStatusJSON)
        ]

        let data = try await makeManager().fetchUsage(for: instance)

        XCTAssertEqual(mockNetwork.requestCount, 3, "刷新失败后主请求仍继续")
        XCTAssertEqual(data.metrics.first?.label, "credits", "主流程不受刷新失败影响")
        XCTAssertEqual(store.apiKey, oldCredential, "刷新失败不得写入任何凭据")
        XCTAssertEqual(mockNetwork.sequenceCapture[1].value(forHTTPHeaderField: "Cookie"), oldCredential)
    }
}

/// 门控 network mock (参照 PlatformViewModelTests.GatedNetworkService): 每个请求
/// 到达即挂起, 由测试按 URL 条件放行并决定响应体. 用来在"刷新在途"窗口内同步
/// 改 store — 确定性复现 P0-1 (CAS) / P1-1 (单飞) 的竞态时序, 不靠 sleep 碰运气.
private final class GatedRefreshNetworkService: NetworkService {
    /// 一个在途请求的挂起句柄. 类引用做身份: 放行/取消都以引用同一性摘除.
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
    /// 全部请求按到达顺序捕获 (含已放行的), 供断言各次请求的头.
    var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return captured }
    var refreshRequestCount: Int {
        lock.lock(); defer { lock.unlock() }
        return captured.filter { $0.url?.absoluteString == StepFunSessionRefresher.refreshURL }.count
    }

    func hasPending(where matches: (URLRequest) -> Bool) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return pending.contains { matches($0.request) }
    }

    func data(from request: URLRequest) async throws -> (Data, URLResponse) {
        lock.lock()
        count += 1
        captured.append(request)
        lock.unlock()
        let box = PendingRequest(request: request)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<(Data, URLResponse), Error>) in
                self.lock.lock()
                box.continuation = continuation
                self.pending.append(box)
                self.lock.unlock()
                // 注册前任务已被取消 (onCancel 已跑完, 没人会来放行): 自己中止,
                // 否则这个请求永远没人 resume.
                if Task.isCancelled { self.abort(box) }
            }
        } onCancel: {
            self.abort(box)
        }
    }

    private func abort(_ box: PendingRequest) {
        lock.lock()
        let continuation = box.continuation
        box.continuation = nil
        pending.removeAll { $0 === box }
        lock.unlock()
        continuation?.resume(throwing: URLError(.cancelled))
    }

    /// 放行第一个满足 matches 的挂起请求, 响应由 responder 按请求内容决定.
    /// 无匹配 (尚未到达 / 已被取消中止) 时无操作, 调用方轮询重试.
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
