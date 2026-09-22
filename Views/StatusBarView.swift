import SwiftUI

/// 把渲染逻辑抽成 pure helper, 让单测可以脱离 SwiftUI runtime 直接验证过滤 / 格式.
enum StatusBarViewHelper {
    /// 状态点颜色判定顺序 (P2-8, miniMax/DeepSeek Round 2):
    ///   nil 数据 → secondary; 空 metrics → secondary (无数据可判, 不谎报红);
    ///   isHealthy=false → red (与弹窗口径一致);
    ///   健康时才按第一个 metric 的百分比/到期阈值走红黄绿.
    /// GLM 空 limits (isHealthy=false) 时空 metrics 必须先兜底, 否则状态栏红而弹窗灰.
    static func statusColor(for data: PlatformUsageData?) -> Color {
        guard let data else { return .secondary }
        if data.metrics.isEmpty { return .secondary }
        // 失效判定对所有平台优先: Stepfun 订阅停用时 credits 余量仍有 96%,
        // 旧逻辑在"百分比还够看"的分支里直接返回绿色, 与弹窗口径矛盾.
        // 这里统一为 isHealthy=false → 红 (与各 service 的 isHealthy 语义一致).
        guard data.isHealthy else { return .red }
        if let metric = data.metrics.first, metric.unit != "unlimited" {
            if let total = metric.totalValue, total > 0 {
                let remainingRatio = metric.currentValue / total
                if remainingRatio < 0.1 { return .red }
                if remainingRatio < 0.5 { return .yellow }
                return .green
            }
            if let expiry = metric.resetTime {
                let days = expiry.timeIntervalSinceNow / 86400
                if days < 3 { return .red }
                if days < 7 { return .yellow }
            }
        }
        // 走到这里 isHealthy 必为 true (guard 已挡住不健康), 三元判断是死代码.
        return .green
    }

    /// 按 enabledLabels 过滤并排序 metrics. nil 表示"不过滤", 兼容老调用.
    /// 顺序: 严格按 enabledLabels 给的顺序; enabledLabels 里没有的 metric 会被丢弃.
    /// 同族匹配 (A4-2): MiniMax 周额度三态 (标准 / 加成 / 无限) 在 service 侧互斥
    /// 产出, 用户勾选时勾的是族名 weekly_limit; 套餐切换 (加成 / ∞) 后产出的
    /// boosted/unlimited label 精确匹配不到 → 过滤空 → prefix(2) 防呆把用户没勾的
    /// five_hour 复活. 匹配前先把族名展开为族内全部 label, 渲染取产出的实际 label.
    /// 防呆 (R3-3): 同族匹配后仍无交集时 (如默认勾 credits 但实际是非 credit 套餐)
    /// 回退显示前 2 个 metric, 不让状态栏恒显 "--" 逼用户去翻菜单; 有交集时行为不变.
    static func visibleMetrics(from metrics: [UsageMetric], enabledLabels: [String]?) -> [UsageMetric] {
        guard let enabledLabels else { return metrics }
        let byLabel = Dictionary(uniqueKeysWithValues: metrics.map { ($0.label, $0) })
        // 同族展开 + 去重 (保序): 勾族名 weekly_limit 能匹配 boosted/unlimited 产出;
        // 勾族内某个具体 label 时同样整族展开 (套餐切换后仍显示周额度, 不回退).
        var expanded: [String] = []
        for label in enabledLabels {
            for candidate in Self.familyMembers(of: label) where !expanded.contains(candidate) {
                expanded.append(candidate)
            }
        }
        let filtered = expanded.compactMap { byLabel[$0] }
        if filtered.isEmpty && !metrics.isEmpty {
            return Array(metrics.prefix(2))
        }
        return filtered
    }

    /// label 的同族成员: 族名 (weekly_limit) 与族内具体 label (boosted/unlimited)
    /// 都映射到整族; 非族名单个 label 映射到自身.
    private static func familyMembers(of label: String) -> [String] {
        if let members = labelFamilies[label] { return members }
        for members in labelFamilies.values where members.contains(label) { return members }
        return [label]
    }

