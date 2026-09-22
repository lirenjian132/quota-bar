import Foundation

// GET /api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit 响应.
// 接口契约源自 Step Code 官方开源客户端 (stepfun-ai/Step-Code) 的 proto 定义,
// 经实测验证 (2026-09). 套餐是月 credits 池制: credit_buckets 给绝对量,
// subscription_credit_left_rate 是百分比剩余.
struct StepFunRateLimitResponse: Codable {
    let status: Int
    let desc: String?
    let planCreditRateLimit: StepFunPlanCreditRateLimit?
    // 非 credit 套餐族 (plan_family != 2) 没有 credits 池, 用两个窗口剩余率兜底.
    let fiveHourUsageLeftRate: Double?
    let weeklyUsageLeftRate: Double?
}

struct StepFunPlanCreditRateLimit: Codable {
    // 套餐 credits 剩余率 (0~1), 如 0.96886694 = 剩余 96.89%.
    let subscriptionCreditLeftRate: Double?
    // 月度 credits 池重置时刻 (Unix 秒). 上游文档标注 "string or integer",
    // 两种形态都要接 — 整数形态会让纯 String? 字段整体解码失败 (见 init(from:)).
    let subscriptionCreditResetTime: Double?

    // 自定义解码: resetTime 双读 String/Double/Int. 注意外层解码器带
    // .convertFromSnakeCase, JSON key 在容器里已被转成驼峰, 所以这里 CodingKeys
    // 必须用默认驼峰 raw value (写蛇形真值会全部查找落空, leftRate 解成 nil).
    private enum CodingKeys: String, CodingKey {
        case subscriptionCreditLeftRate
        case subscriptionCreditResetTime
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // try? 而非 decodeIfPresent: 类型不匹配时 decodeIfPresent 抛错而非返回 nil,
        // 一个字段形态漂移不该让整个响应解码失败.
        subscriptionCreditLeftRate = try? container.decode(Double.self, forKey: .subscriptionCreditLeftRate)
        if let string = try? container.decode(String.self, forKey: .subscriptionCreditResetTime) {
            subscriptionCreditResetTime = Double(string)
        } else {
            subscriptionCreditResetTime = try? container.decode(Double.self, forKey: .subscriptionCreditResetTime)
        }
    }
}

// GET /api/step.openapi.devcenter.Dashboard/GetStepPlanStatus 响应.
struct StepFunPlanStatusResponse: Codable {
    let status: Int
    let desc: String?
    let subscription: StepFunSubscription?
}

struct StepFunSubscription: Codable {
    // 订阅状态: 1 = 有效. 0/负值为过期/停订.
    let status: Int?
    // 套餐等级名 ("Mini"/"Go"/"Pro"/"Max").
    let name: String?
    // 订阅到期 (Unix 秒, 字符串), 年费套餐通常一年.
    let expiredAt: String?
}

// Stepfun (阶跃星辰) 套餐平台.
// 官方无公开 API; 接口来自官方开源客户端 Step Code 的 proto 契约
// (gRPC-Connect, base https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard).
// 鉴权 = 网页 SSO 双 cookie (Oasis-Webid + Oasis-Token) + 三个 oasis-* 头,
// 配置层整串 cookie 存 apiKey, FileKeyStore 加密覆盖.
final class StepFunPlatformAPIService: PlatformAPIService {
    let platformType: PlatformType = .stepfun

    // 套餐余量按小时级感知即可, 缓存窗口与 TokenRhythm 对齐, 避免无谓请求.
    private let cacheTimeout: TimeInterval = 300
    private let cache = PlatformUsageCache<PlatformUsageData>()

    // platform.stepfun.com 网页端 app id / 平台标识, 前端固定值.
    private let appID = "10300"
    private let platform = "web"

    // credits 剩余低于 10% 由 view 层标红, 这里只把"订阅失效"判为不健康.
    private let validSubscriptionStatus = 1

