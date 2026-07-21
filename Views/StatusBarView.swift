import SwiftUI

/// 把渲染逻辑抽成 pure helper, 让单测可以脱离 SwiftUI runtime 直接验证过滤 / 格式.
enum StatusBarViewHelper {
    /// 按 enabledLabels 过滤并排序 metrics. nil 表示"不过滤", 兼容老调用.
    /// 顺序: 严格按 enabledLabels 给的顺序; enabledLabels 里没有的 metric 会被丢弃.
    static func visibleMetrics(from metrics: [UsageMetric], enabledLabels: [String]?) -> [UsageMetric] {
        guard let enabledLabels else { return metrics }
        let byLabel = Dictionary(uniqueKeysWithValues: metrics.map { ($0.label, $0) })
        return enabledLabels.compactMap { byLabel[$0] }
    }

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

    private static func formatBalance(_ value: Double) -> String {
        if value >= 1000 { return String(format: "%.1fK", value / 1000) }
        if value >= 100 { return String(format: "%.0f", value) }
        return String(format: "%.1f", value)
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
        guard let data = platformData else { return .secondary }
        if data.metrics.isEmpty { return .secondary }
        // 颜色只看第一个 metric (5h 是最重要的); ∞ 不参与颜色.
        if let metric = data.metrics.first, metric.unit != "unlimited",
           let total = metric.totalValue, total > 0 {
            let remainingRatio = metric.currentValue / total
            if remainingRatio < 0.1 { return .red }
            if remainingRatio < 0.5 { return .yellow }
            return .green
        }
        return data.isHealthy ? .green : .red
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