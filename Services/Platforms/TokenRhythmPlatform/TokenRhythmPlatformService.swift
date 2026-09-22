import Foundation

// GET /api/wallet/summary 响应. 基元律动 (TokenRhythm) 网页端用户中心用的同款接口,
// 非公开 API: 字段结构以前端 bundle 逆向实测为准 (2026-09).
// 金额字段是字符串形式的 8 位小数 ("253.76250848"), 保留 Double 解析, 显示层取整.
struct TokenRhythmWalletResponse: Codable {
    let code: Int
    let message: String?
    let data: TokenRhythmWalletData?
}

struct TokenRhythmWalletData: Codable {
    let currency: String?
    let availableBalanceCny: String?
    let giftAvailableCny: String?
    let rechargeBalanceCny: String?
    let asOf: String?
}

// GET /api/wallet/expiring-credits 响应: 赠金是逐笔发放的, 每笔各自有到期日.
// summary.nextExpiryAt 是全钱包最早一笔的到期时刻 — 到期该笔剩余额蒸发.
struct TokenRhythmExpiringResponse: Codable {
    let code: Int
    let message: String?
    let data: TokenRhythmExpiringData?
}

struct TokenRhythmExpiringData: Codable {
    let summary: TokenRhythmExpiringSummary?
}

struct TokenRhythmExpiringSummary: Codable {
    let nextExpiryAt: String?
}

// 基元律动余额型中转站: 无包月套餐概念, 只看钱包余额 (赠金/充值), 用完即换账号.
// 鉴权不是 API key 而是网页登录会话 cookie tr_session=sess_..., 配置层把它当作
// apiKey 存储 (auth_header=Cookie / auth_prefix=tr_session= 由 template 提供),
// FileKeyStore 的 0600 加密存储因此原样覆盖.
// session 登录后约 30 天有效, 过期返回 401 → unauthorized, 重新登录粘新值即可.
final class TokenRhythmPlatformAPIService: PlatformAPIService {
    let platformType: PlatformType = .tokenrhythm

    // 薅羊毛多账号 (约 10 个) 都打同一域名, 缓存窗口比其他平台长, 避免高频请求触发风控.
    // 全局刷新循环 (默认 1 分钟) 命中缓存直接返回, 实际 5 分钟才发一次真实请求.
    private let cacheTimeout: TimeInterval = 300
    private let cache = PlatformUsageCache<PlatformUsageData>()

    // 余额低于 10 元视为不健康 (状态栏变红), 提示该换账号了.
    private let lowBalanceThreshold: Double = 10

    func fetchUsage(config: PlatformConfigData, network: NetworkService) async throws -> PlatformUsageData {
        if let cached = cache.read(timeout: cacheTimeout) {
            return cached
        }

        guard !config.apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.notConfigured(config.platformType)
        }

        guard let url = URL(string: config.apiBaseURL) else {
            throw PlatformError.invalidResponse(config.platformType)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("\(config.authPrefix)\(config.apiKey)", forHTTPHeaderField: config.authHeader)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 10

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await network.data(from: request)
        } catch {
            throw PlatformError.networkError(config.platformType, error.localizedDescription)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw PlatformError.invalidResponse(config.platformType)
        }

        // session 过期/被顶: 用户需重新登录网页并粘贴新的 tr_session.
        if httpResponse.statusCode == 401 {
            throw PlatformError.unauthorized(config.platformType)
        }

        guard httpResponse.statusCode == 200 else {
            let responseString = String(data: data, encoding: .utf8) ?? "unable to decode"
            throw PlatformError.networkError(config.platformType, "HTTP \(httpResponse.statusCode): \(responseString.prefix(200))")
        }

        let walletResponse: TokenRhythmWalletResponse
        do {
            walletResponse = try JSONDecoder().decode(TokenRhythmWalletResponse.self, from: data)
        } catch {
            throw PlatformError.decodingError(config.platformType, error.localizedDescription)
        }

