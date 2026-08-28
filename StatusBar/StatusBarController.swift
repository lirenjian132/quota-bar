import AppKit
import SwiftUI

@MainActor
class StatusBarController {
    private static let minimumItemWidth: CGFloat = 36

    private var statusItem: NSStatusItem
    private var statusBarView: RightClickStatusBarView
    private let viewModel: PlatformViewModel
    private let launchAtLoginService: LaunchAtLoginServing = LaunchAtLoginService()
    private var popover: NSPopover?
    private var clickMonitor: Any?

    // 钉选多实例: 每个钉选账号实例一个独立的 NSStatusItem, 常驻状态栏.
    private var pinnedItems: [String: NSStatusItem] = [:]
    private var pinnedViews: [String: RightClickStatusBarView] = [:]

    init(viewModel: PlatformViewModel) {
        self.viewModel = viewModel

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        let statusBarContentView = StatusBarView(
            platformData: viewModel.activePlatformData,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: viewModel.activeInstance)
        )
        statusBarView = RightClickStatusBarView(rootView: statusBarContentView)

        guard let button = statusItem.button else {
            return
        }

        button.frame.size.height = NSStatusBar.system.thickness
        statusBarView.translatesAutoresizingMaskIntoConstraints = false
        button.addSubview(statusBarView)
        NSLayoutConstraint.activate([
            statusBarView.leadingAnchor.constraint(equalTo: button.leadingAnchor),
            statusBarView.trailingAnchor.constraint(equalTo: button.trailingAnchor),
            statusBarView.topAnchor.constraint(equalTo: button.topAnchor),
            statusBarView.bottomAnchor.constraint(equalTo: button.bottomAnchor)
        ])

        statusBarView.onLeftClick = { [weak self] in
            self?.statusItemClicked()
        }

        statusBarView.onRightClick = { [weak self] in
            self?.showDisplaySettingsSubmenu(from: nil)
        }

        statusBarView.layoutSubtreeIfNeeded()
        let fittedWidth = max(
            StatusBarController.minimumItemWidth,
            ceil(statusBarView.fittingSize.width)
        )
        statusItem.length = fittedWidth

