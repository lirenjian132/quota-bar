import Foundation

/// StepFun 会话自动刷新 (根治手动续期痛点).
///
/// 契约 (DeepSeek 2026-09 实测, 勿再探测生产端点):
///   POST https://platform.stepfun.com/passport/proto.api.passport.v1.PassportService/RefreshToken
///   头: oasis-appid: 10300 / oasis-platform: web / oasis-webid: 凭据里的 webid
///        (必须与 refresh JWT 内 device_id 一致, 服务端据此校验)
///   Cookie: 整串现有凭据 ("Oasis-Webid=x; Oasis-Token=<access>...<refresh>")
///   body: "{}"
///   200 响应: {"accessToken":{"raw":...,"duration":1800,"mode":2},"refreshToken":{"raw":...}}
///   mode: 2 = SIGN_IN 有效会话; 其它值 (0/1/4 = 匿名/异常) 视为失效.
///
/// 预刷新策略: access TTL 1800s, 本地解析 access JWT 的 exp, exp - now < 300s
/// 才发刷新请求 (纯本地判断, 无请求成本); 解析不出 exp 则不刷新, 让主请求
/// 自然 401 走既有错误链.
///
/// 纯逻辑 + 可注入 network, 不依赖 PlatformAPIService; 失败一律返回 nil,
/// 由调用方决定是否阻断 (PlatformManager 侧不阻断, 静默降级).
struct StepFunSessionRefresher {
    let network: NetworkService

    static let refreshURL = "https://platform.stepfun.com/passport/proto.api.passport.v1.PassportService/RefreshToken"
    /// access token 剩余寿命低于该值才预刷新 (TTL 1800s 的 1/6, 覆盖一次请求周期).
    static let refreshThreshold: TimeInterval = 300

    // platform.stepfun.com 网页端 app id / 平台标识, 与 StepFunPlatformService 一致.
    private static let appID = "10300"
    private static let webPlatform = "web"

    // MARK: - 纯逻辑 (无 IO)

    /// 解析凭据串 "Oasis-Webid=x; Oasis-Token=<jwt>...<jwt>" → (webid, access exp).
    ///
    /// 结构不符 (缺 Oasis-Webid / Oasis-Token 不是 "<access>...<refresh>" 双 JWT /
    /// access JWT payload 解不出 JSON) → 整体返回 nil, 调用方不得刷新.
    /// payload 是合法 JSON 但没有 exp 字段 → 返回 (webid, nil): 解析成功但
    /// 无过期信息, needsRefresh 视作"不需刷新".
    static func parse(_ credential: String) -> (webid: String, accessExpiry: Date?)? {
        var webid: String?
        var token: String?
        // cookie 名大小写不敏感 (与 StepFunPlatformService.webid(from:) 一致).
        for pair in credential.split(separator: ";") {
            let kv = pair.split(separator: "=", maxSplits: 1)
            guard kv.count == 2 else { continue }
            let name = kv[0].trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let value = kv[1].trimmingCharacters(in: .whitespacesAndNewlines)
            if name == "oasis-webid" { webid = value }
            if name == "oasis-token" { token = value }
        }
        guard let webid, !webid.isEmpty, let token, !token.isEmpty else { return nil }

        // Oasis-Token = access JWT 字面 "..." 连接 refresh JWT. 单 JWT (无 "...")
        // 或三段以上都不是有效会话形态, 直接判无效.
        let parts = token.split(separator: "...", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }

        // access JWT payload 必须是合法 base64url JSON: 坏 base64 / 坏 JSON → 无效.
        guard let payload = decodeJWTPayload(parts[0]) else { return nil }
        var expiry: Date?
        // exp 是 Unix 秒. JSONSerialization 解出的数字是 NSNumber, 桥接到 Double.
        // 契约漂移成字符串 ("1790000000") 时也接受 (P1-2) — 只认数字的话会静默
        // 不刷新, 拖到主请求 401 才暴露; 非数字字符串当无 exp 处理.
        if let exp = payload["exp"] as? TimeInterval {
            expiry = Date(timeIntervalSince1970: exp)
        } else if let expText = payload["exp"] as? String, let exp = Double(expText) {
            expiry = Date(timeIntervalSince1970: exp)
        }
        return (webid: webid, accessExpiry: expiry)
    }

    /// 是否需刷新 (输入已解析结果): exp - now < threshold, 缺 exp → false.
    /// PlatformManager 侧 parse 一次即可同时驱动判定与刷新, 不必把同一串解析两遍 (P2-1).
    static func needsRefresh(_ parsed: (webid: String, accessExpiry: Date?), now: Date = Date()) -> Bool {
        guard let expiry = parsed.accessExpiry else { return false }
        return expiry.timeIntervalSince(now) < refreshThreshold
    }