    func fetchUsage(config: PlatformConfigData, network: NetworkService) async throws -> PlatformUsageData {
        if let cached = cache.read(timeout: cacheTimeout) {
            return cached
        }

        let credential = config.apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !credential.isEmpty else {
            throw PlatformError.notConfigured(config.platformType)
        }

        // 请求1: 套餐 credits 余量 (主指标).
        let rateLimit = try await request(config: config, network: network, method: "QueryStepPlanRateLimit") as StepFunRateLimitResponse
        // status != 1 是业务错误 (账户停用/欠费): 透出 desc, 与其它平台报 apiError 的做法一致.
        guard rateLimit.status == validSubscriptionStatus else {
            let message = rateLimit.desc?.isEmpty == false ? rateLimit.desc! : "status \(rateLimit.status)"
            throw PlatformError.apiError(config.platformType, message)
        }

        // 指标构造: credit 套餐 (plan_family == 2) 用 plan_credit_rate_limit;
        // 其它套餐族没有 credits 池, 契约改用 five_hour/weekly 窗口剩余率,
        // 降级生成两个 % 指标; 两者全缺才算无效响应.
        let metrics: [UsageMetric]
        if let leftRate = rateLimit.planCreditRateLimit?.subscriptionCreditLeftRate {
            metrics = [
                UsageMetric(
                    label: "credits",
                    currentValue: leftRate * 100,
                    totalValue: 100,
                    unit: "%",
                    // 月度 credits 池重置日; 时间戳缺失时为 nil.
                    resetTime: resetDate(from: rateLimit.planCreditRateLimit?.subscriptionCreditResetTime)
                )
            ]
        } else {
            metrics = Self.windowMetrics(from: rateLimit)
            guard !metrics.isEmpty else {
                throw PlatformError.invalidResponse(config.platformType)
            }
        }

        // 请求2: 订阅状态 (失效即红). 失败不阻塞: 降级为仅显示余量.
        var subscriptionExpired = false
        if let statusResponse: StepFunPlanStatusResponse = try? await request(config: config, network: network, method: "GetStepPlanStatus") {
            // status 判定必须独立进行 (旧逻辑把 statusResponse.status == 1 当入口 guard,
            // 停用订阅时整段判定被跳过, isHealthy 恒 true — 停用/停订却在状态栏显示健康).
            //
            // 外层 status != 1 (账户停用/欠费/查询异常) 时 subscription 字段不可信:
            // 此刻采信它会把"查询失败"误判成"停订" (miniMax Round 2), 因此静默降级 —
            // 只显示余量, isHealthy 保持 true. 仅当外层 status == 1 (查询正常) 且
            // subscription.status 明确 != 1 (过期/停订) 时才判失效.
            if statusResponse.status == validSubscriptionStatus {
                if let sub = statusResponse.subscription, sub.status != nil && sub.status != validSubscriptionStatus {
                    // 订阅状态 0/负值 = 过期/停订; 停订时 expired_at 往往仍在未来 (账期未到),
                    // 不能只看时间戳, 先信 status 字段.
                    subscriptionExpired = true
                } else if let expiredAt = statusResponse.subscription?.expiredAt, let ts = Double(expiredAt) {
                    subscriptionExpired = ts < Date().timeIntervalSince1970
                }
            }
        }

        let usageData = PlatformUsageData(
            platform: config.platformType,
            instanceID: config.instanceID,
            displayName: config.displayName,
            metrics: metrics,
            lastUpdated: Date(),
            // 订阅失效/过期 → 不健康 (红). credits 余量的红黄绿由 view 按百分比渲染.
            isHealthy: !subscriptionExpired
        )

        cache.write(usageData)
        return usageData
    }

    func clearCache() {
        cache.clear()
    }

    // MARK: - Private

    /// 非 credit 套餐族: 用窗口剩余率生成 % 指标 (five_hour / weekly_limit).
    /// 窗口率与 credit left_rate 同为 0~1 比例; 某窗口缺失则跳过, 全缺返回空
    /// (调用方按 invalidResponse 处理).
    private static func windowMetrics(from response: StepFunRateLimitResponse) -> [UsageMetric] {
        var metrics: [UsageMetric] = []
        if let fiveHour = response.fiveHourUsageLeftRate {
            metrics.append(UsageMetric(
                label: "five_hour",
                currentValue: fiveHour * 100,
                totalValue: 100,
                unit: "%",
                resetTime: nil
            ))
        }
        if let weekly = response.weeklyUsageLeftRate {
            metrics.append(UsageMetric(
                label: "weekly_limit",
                currentValue: weekly * 100,
                totalValue: 100,
                unit: "%",
                resetTime: nil
            ))
        }
        return metrics
    }

