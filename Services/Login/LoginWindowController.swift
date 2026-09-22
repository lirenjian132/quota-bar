import AppKit
import WebKit

/// 登录窗抽象: 生产用 LoginWindowController, 测试注入 fake 验证回调链路
/// (不弹真 WebKit 窗口, 不在测试进程里发网页请求).
@MainActor
protocol LoginWindowControlling: AnyObject {
    /// 用户点「完成并提取」且 cookie 抓取成功: 回传拼好的凭据串.
    var onComplete: ((String) -> Void)? { get set }
    /// 取消 / 直接关窗.
    var onCancel: (() -> Void)? { get set }
    func present()
}

/// 登录回调闸门 (R-6): onComplete / onCancel 只允许第一次生效, 之后到达的回调
/// (用户取消后 getAllCookies 才异步返回、关窗重复触发) 一律丢弃 — 迟到的完成
/// 回调不得在取消之后写凭据.
///
/// LoginWindowController (生产) 与 FakeLoginWindowController (测试替身) 共用
/// 同一状态机, 单测可直接钉住转换规则.
struct LoginCallbackGate {
    private(set) var didComplete = false
    private(set) var didCancel = false

    /// 完成回调可生效: 既未完成也未取消.
    func allowsCompletion() -> Bool { !didComplete && !didCancel }

    /// 取消回调可生效: 既未完成也未取消.
    func allowsCancellation() -> Bool { !didComplete && !didCancel }

    mutating func markCompleted() { didComplete = true }
    mutating func markCancelled() { didCancel = true }
}

/// 一键续期登录窗: WKWebView 加载平台登录页, 用户手动登录 (可能过滑块/验证码,
/// 纯展示用户操作, 不作任何自动化), 点「完成并提取」时从 webView 自身
/// nonPersistent dataStore 的 cookie store 抓取约定 cookie 拼成凭据串回调.
///
/// 为什么不能自动判定登录完成: WebKit 没有"登录成功"信号 (登录是业务接口,
/// 页面跳转/cookie 落库都无法区分"已登录"与"还在登录页"), 自动判定会把没登录
/// 当成功写坏凭据. 完成动作必须由用户手动触发.
@MainActor
final class LoginWindowController: NSWindowController, NSWindowDelegate, LoginWindowControlling {
    var onComplete: ((String) -> Void)?
    var onCancel: (() -> Void)?

    private let platformType: PlatformType
    private let platform: WebLoginPlatform
    private let webView: WKWebView
    private let completeButton: NSButton
    /// 回调闸门 (R-6): 完成/取消任一发生过后, 迟到的回调一律忽略.
    private var gate = LoginCallbackGate()

    init(platformType: PlatformType, platform: WebLoginPlatform) {
        self.platformType = platformType
        self.platform = platform

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 700),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        // nonPersistent dataStore: 登录 cookie 只存活本次会话 — 关窗即清, 不跨
        // 会话落盘, 与 app 其它网页功能的默认 store 完全隔离. 「完成并提取」在
        // 用户点击那一刻从同一 store 同步读取 (见 completeTapped), 非持久化
        // 不影响提取 — 它活不到"关窗之后"被需要.
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        webView = WKWebView(frame: .zero, configuration: configuration)
        completeButton = NSButton()

        super.init(window: window)

        window.title = String(format: I18nService.shared.translate("login.title"), platformType.displayName)
        window.delegate = self
        setupContent(in: window.contentView ?? NSView())
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    // MARK: - Layout