    /// 是否需刷新: 解析成功且 exp - now < threshold.
    /// 解析失败或缺 exp → false (不刷新, 让主请求自然 401).
    static func needsRefresh(_ credential: String, now: Date = Date()) -> Bool {
        guard let parsed = parse(credential) else { return false }
        return needsRefresh(parsed, now: now)
    }

    // MARK: - 网络

    /// 执行刷新. 成功且 mode == 2 → 返回新凭据串 (webid 不变,
    /// Oasis-Token 换 "<新 access raw>...<新 refresh raw>", 与
    /// WebLoginRenewal.credentialString 产出的格式完全一致).
    /// 失败 (解析失败 / 网络错 / 非 200 / mode != 2 / raw 非 JWT 形态) → nil.
    func refresh(_ credential: String) async -> String? {
        guard let parsed = Self.parse(credential) else { return nil }
        return await refresh(credential, parsed: parsed)
    }

    /// 执行刷新 (凭据已由调用方解析 — PlatformManager 侧 parse 一次同时驱动
    /// needsRefresh 与本调用, 同一字符串不重复解析).
    ///
    /// 成功且 mode == 2 → 返回新凭据串 (webid 不变,
    /// Oasis-Token 换 "<新 access raw>...<新 refresh raw>", 与
    /// WebLoginRenewal.credentialString 产出的格式完全一致).
    /// 失败 (解析失败 / 网络错 / 非 200 / mode != 2 / raw 非 JWT 形态) → nil.
    func refresh(_ credential: String, parsed: (webid: String, accessExpiry: Date?)) async -> String? {
        guard let url = URL(string: Self.refreshURL) else { return nil }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(Self.appID, forHTTPHeaderField: "oasis-appid")
        request.setValue(Self.webPlatform, forHTTPHeaderField: "oasis-platform")
        // webid 必须与 refresh JWT 内 device_id 一致, 服务端据此校验令牌挪用.
        request.setValue(parsed.webid, forHTTPHeaderField: "oasis-webid")
        // 整串现有凭据原样进 Cookie 头 (access + refresh 都在里面).
        request.setValue(credential, forHTTPHeaderField: "Cookie")
        request.httpBody = "{}".data(using: .utf8)
        request.timeoutInterval = 10

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await network.data(from: request)
        } catch {
            // 网络/传输错误: 不阻断主流程, 调用方 nil 处理.
            return nil
        }
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return nil }

        guard let payload = try? JSONDecoder().decode(RefreshResponse.self, from: data) else { return nil }
        // HTTP 200 但 accessToken / refreshToken 缺失或 null (契约漂移) → 从严判失败,
        // 不崩、不写任何坏数据 (P2-1 盲区: 解不出 token 时曾无测试覆盖).
        guard let token = payload.accessToken, let refreshToken = payload.refreshToken else { return nil }
        // mode 2 = SIGN_IN 有效会话; 0/1/4 = 匿名/异常 → 视作失效.
        guard token.mode == Self.validMode else { return nil }

        let newAccess = token.raw
        let newRefresh = refreshToken.raw
        // 兜底: raw 必须是 JWT 形态 (至少含 header.payload.signature 两个点).
        // 防服务端契约漂移时把垃圾写进 keychain — 静默写坏数据比不刷新更糟.
        guard Self.isJWT(newAccess), Self.isJWT(newRefresh) else { return nil }

        return "Oasis-Webid=\(parsed.webid); Oasis-Token=\(newAccess)...\(newRefresh)"
    }

    /// SIGN_IN 模式值 (有效会话). 其它模式 = 匿名/异常.
    private static let validMode = 2

    private static func isJWT(_ value: String) -> Bool {
        value.split(separator: ".", omittingEmptySubsequences: false).count >= 3
    }

    /// 解 JWT (header.payload.signature) 的 payload 段为 JSON dict.
    /// base64url (无 padding, -/_ 代替 +/); 补 padding 后标准 base64 解码.
    private static func decodeJWTPayload(_ jwt: String) -> [String: Any]? {
        let segments = jwt.split(separator: ".", omittingEmptySubsequences: false)
        guard segments.count >= 2 else { return nil }
        var base64 = String(segments[1])
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        let remainder = base64.count % 4
        if remainder > 0 {
            base64 += String(repeating: "=", count: 4 - remainder)
        }
        guard let data = Data(base64Encoded: base64),
              let object = try? JSONSerialization.jsonObject(with: data),
              let dict = object as? [String: Any] else { return nil }
        return dict
    }
}

/// RefreshToken 响应体. mode 缺失时解成 nil → 不等于 2 → 判失效 (从严).
/// accessToken / refreshToken 可为 nil: HTTP 200 但响应体不全 (缺失 / null)
/// 时按失败处理, 不崩 (P2-1).
private struct RefreshResponse: Decodable {
    let accessToken: Token?
    let refreshToken: Token?

    struct Token: Decodable {
        let raw: String
        let mode: Int?
    }
}
