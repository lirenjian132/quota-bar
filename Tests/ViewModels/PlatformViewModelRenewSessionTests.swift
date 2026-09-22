import XCTest
@testable import QuotaBar

/// PlatformViewModel.renewSession 一键续期: 注入 fake 登录窗, 验证
/// no-op / 回调通道 / 凭据落库 / 触发刷新 / 取消不改动.
/// 走 AppEnvironment 隔离套件 (testDefaults + 内存 keychain), 不碰真实配置.
@MainActor
final class PlatformViewModelRenewSessionTests: XCTestCase {
    private let defaults = AppEnvironment.testDefaults
    private var mock: MockNetworkService!
    private var manager: PlatformManager!
    private var fakeWindow: FakeLoginWindowController!
    private var factoryCallCount = 0

    override func setUp() async throws {
        // 不清 defaults (共享 suite 已被 AppEnvironment 首访清过一次):
        // ConfigService.shared / PlatformInstanceStore.shared 是本进程单例,
        // 内存态跨测试类共享, 靠"加实例→用完即删"保持隔离, 与 PlatformViewModelTests 一致.
        mock = MockNetworkService()
        manager = PlatformManager(networkService: mock, configService: .shared, instanceStore: .shared)
        fakeWindow = FakeLoginWindowController()
        factoryCallCount = 0
    }

    private func makeViewModel() -> PlatformViewModel {
        PlatformViewModel(platformManager: manager, loginWindowFactory: { _ in
            self.factoryCallCount += 1
            return self.fakeWindow
        })
    }

    /// 轮询等待请求发出 (renewSession 的 fetchUsage 是不等待的 Task).
    private func waitForRequest() async {
        for _ in 0..<250 where mock.requestCount == 0 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    // MARK: - no-op

    func testRenewSessionIsNoOpForKeyPlatform() {
        let viewModel = makeViewModel()
        // MiniMax/GLM 是 API key 平台: 不得建窗, 不得有任何动作.
        let minimax = PlatformInstance(id: "renew-noop", platformType: .minimax_cn, displayName: "")
        viewModel.renewSession(for: minimax)
        let glm = PlatformInstance(id: "renew-noop-glm", platformType: .glm_cn, displayName: "")
        viewModel.renewSession(for: glm)

        XCTAssertEqual(factoryCallCount, 0, "API key 平台不得创建登录窗")
        XCTAssertEqual(fakeWindow.presentCount, 0)
    }

    // MARK: - 窗口与回调通道

    func testRenewSessionPresentsWindowAndWiresCallbacks() {
        let viewModel = makeViewModel()
        var instance = PlatformInstanceStore.shared.addInstance(of: .stepfun, displayName: "StepFun 续期")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }

        viewModel.renewSession(for: instance)

        XCTAssertEqual(factoryCallCount, 1, "cookie 平台应创建登录窗")
        XCTAssertEqual(fakeWindow.presentCount, 1, "登录窗应被 present")
        // 窗口创建不可深度断言 (WebKit), 钉住回调通道存在性即可.
        XCTAssertNotNil(fakeWindow.onComplete, "完成回调通道应接通")
        XCTAssertNotNil(fakeWindow.onCancel, "取消回调通道应接通")
    }

    // MARK: - 完成 → 落库 + 刷新

    func testRenewSessionStepFunCompleteWritesCredentialAndFetches() async throws {
        mock.mockData = Data("{\"code\":0}".utf8)
        mock.mockResponse = MockNetworkService.makeResponse(url: "https://platform.stepfun.com/", statusCode: 200)

        let viewModel = makeViewModel()
        var instance = PlatformInstanceStore.shared.addInstance(of: .stepfun, displayName: "StepFun 续期")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }
        ConfigService.shared.store(for: instance).setAPIKey("Oasis-Webid=old-web; Oasis-Token=old-tok")

        viewModel.renewSession(for: instance)
        fakeWindow.simulateComplete("Oasis-Webid=new-web; Oasis-Token=new-tok")

        // StepFun: 整串 cookie 原样入库 (auth_prefix 为空).
        XCTAssertEqual(
            ConfigService.shared.store(for: instance).apiKey,
            "Oasis-Webid=new-web; Oasis-Token=new-tok",
            "StepFun 凭据串应原样写入凭据存储"
        )