    /// POST 一个 Dashboard RPC 方法, 返回解码后的泛型响应.
    /// 401/403 (cookie 过期/被顶/被拒) → unauthorized, 提示用户重新登录粘贴.
    private func request<T: Decodable>(config: PlatformConfigData, network: NetworkService, method: String) async throws -> T {
        // webid 拆不到说明 cookie 串粘错了 (漏了 Oasis-Webid 或值是空白):
        // 早失败, 别发一趟必然 401 的请求再把用户引向"重新登录" — 这是格式问题,
        // 报 apiError 直接给出粘贴格式, 比 invalidResponse ("无效的响应数据") 准确.
        // 文案走 i18n (error.stepfun.cookieFormat), 与其它平台错误提示同一套本地化机制.
        guard webid(from: config.apiKey) != nil else {
            throw PlatformError.apiError(config.platformType, I18nService.shared.translate("error.stepfun.cookieFormat"))
        }

        // apiBaseURL 是 service 基址 (到 .../Dashboard 为止), method 拼在末尾.
        guard let url = URL(string: config.apiBaseURL + "/" + method) else {
            throw PlatformError.invalidResponse(config.platformType)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        // apiKey 存的是整串 cookie ("Oasis-Webid=x; Oasis-Token=y"), 原样放 Cookie 头.
        request.setValue(config.apiKey, forHTTPHeaderField: "Cookie")
        // oasis-* 头是服务端"令牌挪用"校验的一部分: webid 必须与 cookie 里的配对.
        request.setValue(appID, forHTTPHeaderField: "oasis-appid")
        request.setValue(platform, forHTTPHeaderField: "oasis-platform")
        request.setValue(webid(from: config.apiKey) ?? "", forHTTPHeaderField: "oasis-webid")
        // Connect 协议版本头, 上游参考实现固定带 1.
        request.setValue("1", forHTTPHeaderField: "Connect-Protocol-Version")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
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
        // 401/403 都算凭据问题 (cookie 过期/被顶/风控拒绝).
        if httpResponse.statusCode == 401 || httpResponse.statusCode == 403 {
            throw PlatformError.unauthorized(config.platformType)
        }
        guard httpResponse.statusCode == 200 else {
            let responseString = String(data: data, encoding: .utf8) ?? "unable to decode"
            throw PlatformError.networkError(config.platformType, "HTTP \(httpResponse.statusCode): \(responseString.prefix(200))")
        }

        do {
            // 接口字段是 snake_case (subscription_credit_left_rate), 结构体用驼峰.
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(T.self, from: data)
        } catch {
            throw PlatformError.decodingError(config.platformType, error.localizedDescription)
        }
    }

    /// 从整串 cookie 里拆 Oasis-Webid 的值. 拆不到或值为空白 ("Oasis-Webid=   ;")
    /// 返回 nil — 空白值同样是粘错了, 放过去会发必然失败的请求.
    private func webid(from cookie: String) -> String? {
        for pair in cookie.split(separator: ";") {
            let kv = pair.split(separator: "=", maxSplits: 1)
            // cookie 名大小写不敏感, 容忍用户从不同导出格式粘贴.
            if kv.count == 2, kv[0].trimmingCharacters(in: .whitespaces).lowercased() == "oasis-webid" {
                let value = kv[1].trimmingCharacters(in: .whitespaces)
                return value.isEmpty ? nil : value
            }
        }
        return nil
    }

    private func resetDate(from unixSeconds: Double?) -> Date? {
        guard let ts = unixSeconds, ts > 0 else { return nil }
        // 合理性区间: 晚于 2020、早于 2096. 挡住服务端改毫秒时间戳后解析出的 1970 年假日期.
        guard ts > 1_600_000_000, ts < 4_000_000_000 else { return nil }
        return Date(timeIntervalSince1970: ts)
    }
}