        // 业务码非 0 (如 session 半失效): 透出服务端 message, 比笼统报错有用.
        guard walletResponse.code == 0 else {
            let message = walletResponse.message?.isEmpty == false ? walletResponse.message! : "code \(walletResponse.code)"
            throw PlatformError.apiError(config.platformType, message)
        }

        // 金额是字符串小数, 解析失败按无效响应处理.
        guard let balanceString = walletResponse.data?.availableBalanceCny,
              let balance = Double(balanceString) else {
            throw PlatformError.invalidResponse(config.platformType)
        }

        // 余额型指标: totalValue = nil → StatusBarViewHelper 走 formatBalance (整数显示).
        // resetTime 放最早到期时刻 (expiring-credits 接口), popover 显示,
        // 状态栏颜色用它做 7 天黄 / 3 天红的"钱要蒸发"警示.
        let nextExpiry = await nextExpiryDate(from: config, network: network)
        let usageData = PlatformUsageData(
            platform: config.platformType,
            instanceID: config.instanceID,
            displayName: config.displayName,
            metrics: [
                UsageMetric(
                    label: "balance",
                    currentValue: balance,
                    totalValue: nil,
                    unit: "CNY",
                    resetTime: nextExpiry
                )
            ],
            lastUpdated: Date(),
            // 不健康 = 余额见底或最早到期在 3 天内 (到期即蒸发, 比低余额更急).
            isHealthy: balance >= lowBalanceThreshold && !isExpiringSoon(nextExpiry, withinDays: 3)
        )

        cache.write(usageData)
        return usageData
    }

    func clearCache() {
        cache.clear()
    }

    // 查最早到期时间. 非致命增强: 接口失败/字段缺失返回 nil, 余额显示不受影响.
    private func nextExpiryDate(from config: PlatformConfigData, network: NetworkService) async -> Date? {
        // apiBaseURL 是 wallet/summary 全址, 换成同 service 的 expiring-credits.
        let expiringURLString = config.apiBaseURL.replacingOccurrences(
            of: "/wallet/summary",
            with: "/wallet/expiring-credits"
        )
        // 替换是 no-op (base 不含 /wallet/summary) 时 URL 与主请求相同: 再打一趟
        // 同样的 summary 没意义, 直接放弃到期信息 (降级为纯余额显示).
        guard expiringURLString != config.apiBaseURL,
              let url = URL(string: expiringURLString) else {
            return nil
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("\(config.authPrefix)\(config.apiKey)", forHTTPHeaderField: config.authHeader)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 10

        guard let (data, response) = try? await network.data(from: request),
              let httpResponse = response as? HTTPURLResponse,
              httpResponse.statusCode == 200 else {
            return nil
        }
        guard let decoded = try? JSONDecoder().decode(TokenRhythmExpiringResponse.self, from: data),
              decoded.code == 0 else {
            return nil
        }
        guard let iso = decoded.data?.summary?.nextExpiryAt else { return nil }
        return Self.parseISO8601(iso)
    }

    /// 解析上游 ISO8601 时间串. 实测原文带毫秒 ("2026-09-26T15:20:56.281Z"):
    /// ISO8601DateFormatter 默认的 .withInternetDateTime 解毫秒串返回 nil,
    /// 曾导致到期时间恒 nil、7 天黄/3 天红警示全链失效.
    /// 先试带毫秒格式, 再回退无毫秒格式, 两种形态都接.
    static func parseISO8601(_ string: String) -> Date? {
        let withFractionalSeconds = ISO8601DateFormatter()
        withFractionalSeconds.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFractionalSeconds.date(from: string) {
            return date
        }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: string)
    }

    private func isExpiringSoon(_ date: Date?, withinDays days: Double) -> Bool {
        guard let date else { return false }
        return date.timeIntervalSinceNow < days * 86400
    }
}