    /// 同族 label 表: 同一底层指标因账户套餐不同产出不同 label. 目前只有
    /// MiniMax 周额度三态; 新平台若有类似互斥多态, 在此登记.
    private static let labelFamilies: [String: [String]] = [
        "weekly_limit": ["weekly_limit", "weekly_limit_boosted", "weekly_limit_unlimited"]
    ]

    /// 单个 metric 的渲染文本.
    ///   - unit == "unlimited" → "∞"
    ///   - totalValue > 0       → 按 displayMode (remaining/used) 取百分比, 不带 % (状态栏窄)
    ///   - 其它 (无 total)      → formatBalance
    static func formatMetricText(_ metric: UsageMetric, displayMode: DisplayMode) -> String {
        if metric.unit == "unlimited" { return "∞" }
        guard let total = metric.totalValue, total > 0 else {
            return formatBalance(metric.currentValue)
        }
        let ratio: Double
        switch displayMode {
        case .remaining:
            ratio = metric.currentValue / total
        case .used:
            ratio = (total - metric.currentValue) / total
        }
        return "\(Int(ratio * 100))"
    }

    // 余额型 metric 专用 (无 totalValue 的绝对金额, 如 TokenRhythm CNY).
    // 不要用于 times/次数类: 那类应带 totalValue 走上面的百分比分支.
    private static func formatBalance(_ value: Double) -> String {
        if value >= 1000 { return String(format: "%.1fK", value / 1000) }
        // 余额一律显示整数: 状态栏空间有限, 个位精度足够判断"该不该换账号".
        return String(format: "%.0f", value)
    }
}

struct StatusBarView: View {
    let platformData: PlatformUsageData?
    var displayMode: DisplayMode = .used
    var enabledMetrics: [String]? = nil  // nil = 不过滤, 兼容老调用

    init(platformData: PlatformUsageData?, displayMode: DisplayMode = .used, enabledMetrics: [String]? = nil) {
        self.platformData = platformData
        self.displayMode = displayMode
        self.enabledMetrics = enabledMetrics
    }

    private var visibleMetrics: [UsageMetric] {
        StatusBarViewHelper.visibleMetrics(from: platformData?.metrics ?? [], enabledLabels: enabledMetrics)
    }

    private var primaryText: String {
        let v = visibleMetrics
        guard !v.isEmpty else { return "--" }
        return StatusBarViewHelper.formatMetricText(v[0], displayMode: displayMode)
    }

    private var secondaryText: String? {
        let v = visibleMetrics
        guard v.count > 1 else { return nil }
        return StatusBarViewHelper.formatMetricText(v[1], displayMode: displayMode)
    }

    private var statusColor: Color {
        StatusBarViewHelper.statusColor(for: platformData)
    }

    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: "circle.fill")
                .font(.system(size: 12))
                .frame(width: 12)
                .foregroundColor(statusColor)

            // 0 / 1 个 metric: 大字居中; 2 个: 当前上下两行布局.
            if let secondary = secondaryText {
                VStack(alignment: .leading, spacing: 0) {
                    Text(primaryText)
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .lineLimit(1)
                    Text(secondary)
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            } else {
                // 0 / 1 个 metric: 大字居中.
                // 字号 14pt — 比 NSStatusBar 系统厚度 22pt 留出 8pt 给行高 + padding.
                Text(primaryText)
                    .font(.system(size: 14, weight: .medium, design: .monospaced))
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            }
        }
        .padding(.horizontal, 2)
        // 不锁 maxHeight: 让外层 NSStatusItem 按系统菜单栏厚度 (22pt) 自动 fit.
        .frame(minWidth: 36, idealWidth: 44, maxWidth: .infinity, alignment: .leading)
    }
}

#Preview {
    StatusBarView(platformData: nil)
}