        // 钉选平台监听: 平台启用状态或钉选状态变化时重建 item
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handlePlatformChanged),
            name: .platformEnabledChanged,
            object: nil
        )

        // 启用 metric 变化: 重绘所有 status item (主 item + 所有 pinned item)
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleEnabledMetricsChanged(_:)),
            name: .enabledMetricsChanged,
            object: nil
        )

        // 实例顺序变化 (左移/右移): 全拆钉选块按新顺序重建
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleInstancesReordered),
            name: .platformInstancesReordered,
            object: nil
        )

        // 初始化时构建钉选 item
        rebuildPinnedItems()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    // MARK: - Click Handling

    private func setupClickMonitor() {
        removeClickMonitor()
        clickMonitor = NSEvent.addGlobalMonitorForEvents(matching: .leftMouseDown) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.closePopoverIfNeeded()
            }
        }
    }

    private func removeClickMonitor() {
        if let monitor = clickMonitor {
            NSEvent.removeMonitor(monitor)
            clickMonitor = nil
        }
    }

    private func closePopoverIfNeeded() {
        guard let popover = popover, popover.isShown else { return }
        popover.performClose(nil)
        self.popover = nil
        removeClickMonitor()
    }

    private func statusItemClicked(from button: NSStatusBarButton? = nil) {
        // 优先用传入的 button, 否则用主 statusItem 的; 钉选模式下找第一个可见的 pinned item
        let targetButton = button
            ?? statusItem.button
            ?? pinnedItems.values.compactMap { $0.isVisible ? $0.button : nil }.first
        guard let targetButton else { return }

        if let existingPopover = popover, existingPopover.isShown {
            existingPopover.performClose(nil)
            popover = nil
            removeClickMonitor()
            return
        }

        let popoverContentView = PopoverContentView(viewModel: viewModel)
        let hostingController = NSHostingController(rootView: popoverContentView)
        let newPopover = NSPopover()
        newPopover.contentViewController = hostingController
        newPopover.behavior = .applicationDefined
        newPopover.show(relativeTo: targetButton.bounds, of: targetButton, preferredEdge: .minY)

        DispatchQueue.main.async {
            hostingController.view.window?.makeFirstResponder(hostingController.view)
        }

        popover = newPopover
        setupClickMonitor()
    }

    // MARK: - Right Click Menu

    private func showDisplaySettingsSubmenu(from item: NSStatusItem? = nil) {
        closePopoverIfNeeded()

        // Display Settings submenu
        let displayMenu = NSMenu()

        let usedItem = NSMenuItem(title: I18nService.shared.translate("menu.showUsed"), action: #selector(setDisplayModeUsed), keyEquivalent: "")
        usedItem.target = self
        usedItem.state = ConfigService.shared.displayMode == .used ? .on : .off
        displayMenu.addItem(usedItem)

        let remainingItem = NSMenuItem(title: I18nService.shared.translate("menu.showRemaining"), action: #selector(setDisplayModeRemaining), keyEquivalent: "")
        remainingItem.target = self
        remainingItem.state = ConfigService.shared.displayMode == .remaining ? .on : .off
        displayMenu.addItem(remainingItem)

        let displaySettingsItem = NSMenuItem(title: I18nService.shared.translate("menu.displaySettings"), action: nil, keyEquivalent: "")
        displaySettingsItem.submenu = displayMenu

        // Refresh Interval submenu
        let refreshMenu = NSMenu()
        for interval in RefreshInterval.allCases {
            let item = NSMenuItem(
                title: I18nService.shared.translate(interval.i18nKey),
                action: #selector(setRefreshInterval(_:)),
                keyEquivalent: ""
            )
            item.target = self
            item.representedObject = interval.rawValue
            item.state = interval == ConfigService.shared.refreshInterval ? .on : .off
            refreshMenu.addItem(item)
        }
        let refreshItem = NSMenuItem(title: I18nService.shared.translate("menu.refreshInterval"), action: nil, keyEquivalent: "")
        refreshItem.submenu = refreshMenu

        let launchAtLoginItem = NSMenuItem(
            title: I18nService.shared.translate("menu.launchAtLogin"),
            action: #selector(toggleLaunchAtLogin(_:)),
            keyEquivalent: ""
        )
        launchAtLoginItem.target = self
        launchAtLoginItem.state = launchAtLoginService.isEnabled ? .on : .off

        // Platform Enable/Disable submenu
        let platformMenu = NSMenu()

        // 每个平台一个子菜单, 含「启用」+「固定到状态栏」两个选项
        // 每个账号实例一个子菜单, 含「启用」+「固定到状态栏」两个选项.
        // representedObject 用 instance.id (String), 避免 Swift enum 经 ObjC 桥接后解不回.
        let allInstances = PlatformInstanceStore.shared.instances
        for (index, instance) in allInstances.enumerated() {
            let platSubmenu = NSMenu()

            let enableItem = NSMenuItem(
                title: I18nService.shared.translate("menu.platformEnabled"),
                action: #selector(togglePlatformEnabled(_:)),
                keyEquivalent: ""
            )
            enableItem.target = self
            enableItem.representedObject = instance.id
            enableItem.state = instance.isEnabled ? .on : .off
            platSubmenu.addItem(enableItem)

            let pinItem = NSMenuItem(
                title: I18nService.shared.translate("menu.pinToStatusBar"),
                action: #selector(togglePlatformPinned(_:)),
                keyEquivalent: ""
            )
            pinItem.target = self
            pinItem.representedObject = instance.id
            pinItem.state = instance.isPinned ? .on : .off
            // 未启用的实例不能钉选
            pinItem.isEnabled = instance.isEnabled
            platSubmenu.addItem(pinItem)

            // 排序: 左移/右移 (首个不可再左移, 末个不可再右移)
            let moveLeftItem = NSMenuItem(
                title: I18nService.shared.translate("menu.moveLeft"),
                action: #selector(moveInstanceAction(_:)),
                keyEquivalent: ""
            )
            moveLeftItem.target = self
            moveLeftItem.representedObject = ["id": instance.id, "offset": -1]
            moveLeftItem.isEnabled = index > 0
            platSubmenu.addItem(moveLeftItem)

            let moveRightItem = NSMenuItem(
                title: I18nService.shared.translate("menu.moveRight"),
                action: #selector(moveInstanceAction(_:)),
                keyEquivalent: ""
            )
            moveRightItem.target = self
            moveRightItem.representedObject = ["id": instance.id, "offset": 1]
            moveRightItem.isEnabled = index < allInstances.count - 1
            platSubmenu.addItem(moveRightItem)

            platSubmenu.addItem(NSMenuItem.separator())

            let renameItem = NSMenuItem(
                title: I18nService.shared.translate("menu.renameAccount"),
                action: #selector(renameAccountAction(_:)),
                keyEquivalent: ""
            )
            renameItem.target = self
            renameItem.representedObject = instance.id
            platSubmenu.addItem(renameItem)

            let deleteItem = NSMenuItem(
                title: I18nService.shared.translate("menu.deleteAccount"),
                action: #selector(deleteAccountAction(_:)),
                keyEquivalent: ""
            )
            deleteItem.target = self
            deleteItem.representedObject = instance.id
            // 最后一个启用的实例不能删 (删完全 app 就没有可显示账号了)
            deleteItem.isEnabled = !(instance.isEnabled && PlatformManager.shared.isLastEnabledInstance(instance))
            platSubmenu.addItem(deleteItem)

            let platItem = NSMenuItem(title: instance.displayTitle, action: nil, keyEquivalent: "")
            platItem.submenu = platSubmenu
            platformMenu.addItem(platItem)
        }

        platformMenu.addItem(NSMenuItem.separator())
        for type in PlatformType.allCases {
            let addItem = NSMenuItem(
                title: I18nService.shared.translate("menu.addAccount.\(type.rawValue)"),
                action: #selector(addAccountAction(_:)),
                keyEquivalent: ""
            )
            addItem.target = self
            addItem.representedObject = type.rawValue
            platformMenu.addItem(addItem)
        }
        platformMenu.addItem(NSMenuItem.separator())
        let configureItem = NSMenuItem(title: I18nService.shared.translate("menu.configurePlatform"), action: #selector(showConfigMenu), keyEquivalent: "")
        configureItem.target = self
        platformMenu.addItem(configureItem)

        let platformItem = NSMenuItem(title: I18nService.shared.translate("menu.platforms"), action: nil, keyEquivalent: "")
        platformItem.submenu = platformMenu

        // Enabled Metrics submenu: 每个平台一个子菜单, 含该平台所有可显示指标的多选.
        // 已勾 2 个时第 3 个菜单项禁用, 不让超过上限.
        // 注意: representedObject 一律用 instance.id (String) — Swift enum 经 ObjC
        // 桥接会包成 NSObject wrapper, 取时 as? 反解会失败.
        let enabledMetricsMenu = NSMenu()
        let availableLabels = ["five_hour", "weekly_limit", "mcp_monthly"]
        for instance in PlatformInstanceStore.shared.instances {
            let platSub = NSMenu()
            let current = ConfigService.shared.enabledMetrics(for: instance)
            for label in availableLabels {
                let item = NSMenuItem(
                    title: I18nService.shared.translate("menu.metric.\(label)"),
                    action: #selector(toggleEnabledMetric(_:)),
                    keyEquivalent: ""
                )
                item.target = self
                // 用 String 字典, 避免 Swift enum bridge 问题
                item.representedObject = ["instanceID": instance.id, "label": label]
                let checked = current.contains(label)
                let atLimit = current.count >= 2 && !checked
                item.state = checked ? .on : .off
                item.isEnabled = !atLimit
                platSub.addItem(item)
            }
            let platTitleItem = NSMenuItem(title: instance.displayTitle, action: nil, keyEquivalent: "")
            platTitleItem.submenu = platSub
            enabledMetricsMenu.addItem(platTitleItem)
        }
        let enabledMetricsItem = NSMenuItem(title: I18nService.shared.translate("menu.enabledMetrics"), action: nil, keyEquivalent: "")
        enabledMetricsItem.submenu = enabledMetricsMenu

        // Language submenu
        let languageMenu = NSMenu()
        let isEnglish = I18nService.shared.currentLocale == "en"

        let englishItem = NSMenuItem(title: I18nService.shared.translate("menu.lang.en"), action: #selector(setLanguageEnglish), keyEquivalent: "")
        englishItem.target = self
        englishItem.state = isEnglish ? .on : .off
        languageMenu.addItem(englishItem)

        let chineseItem = NSMenuItem(title: I18nService.shared.translate("menu.lang.zh"), action: #selector(setLanguageChinese), keyEquivalent: "")
        chineseItem.target = self
        chineseItem.state = !isEnglish ? .on : .off
        languageMenu.addItem(chineseItem)

        let languageItem = NSMenuItem(title: I18nService.shared.translate("menu.language"), action: nil, keyEquivalent: "")
        languageItem.submenu = languageMenu

        // Root menu
        let rootMenu = NSMenu()
        // 立即刷新: 清所有平台缓存(token/usage)重新拉取, 平台偶发卡住时一键自愈.
        let refreshNowItem = NSMenuItem(title: I18nService.shared.translate("menu.refreshNow"), action: #selector(refreshAllNow), keyEquivalent: "")
        refreshNowItem.target = self
        rootMenu.addItem(refreshNowItem)
        rootMenu.addItem(NSMenuItem.separator())
        rootMenu.addItem(displaySettingsItem)
        rootMenu.addItem(refreshItem)
        rootMenu.addItem(platformItem)
        rootMenu.addItem(enabledMetricsItem)
        rootMenu.addItem(languageItem)
        rootMenu.addItem(launchAtLoginItem)
        rootMenu.addItem(NSMenuItem.separator())

        let aboutItem = NSMenuItem(
            title: I18nService.shared.translate("menu.about"),
            action: #selector(showAbout),
            keyEquivalent: ""
        )
        aboutItem.target = self
        rootMenu.addItem(aboutItem)

        rootMenu.addItem(NSMenuItem.separator())

        let checkUpdateItem = NSMenuItem(
            title: titleForUpdateState(),
            action: #selector(checkUpdateAction),
            keyEquivalent: "u"
        )
        checkUpdateItem.keyEquivalentModifierMask = .command
        checkUpdateItem.target = self
        checkUpdateItem.isEnabled = UpdateService.shared.canCheckForUpdates
        checkUpdateItem.toolTip = tooltipForUpdateState()
        rootMenu.addItem(checkUpdateItem)

        let openReleasesItem = NSMenuItem(
            title: I18nService.shared.translate("update.openReleases"),
            action: #selector(openReleasesAction),
            keyEquivalent: ""
        )
        openReleasesItem.target = self
        rootMenu.addItem(openReleasesItem)

        rootMenu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(title: I18nService.shared.translate("menu.quit"), action: #selector(quitAction), keyEquivalent: "q")
        quitItem.target = self
        rootMenu.addItem(quitItem)

        // 弹出菜单: 优先用传入的 item, 否则用主 statusItem.
        // 钉选模式下主 statusItem 被隐藏, 找第一个 pinned item 来弹.
        let targetItem = item ?? statusItem
        if targetItem.isVisible || item != nil {
            targetItem.menu = rootMenu
            targetItem.button?.performClick(nil)
            targetItem.menu = nil
        } else {
            // 主 item 隐藏了, 用第一个 pinned item
            if let firstPinned = pinnedItems.values.first(where: { $0.isVisible }) {
                firstPinned.menu = rootMenu
                firstPinned.button?.performClick(nil)
                firstPinned.menu = nil
            }
        }
    }

    // MARK: - Actions

    @objc private func togglePlatformEnabled(_ sender: NSMenuItem) {
        guard let instanceID = sender.representedObject as? String,
              var instance = PlatformInstanceStore.shared.instance(id: instanceID) else { return }
        let newState = !instance.isEnabled
        // 禁用实例前先取消钉选 (必须在 setPlatformEnabled 之前, 因为它同步发通知触发 rebuildPinnedItems)
        if !newState && instance.isPinned {
            instance.isPinned = false
        }
        PlatformManager.shared.setPlatformEnabled(newState, for: instance)
        sender.state = newState ? .on : .off
    }

    @objc private func togglePlatformPinned(_ sender: NSMenuItem) {
        guard let instanceID = sender.representedObject as? String,
              var instance = PlatformInstanceStore.shared.instance(id: instanceID) else { return }
        let newState = !instance.isPinned
        instance.isPinned = newState
        sender.state = newState ? .on : .off
        // 发通知触发 rebuildPinnedItems
        NotificationCenter.default.post(name: .platformEnabledChanged, object: nil)
    }

    @objc private func showConfigMenu() {
        viewModel.configureAPIKey(for: viewModel.activeInstance)
        statusItemClicked()
    }

    // 添加账号: 创建实例并直接弹出配置面板, 取消且未填 key 会自动回收
    @objc private func addAccountAction(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let type = PlatformType(rawValue: raw) else { return }
        viewModel.addInstance(of: type)
        statusItemClicked()
    }

    @objc private func renameAccountAction(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let instance = PlatformInstanceStore.shared.instance(id: id) else { return }

        let alert = NSAlert()
        alert.messageText = I18nService.shared.translate("menu.renameAccount.title")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.stringValue = instance.displayName
        alert.accessoryView = field
        alert.addButton(withTitle: I18nService.shared.translate("menu.about.ok"))
        alert.addButton(withTitle: I18nService.shared.translate("common.cancel"))
        alert.window.initialFirstResponder = field

        if alert.runModal() == .alertFirstButtonReturn {
            viewModel.renameInstance(instance, to: field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }

    @objc private func deleteAccountAction(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let instance = PlatformInstanceStore.shared.instance(id: id) else { return }

        let alert = NSAlert()
        alert.messageText = String(
            format: I18nService.shared.translate("menu.deleteAccount.confirm"),
            instance.displayTitle
        )
        alert.informativeText = I18nService.shared.translate("menu.deleteAccount.informative")
        alert.alertStyle = .warning
        alert.addButton(withTitle: I18nService.shared.translate("menu.deleteAccount.confirmOk"))
        alert.addButton(withTitle: I18nService.shared.translate("common.cancel"))

        if alert.runModal() == .alertFirstButtonReturn {
            viewModel.removeInstance(instance)
        }
    }

    // 左移/右移账号: 通知触发钉选块按新顺序全量重建
    @objc private func moveInstanceAction(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any],
              let id = payload["id"] as? String,
              let offset = payload["offset"] as? Int else { return }
        PlatformInstanceStore.shared.moveInstance(id: id, offset: offset)
    }

    @objc private func setDisplayModeUsed() {
        ConfigService.shared.displayMode = .used
        updateStatusBarView()
    }

    @objc private func setDisplayModeRemaining() {
        ConfigService.shared.displayMode = .remaining
        updateStatusBarView()
    }

    @objc private func toggleEnabledMetric(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any],
              let instanceID = payload["instanceID"] as? String,
              let instance = PlatformInstanceStore.shared.instance(id: instanceID),
              let label = payload["label"] as? String else { return }

        var current = ConfigService.shared.enabledMetrics(for: instance)
        if current.contains(label) {
            current.removeAll { $0 == label }
        } else {
            current.append(label)
        }
        ConfigService.shared.setEnabledMetrics(current, for: instance)
        // 通知已由 setter 发, 无需再手动 post.
        // 同步更新菜单项状态 (因为菜单已弹出, 不会重新构造).
        let atLimit = current.count >= 2
        for item in sender.menu?.items ?? [] {
            guard let p = item.representedObject as? [String: Any],
                  let l = p["label"] as? String else { continue }
            let isCurrent = current.contains(l)
            item.state = isCurrent ? .on : .off
            item.isEnabled = !(atLimit && !isCurrent)
        }
    }

    @objc private func setRefreshInterval(_ sender: NSMenuItem) {
        guard let rawValue = sender.representedObject as? String,
              let interval = RefreshInterval(rawValue: rawValue) else { return }
        ConfigService.shared.refreshInterval = interval
        viewModel.restartAutoRefresh()
    }

    // 一键自愈: 清掉所有平台的 token/usage 缓存并立即重新拉取.
    // 某平台因 token 过期/网络偶发卡住显示异常时, 右键点这个即可恢复.
    // 先停定时刷新, 避免定时触发的 fetchAllUsage cancel 掉这次手动拉取 (cancel 后
    // 结果会被 fetchAllUsage 的 Task.isCancelled 丢弃, 表现为"刷新没反应").
    @objc private func refreshAllNow() {
        PlatformManager.shared.clearAllCaches()
        viewModel.stopAutoRefresh()
        Task { @MainActor [weak self] in
            await self?.viewModel.fetchAllUsage()
            self?.viewModel.startAutoRefresh()
        }
    }

    @objc private func toggleLaunchAtLogin(_ sender: NSMenuItem) {
        let enable = sender.state != .on
        do {
            try launchAtLoginService.setEnabled(enable)
        } catch {
            let alert = NSAlert()
            alert.messageText = I18nService.shared.translate("menu.launchAtLogin")
            alert.informativeText = I18nService.shared.translate("menu.launchAtLogin.failed")
            alert.alertStyle = .warning
            alert.addButton(withTitle: I18nService.shared.translate("menu.about.ok"))
            alert.runModal()
        }
        // Clear menu so next right-click rebuilds from system status
        statusItem.menu = nil
    }

    @objc private func setLanguageEnglish() {
        I18nService.shared.setLocale("en")
        updateStatusBarView()
    }

    @objc private func setLanguageChinese() {
        I18nService.shared.setLocale("zh-Hans")
        updateStatusBarView()
    }

    @objc private func checkUpdateAction() {
        UpdateService.shared.checkForUpdates()
    }

    @objc private func openReleasesAction() {
        UpdateService.shared.openReleasesPage()
    }

    @objc private func showAbout() {
        let alert = NSAlert()
        alert.messageText = "QuotaBar"
        alert.informativeText = String(
            format: I18nService.shared.translate("menu.about.version"),
            AppVersion.marketingVersion,
            Calendar.current.component(.year, from: Date())
        )
        alert.alertStyle = .informational
        alert.addButton(withTitle: I18nService.shared.translate("menu.about.ok"))
        alert.addButton(withTitle: I18nService.shared.translate("menu.about.openReleases"))
        alert.icon = NSImage(named: NSImage.applicationIconName)

        let response = alert.runModal()
        if response == .alertSecondButtonReturn {
            UpdateService.shared.openReleasesPage()
        }
    }

    /// 根据 UpdateService.State 返回菜单项 title, 状态文案优先, idle 状态用通用文案
    private func titleForUpdateState() -> String {
        let key: String
        switch UpdateService.shared.state {
        case .idle:
            return I18nService.shared.translate("menu.checkUpdate")
        case .checking:
            key = "update.checking"
        case .upToDate:
            key = "update.upToDate"
        case .updateAvailable:
            key = "update.updateAvailable"
        case .failed:
            key = "update.checkFailed"
        }
        return I18nService.shared.translate(key)
    }

    /// "Last checked: 2 hours ago" — i18n 模板 + Date.formatted relative 渲染
    private func tooltipForUpdateState() -> String? {
        guard let date = UpdateService.shared.lastCheckDate else { return nil }
        let template = I18nService.shared.translate("update.lastChecked")
        let relative = date.formatted(.relative(presentation: .named))
        return String(format: template, relative)
    }

    @objc private func quitAction() {
        NSApplication.shared.terminate(nil)
    }

    // MARK: - Update

    private func updateStatusBarView() {
        let active = viewModel.activeInstance
        statusBarView.update(rootView: StatusBarView(
            platformData: viewModel.activePlatformData,
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: active)
        ))
        statusBarView.layoutSubtreeIfNeeded()
    }

    // 单平台数据更新 (兼容旧 delegate 回调).
    func update(data: PlatformUsageData?) {
        updateAll(data: viewModel.platformData)
    }

    // 全量更新: 同时刷新主 item 和所有钉选 item.
    // allData 是所有平台的数据字典.
    func updateAll(data allData: [String: PlatformUsageData]) {
        let pinned = PlatformInstance.allPinned

        // 有钉选实例: 主 item 隐藏, 只用 pinned items 显示
        if !pinned.isEmpty {
            statusItem.length = 0
            statusItem.isVisible = false

            for instance in pinned {
                updatePinnedItem(instance, data: allData[instance.id])
            }
            return
        }

        // 没有钉选实例: 主 item 显示 activeInstance (原有行为)
        statusItem.isVisible = true
        statusBarView.update(rootView: StatusBarView(
            platformData: viewModel.activePlatformData,
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: viewModel.activeInstance)
        ))
        statusBarView.layoutSubtreeIfNeeded()
        statusItem.length = max(
            StatusBarController.minimumItemWidth,
            ceil(statusBarView.fittingSize.width)
        )
    }

    // 更新单个钉选 item 的内容.
    private func updatePinnedItem(_ instance: PlatformInstance, data: PlatformUsageData?) {
        guard let view = pinnedViews[instance.id] else { return }

        view.update(rootView: StatusBarView(
            platformData: data,
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: instance)
        ))
        view.layoutSubtreeIfNeeded()

        if let item = pinnedItems[instance.id] {
            item.length = max(
                StatusBarController.minimumItemWidth,
                ceil(view.fittingSize.width)
            )
        }
    }

    // MARK: - Pinned Items Management

    // 平台启用/钉选状态变化时, 重建钉选 item 列表.
    // NSStatusItem 的左右位置由创建顺序决定, 增量 rebuild 挪不动已存在的 item,
    // 顺序变化必须全拆重建.
    @objc private func handleInstancesReordered() {
        pinnedItems.values.forEach { NSStatusBar.system.removeStatusItem($0) }
        pinnedItems.removeAll()
        pinnedViews.removeAll()
        rebuildPinnedItems()
        updateAll(data: viewModel.platformData)
    }

    @objc private func handlePlatformChanged() {
        rebuildPinnedItems()
        updateAll(data: viewModel.platformData)
        // 主动拉取刚钉选但还没有数据的实例, 避免新 pin 的块一直显示 "--"
        for instance in PlatformInstance.allPinned where viewModel.platformData[instance.id] == nil {
            viewModel.fetchUsage(for: instance)
        }
    }

    // 启用 metric 变化: 通知的 object 是 instance id (String, 来自 setter).
    // 简化: 直接重绘全部 status item, 避免按平台分支.
    @objc private func handleEnabledMetricsChanged(_ note: Notification) {
        updateAll(data: viewModel.platformData)
    }

    // 根据当前 isPinned 状态, 增删 NSStatusItem.
    private func rebuildPinnedItems() {
        let pinned = Set(PlatformInstance.allPinned.map(\.id))
        let existing = Set(pinnedItems.keys)

        // 移除不再钉选的
        for id in existing.subtracting(pinned) {
            if let item = pinnedItems.removeValue(forKey: id) {
                NSStatusBar.system.removeStatusItem(item)
            }
            pinnedViews.removeValue(forKey: id)
        }

        // 新增刚钉选的
        for instance in PlatformInstance.allPinned where !existing.contains(instance.id) {
            createPinnedItem(for: instance)
        }
    }

    private func createPinnedItem(for instance: PlatformInstance) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

        let view = RightClickStatusBarView(rootView: StatusBarView(
            platformData: viewModel.platformData[instance.id],
            displayMode: ConfigService.shared.displayMode,
            enabledMetrics: ConfigService.shared.enabledMetrics(for: instance)
        ))

        guard let button = item.button else { return }
        button.frame.size.height = NSStatusBar.system.thickness
        view.translatesAutoresizingMaskIntoConstraints = false
        button.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: button.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: button.trailingAnchor),
            view.topAnchor.constraint(equalTo: button.topAnchor),
            view.bottomAnchor.constraint(equalTo: button.bottomAnchor)
        ])

        // 点击任意一块都打开弹出面板, 传入该 item 自己的 button 用于 popover 定位
        view.onLeftClick = { [weak self, weak item] in
            self?.statusItemClicked(from: item?.button)
        }
        // 右键菜单: 用被点击的 item 自己的 button 弹出菜单
        view.onRightClick = { [weak self, weak item] in
            self?.showDisplaySettingsSubmenu(from: item)
        }

        view.layoutSubtreeIfNeeded()
        item.length = max(
            StatusBarController.minimumItemWidth,
            ceil(view.fittingSize.width)
        )

        pinnedItems[instance.id] = item
        pinnedViews[instance.id] = view
    }
}
