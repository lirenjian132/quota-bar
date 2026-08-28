import Foundation

/// 平台账号实例: 同一 PlatformType 可以有多个实例 (如两个 MiniMax 账号各配各的 key).
/// id 是持久化唯一标识, 所有 per-账号 状态 (配置/启用/钉选/指标勾选) 都挂在 id 上.
/// 老版本迁移来的默认实例 id 与平台 rawValue 相同 (如 "minimax_cn"), Keychain account 因此无需迁移.
struct PlatformInstance: Codable, Hashable, Identifiable {
    let id: String
    let platformType: PlatformType
    var displayName: String

    /// 菜单/状态栏展示名: 用户命名过就显示自定义名, 否则用平台默认名.
    var displayTitle: String {
        displayName.isEmpty ? platformType.displayName : displayName
    }

    /// 生成同平台新实例的 id: "minimax_cn-2", "-3", ... 避开已占用的.
    /// 复用已删除实例的 id 会撞上残留的 Keychain 条目/状态 key, 因此同时排除
    /// UserDefaults 里所有历史前缀 (持久化过的实例即使已删也不复用).
    static func nextID(for type: PlatformType, existingIDs: Set<String>, defaults: UserDefaults = AppEnvironment.defaults) -> String {
        let usedPrefixes = Set(
            defaults.dictionaryRepresentation().keys
                .filter { $0.hasPrefix("quotabar.instance.") }
                .map { String($0.dropFirst("quotabar.instance.".count)) }
                .map { $0.components(separatedBy: ".")[0] }
        )
        var n = 2
        let taken = existingIDs.union(usedPrefixes)
        while taken.contains("\(type.rawValue)-\(n)") { n += 1 }
        return "\(type.rawValue)-\(n)"
    }
}
