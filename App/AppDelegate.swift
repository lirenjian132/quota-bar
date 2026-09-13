import AppKit
import SwiftUI

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusBarController: StatusBarController?
    var viewModel: PlatformViewModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        I18nService.shared.loadTranslations()

        // 测试宿主进程: 不跑任何启动副作用. 曾因漏了这行, 测试进程用真实 Keychain
        // 执行迁移、把 key 写进内存测试盘, 导致用户两个 key 被销毁 — 绝不能再犯.
        if AppEnvironment.isRunningTests { return }

        // 一次性把 Keychain 里的 key 搬进文件存储 (在一切 store 初始化之前),
        // 之后 app 永不再碰 Keychain. 读取旧条目可能弹最后一次授权.
        KeychainToFileMigration.run()

        viewModel = PlatformViewModel()
        guard let viewModel else { return }

        statusBarController = StatusBarController(viewModel: viewModel)
        viewModel.delegate = self
        viewModel.startAutoRefresh()
    }

    func applicationWillTerminate(_ notification: Notification) {
        viewModel?.cleanup()
    }
}

extension AppDelegate: PlatformViewModelDelegate {
    func platformViewModel(_ viewModel: PlatformViewModel, didUpdateData data: PlatformUsageData?) {
        statusBarController?.update(data: data)
    }

    func platformViewModel(_ viewModel: PlatformViewModel, didUpdateAllData allData: [String: PlatformUsageData]) {
        statusBarController?.updateAll(data: allData)
    }

    func platformViewModel(_ viewModel: PlatformViewModel, didSwitchInstance instance: PlatformInstance) {
        // Instance switched, status bar will update via didUpdateData
    }
}