    private func setupContent(in contentView: NSView) {
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.load(URLRequest(url: platform.loginURL))
        contentView.addSubview(webView)

        let bar = NSView()
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.windowBackgroundColor.withAlphaComponent(0.9).cgColor
        contentView.addSubview(bar)

        let hintLabel = NSTextField(labelWithString: I18nService.shared.translate("login.hint"))
        hintLabel.font = .systemFont(ofSize: 11)
        hintLabel.textColor = .secondaryLabelColor
        hintLabel.lineBreakMode = .byWordWrapping
        hintLabel.maximumNumberOfLines = 2

        let cancelButton = NSButton(title: I18nService.shared.translate("common.cancel"), target: self, action: #selector(cancelTapped))
        completeButton.title = I18nService.shared.translate("login.complete")
        completeButton.target = self
        completeButton.action = #selector(completeTapped)
        // 回车 = 完成并提取 (默认按钮), 登录页输完密码直接回车即可提交.
        completeButton.keyEquivalent = "\r"

        let buttonRow = NSStackView(views: [NSView(), cancelButton, completeButton])
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 8

        let barStack = NSStackView(views: [hintLabel, buttonRow])
        barStack.orientation = .vertical
        barStack.spacing = 6
        barStack.translatesAutoresizingMaskIntoConstraints = false
        bar.addSubview(barStack)

        NSLayoutConstraint.activate([
            bar.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            bar.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            bar.heightAnchor.constraint(equalToConstant: 64),

            barStack.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 12),
            barStack.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -12),
            barStack.topAnchor.constraint(equalTo: bar.topAnchor, constant: 10),
            barStack.bottomAnchor.constraint(equalTo: bar.bottomAnchor, constant: -10),

            webView.topAnchor.constraint(equalTo: contentView.topAnchor),
            webView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: bar.topAnchor)
        ])
    }

    // MARK: - Present

    func present() {
        guard let window else { return }
        window.center()
        window.makeKeyAndOrderFront(nil)
        // 菜单栏 app (LSUIElement) 默认不在前台: 登录窗要抢焦点, 否则弹了也看不见.
        NSRunningApplication.current.activate(options: [.activateIgnoringOtherApps, .activateAllWindows])
    }

    // MARK: - Actions

    @objc private func completeTapped() {
        completeButton.isEnabled = false
        // getAllCookies 从 webView 自身的 nonPersistent store 取 (与加载登录页的
        // 是同一个 store), 不是 WKWebsiteDataStore.default() — 隔离 + 不落盘.
        // 覆盖整个 store (含页面种下的所有 cookie): 按名字精确匹配取用, 多余的不管.
        webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { [weak self] cookies in
            DispatchQueue.main.async {
                guard let self else { return }
                // 迟到回调闸门 (R-6): 已完成 / 已取消 / 已关窗后 getAllCookies 才
                // 返回的, 一律丢弃 — 不得在取消之后写凭据.
                guard self.gate.allowsCompletion() else { return }
                self.completeButton.isEnabled = true
                var dict: [String: String] = [:]
                for cookie in cookies {
                    dict[cookie.name] = cookie.value
                }
                if let credential = WebLoginRenewalConfig.credentialString(from: dict, platform: self.platform) {
                    self.gate.markCompleted()
                    self.onComplete?(credential)
                    self.window?.close()
                } else {
                    // 没登录完 (cookie 还没种上): 不关窗, 提示用户继续登录.
                    // 不 markCompleted — 用户可继续登录后再点一次「完成并提取」.
                    self.presentMissingCredentialAlert()
                }
            }
        }
    }

    @objc private func cancelTapped() {
        // 先置位再关窗: windowWillClose 里 allowsCancellation() 已为 false,
        // 不会重复触发 onCancel.
        gate.markCancelled()
        window?.close()
    }

    private func presentMissingCredentialAlert() {
        guard let window else { return }
        let alert = NSAlert()
        alert.messageText = I18nService.shared.translate("login.missing.title")
        alert.informativeText = I18nService.shared.translate("login.missing.message")
        alert.alertStyle = .warning
        alert.addButton(withTitle: I18nService.shared.translate("menu.about.ok"))
        alert.beginSheetModal(for: window, completionHandler: nil)
    }

    // MARK: - NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        // 直接关窗 (红按钮, 非 cancelTapped 路径) = 取消: 置位 didCancel 防迟到
        // 回调写凭据. 已完成 (onComplete 已回调) 时不再触发 onCancel; cancelTapped
        // 已置位过时这里 allowsCancellation() 为 false, 不会重复触发.
        guard gate.allowsCancellation() else { return }
        gate.markCancelled()
        onCancel?()
    }
}

/// 装配入口: 平台类型 → 续期配置 → 建窗. 不支持的平台 (无 WebLoginRenewal 契约,
/// 如 MiniMax/GLM) 返回 nil, 调用方静默 no-op.
@MainActor
enum LoginWindowCoordinator {
    static func makeController(for type: PlatformType) -> LoginWindowController? {
        guard let platform = WebLoginRenewalConfig.platform(for: type) else { return nil }
        return LoginWindowController(platformType: type, platform: platform)
    }
}