        // 续期后自动局部刷新, 且用新凭据发请求.
        await waitForRequest()
        XCTAssertGreaterThanOrEqual(mock.requestCount, 1, "续期应触发一次数据刷新")
        XCTAssertEqual(
            mock.lastRequest?.value(forHTTPHeaderField: "Cookie"),
            "Oasis-Webid=new-web; Oasis-Token=new-tok",
            "刷新请求应使用新 cookie"
        )
    }

    func testRenewSessionTokenRhythmCompleteStripsPrefixAndRefreshes() async throws {
        // TokenRhythm 有效响应 (template base URL): 刷新应真的拿到数据.
        mock.mockData = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"253.76250848"}}
        """.data(using: .utf8)
        mock.mockResponse = MockNetworkService.makeResponse(
            url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        let viewModel = makeViewModel()
        var instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "TokenRhythm 续期")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }
        ConfigService.shared.store(for: instance).setAPIKey("sess_old")

        viewModel.renewSession(for: instance)
        fakeWindow.simulateComplete("tr_session=sess_new")

        // 关键: 只存裸值. 存整串会让 Cookie 头变成 "tr_session=tr_session=…".
        XCTAssertEqual(
            ConfigService.shared.store(for: instance).apiKey,
            "sess_new",
            "TokenRhythm 必须只存 cookie 裸值 (template auth_prefix 会补 tr_session=)"
        )

        await waitForRequest()
        XCTAssertEqual(
            mock.lastRequest?.value(forHTTPHeaderField: "Cookie"),
            "tr_session=sess_new",
            "刷新请求的 Cookie 头应恰好是 tr_session=sess_new"
        )
    }

    func testRenewSessionRefreshAfterRenewalUpdatesData() async throws {
        // 续期成功后数据自动刷新: 余额从旧值变新值 (证明不只是发了请求, 还写回了).
        mock.mockData = """
        {"code":0,"message":"ok","data":{"currency":"CNY","availableBalanceCny":"888.00000000"}}
        """.data(using: .utf8)
        mock.mockResponse = MockNetworkService.makeResponse(
            url: "https://tokenrhythm.studio/api/wallet/summary", statusCode: 200)

        let viewModel = makeViewModel()
        var instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "TokenRhythm 刷新")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }
        ConfigService.shared.store(for: instance).setAPIKey("sess_old")

        viewModel.renewSession(for: instance)
        fakeWindow.simulateComplete("tr_session=sess_new")

        for _ in 0..<250 where (viewModel.platformData[instance.id]?.metrics.first?.currentValue ?? 0) == 0 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertEqual(viewModel.platformData[instance.id]?.metrics.first?.currentValue ?? -1, 888, accuracy: 0.001,
                       "续期后局部刷新应把新会话的数据写回")
    }

    // MARK: - 防双窗 (R-4)

    func testRenewSessionTwiceKeepsSingleWindow() {
        // R-4: 在途登录窗未关闭时再次唤起必须 no-op — 连点菜单/按钮只弹一扇
        // (否则每点一次建一扇, 用户在一扇里登录, 另一扇白等).
        let viewModel = makeViewModel()
        let instance = PlatformInstanceStore.shared.addInstance(of: .stepfun, displayName: "防双窗")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }

        viewModel.renewSession(for: instance)
        viewModel.renewSession(for: instance)
        viewModel.renewSession(for: instance)

        XCTAssertEqual(factoryCallCount, 1, "在途窗口存在时工厂不得被再次调用")
        XCTAssertEqual(fakeWindow.presentCount, 1, "连续唤起只应 present 一次")
    }

    // MARK: - 取消

    func testRenewSessionCancelDoesNotTouchCredentialOrFetch() {
        let viewModel = makeViewModel()
        var instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "TokenRhythm 取消")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }
        ConfigService.shared.store(for: instance).setAPIKey("sess_old")

        viewModel.renewSession(for: instance)
        fakeWindow.simulateCancel()

        XCTAssertEqual(ConfigService.shared.store(for: instance).apiKey, "sess_old", "取消不得改动凭据")
        XCTAssertEqual(mock.requestCount, 0, "取消不得触发刷新")
        // 取消后可再次唤起 (窗口引用已释放, 工厂能被再次调用).
        viewModel.renewSession(for: instance)
        XCTAssertEqual(factoryCallCount, 2)
        XCTAssertEqual(fakeWindow.presentCount, 2)
    }

    // MARK: - 取消后异步回调禁写凭据 (R-6)

    func testRenewSessionStaleCompleteAfterCancelDoesNotWrite() {
        // R-6: 用户取消后, getAllCookies 迟到的完成回调不得写凭据 / 不得触发刷新.
        // 生产 LoginWindowController 用 LoginCallbackGate 拦这道; fake 走同一
        // 闸门, 集成层钉住"取消之后 stale complete 零副作用".
        let viewModel = makeViewModel()
        let instance = PlatformInstanceStore.shared.addInstance(of: .tokenrhythm, displayName: "迟到回调")
        defer { PlatformInstanceStore.shared.removeInstance(id: instance.id) }
        ConfigService.shared.store(for: instance).setAPIKey("sess_old")

        viewModel.renewSession(for: instance)
        fakeWindow.simulateCancel()
        fakeWindow.simulateComplete("tr_session=sess_new")   // 取消后迟到的完成回调

        XCTAssertEqual(ConfigService.shared.store(for: instance).apiKey, "sess_old",
                       "取消后迟到的完成回调不得改写凭据")
        XCTAssertEqual(mock.requestCount, 0, "取消后迟到的完成回调不得触发刷新")
    }
}

/// 登录窗测试替身: 记录 present 次数, 保存回调, 让测试显式模拟"用户完成/取消".
/// 回调闸门与生产 LoginWindowController 共用 (LoginCallbackGate) — fake 必须走
/// 同一个状态机, 集成层断言"取消后 complete 不生效"才有意义.
@MainActor
private final class FakeLoginWindowController: LoginWindowControlling {
    var onComplete: ((String) -> Void)?
    var onCancel: (() -> Void)?
    private(set) var presentCount = 0
    private var gate = LoginCallbackGate()

    func present() {
        // 新窗口生命周期开始: 生产上每次 renewSession 建的是新 controller, gate
        // 天然全新; fake 单例复用, 在 present 时重置闸门保持同语义
        // (取消 → 再次唤起 → 完成, 这条路径必须仍然通).
        gate = LoginCallbackGate()
        presentCount += 1
    }

    /// 模拟用户在登录窗登录成功后点「完成并提取」. 取消/完成之后到达的迟到
    /// 回调被闸门丢弃 (与生产 LoginWindowController 一致).
    func simulateComplete(_ credential: String) {
        guard gate.allowsCompletion() else { return }
        gate.markCompleted()
        onComplete?(credential)
    }

    func simulateCancel() {
        guard gate.allowsCancellation() else { return }
        gate.markCancelled()
        onCancel?()
    }
}
