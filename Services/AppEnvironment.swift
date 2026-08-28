import Foundation

/// 进程环境判定 + 存储(UserDefaults/Keychain)工厂.
///
/// 根因修复: 单元测试以 TEST_HOST 方式跑在本 app 进程里, 若直接用 .standard
/// 和真实 Keychain, 会读写用户真实配置; 且测试产物是 ad-hoc 签名, 每次构建
/// 签名都变, 读上一轮创建的 Keychain 条目会触发系统登录密码授权弹窗.
/// 测试进程统一路由到隔离 suite + 内存 Keychain; 生产进程行为不变.
enum AppEnvironment {
    /// XCTest 宿主进程特征: 测试运行器注入该环境变量, 且进程内加载了 XCTest 框架.
    static let isRunningTests: Bool = {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }()

    /// 测试进程专用 UserDefaults suite: 与真实用户配置完全隔离.
    /// 首次访问时清空持久化数据, 每轮测试从干净状态开始.
    static let testDefaults: UserDefaults = {
        let suiteName = "com.quota.statusbar.tests"
        UserDefaults().removePersistentDomain(forName: suiteName)
        return UserDefaults(suiteName: suiteName)!
    }()

    /// 测试进程共用的内存 Keychain (同一进程必须单例, 否则各处状态分裂).
    private static let testKeychain: KeychainStoring = InMemoryKeychainStore()

    static var defaults: UserDefaults {
        isRunningTests ? testDefaults : .standard
    }

    static func makeKeychain() -> KeychainStoring {
        isRunningTests ? testKeychain : KeychainStore()
    }
}
