import XCTest
@testable import QuotaBar

/// StepFunSessionRefresher: 纯逻辑 (parse/needsRefresh) + 网络刷新契约.
/// JWT fixture 全部本地构造 (base64url(任意 payload JSON) + 假签名段),
/// 不触碰真实 token / 生产端点.
final class StepFunSessionRefresherTests: XCTestCase {
    private var mockNetwork: MockNetworkService!

    override func setUp() {
        super.setUp()
        mockNetwork = MockNetworkService()
    }

    // MARK: - JWT fixture 帮助 (本地构造, 禁任何真实 token)

    private let webid = "2c321138f52a7d858f2d9eab619a357038d0f66c"

    /// base64url: 标准 base64 后 -/_ 换 +/、去 padding.
    private func base64url(_ string: String) -> String {
        Data(string.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// 构造三段 JWT; payload 段是合法 base64url JSON (签名段任意).
    private func jwt(payload: [String: Any], signature: String = "fakesig") -> String {
        let header = base64url("{\"alg\":\"EdDSA\",\"typ\":\"JWT\"}")
        let payloadData = try! JSONSerialization.data(withJSONObject: payload)
        let body = base64url(String(data: payloadData, encoding: .utf8)!)
        return "\(header).\(body).\(signature)"
    }

    private func credential(accessJWT: String, refreshJWT: String) -> String {
        "Oasis-Webid=\(webid); Oasis-Token=\(accessJWT)...\(refreshJWT)"
    }

    /// 相对当前时刻的 Unix 秒 (Int, 避开 JSON 双精度序列化误差).
    private func nowPlus(_ offset: TimeInterval) -> Int {
        Int(Date().timeIntervalSince1970) + Int(offset)
    }

    private func expiringCredential(_ offset: TimeInterval) -> String {
        let access = jwt(payload: ["exp": nowPlus(offset), "mode": 2, "oasis_id": "u-1"])
        let refresh = jwt(payload: ["exp": nowPlus(86400 * 30), "app_id": 10300, "device_id": webid])
        return credential(accessJWT: access, refreshJWT: refresh)
    }

    /// exp 序列化成字符串 ("1790000000") 的即将过期凭据 — 契约漂移 fixture (P1-2).
    private func credentialWithStringExp(_ offset: TimeInterval) -> String {
        let access = jwt(payload: ["exp": "\(nowPlus(offset))", "mode": 2, "oasis_id": "u-1"])
        let refresh = jwt(payload: ["exp": nowPlus(86400 * 30), "app_id": 10300, "device_id": webid])
        return credential(accessJWT: access, refreshJWT: refresh)
    }

    private func setRefreshResponse(_ json: String, statusCode: Int = 200) {
        mockNetwork.mockData = json.data(using: .utf8)
        mockNetwork.mockResponse = MockNetworkService.makeResponse(
            url: StepFunSessionRefresher.refreshURL, statusCode: statusCode)
    }

    // MARK: - parse

    func testParseValidCredentialReturnsWebidAndExpiry() {
        let exp = nowPlus(1800)
        let parsed = StepFunSessionRefresher.parse(expiringCredential(1800))

        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed?.webid, webid)
        XCTAssertEqual(parsed?.accessExpiry?.timeIntervalSince1970 ?? 0, Double(exp), accuracy: 1)
    }

    func testParseSingleJWTReturnsNil() {
        // Oasis-Token 必须是 "<access>...<refresh>" 双 JWT; 单 JWT 不是有效会话形态.
        let access = jwt(payload: ["exp": nowPlus(1800), "mode": 2])
        XCTAssertNil(StepFunSessionRefresher.parse("Oasis-Webid=\(webid); Oasis-Token=\(access)"))
    }

    func testParseBadBase64PayloadReturnsNil() {
        // payload 段 "!!!" 不是合法 base64 → 解不出 JSON → 整体无效.
        let refresh = jwt(payload: ["exp": nowPlus(86400)])
        XCTAssertNil(StepFunSessionRefresher.parse("Oasis-Webid=\(webid); Oasis-Token=!!!.!!!.sig...\(refresh)"))
    }

    func testParseBadJSONPayloadReturnsNil() {
        // payload 段是合法 base64 但不是 JSON.
        let bogus = base64url("not json at all")
        let refresh = jwt(payload: ["exp": nowPlus(86400)])
        XCTAssertNil(StepFunSessionRefresher.parse("Oasis-Webid=\(webid); Oasis-Token=aaa.\(bogus).sig...\(refresh)"))
    }

    func testParseMissingWebidReturnsNil() {
        // 只剩 Oasis-Token: 缺 Oasis-Webid → 无效 (webid 是刷新请求必需头).
        let cred = expiringCredential(1800)
        XCTAssertNil(StepFunSessionRefresher.parse(
            cred.replacingOccurrences(of: "Oasis-Webid=\(webid); ", with: "")))
    }

    func testParseAcceptsCaseInsensitiveCookieNames() {
        // cookie 名大小写不敏感 (与 StepFunPlatformService.webid(from:) 一致).
        let parsed = StepFunSessionRefresher.parse(expiringCredential(1800)
            .replacingOccurrences(of: "Oasis-Webid", with: "oasis-webid")
            .replacingOccurrences(of: "Oasis-Token", with: "oasis-token"))
        XCTAssertEqual(parsed?.webid, webid)
    }

    func testParseValidJSONWithoutExpReturnsNilExpiry() {
        // payload 是合法 JWT 但没有 exp 字段: 解析成功, 但无过期信息.
        let access = jwt(payload: ["mode": 2])
        let refresh = jwt(payload: ["exp": nowPlus(86400)])
        let parsed = StepFunSessionRefresher.parse(credential(accessJWT: access, refreshJWT: refresh))
        XCTAssertNotNil(parsed)
        XCTAssertNil(parsed?.accessExpiry)
    }

    // MARK: - needsRefresh

    func testNeedsRefreshFalseWhenExpiryTenMinutesAway() {
        XCTAssertFalse(StepFunSessionRefresher.needsRefresh(expiringCredential(600)))
    }

    func testNeedsRefreshTrueWhenExpiryFourMinutesAway() {
        XCTAssertTrue(StepFunSessionRefresher.needsRefresh(expiringCredential(240)))
    }

    func testNeedsRefreshTrueWhenAlreadyExpired() {
        XCTAssertTrue(StepFunSessionRefresher.needsRefresh(expiringCredential(-60)))
    }

    func testNeedsRefreshFalseWhenUnparseable() {
        XCTAssertFalse(StepFunSessionRefresher.needsRefresh("Oasis-Token=garbage"))
        XCTAssertFalse(StepFunSessionRefresher.needsRefresh(""))
    }

    func testNeedsRefreshUsesInjectedNow() {
        // 同一凭据 + 不同 now: 越过阈值边界时判定翻转 (证明阈值比较生效).
        let cred = expiringCredential(600)
        let expiry = StepFunSessionRefresher.parse(cred)!.accessExpiry!
        XCTAssertFalse(StepFunSessionRefresher.needsRefresh(cred, now: expiry.addingTimeInterval(-400)))
        XCTAssertTrue(StepFunSessionRefresher.needsRefresh(cred, now: expiry.addingTimeInterval(-299)))
    }

    // MARK: - parse: 字符串 exp (P1-2)

    func testParseAcceptsStringExp() {
        // 契约漂移: exp 被序列化成字符串而非 JSON 数字. 只认数字时会静默不刷新,
        // 拖到主请求 401 才暴露; 数字 fallback Double(s) 后应正常解析.
        let exp = nowPlus(1800)
        let parsed = StepFunSessionRefresher.parse(credentialWithStringExp(1800))
        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed?.webid, webid)
        XCTAssertEqual(parsed?.accessExpiry?.timeIntervalSince1970 ?? 0, Double(exp), accuracy: 1)
    }

    func testNeedsRefreshStringExpUsesSameThreshold() {
        // 字符串 exp 与注入 now: 阈值判定走同一条逻辑 (与数字 exp 边界一致).
        let cred = credentialWithStringExp(600)
        let expiry = StepFunSessionRefresher.parse(cred)!.accessExpiry!
        XCTAssertFalse(StepFunSessionRefresher.needsRefresh(cred, now: expiry.addingTimeInterval(-400)))
        XCTAssertTrue(StepFunSessionRefresher.needsRefresh(cred, now: expiry.addingTimeInterval(-299)))
    }

    func testParseNonNumericStringExpReturnsNilExpiry() {
        // 非数字字符串当"无 exp"处理: 解析成功但不刷新, 让主请求自然 401,
        // 不因解析不出时间就崩或误判过期.
        let access = jwt(payload: ["exp": "not-a-number", "mode": 2])
        let refresh = jwt(payload: ["exp": nowPlus(86400)])
        let cred = credential(accessJWT: access, refreshJWT: refresh)
        let parsed = StepFunSessionRefresher.parse(cred)
        XCTAssertNotNil(parsed)
        XCTAssertNil(parsed?.accessExpiry)
        XCTAssertFalse(StepFunSessionRefresher.needsRefresh(cred))
    }

    // MARK: - refresh 契约

    func testRefreshSuccessReturnsNewCredentialAndSendsContractRequest() async throws {
        setRefreshResponse(#"{"accessToken":{"raw":"aaa.bbb.ccc","duration":1800,"mode":2},"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let oldCredential = expiringCredential(240)

        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(oldCredential)

        XCTAssertEqual(result, "Oasis-Webid=\(webid); Oasis-Token=aaa.bbb.ccc...ddd.eee.fff")

        let request = try XCTUnwrap(mockNetwork.lastRequest)
        XCTAssertEqual(request.url?.absoluteString, StepFunSessionRefresher.refreshURL)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(request.value(forHTTPHeaderField: "oasis-appid"), "10300")
        XCTAssertEqual(request.value(forHTTPHeaderField: "oasis-platform"), "web")
        // webid 必须与 refresh JWT 内 device_id 一致 (服务端令牌挪用校验).
        XCTAssertEqual(request.value(forHTTPHeaderField: "oasis-webid"), webid)
        // Cookie 是整串现有凭据 (access + refresh 都在里面).
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), oldCredential)
        XCTAssertEqual(String(data: request.httpBody ?? Data(), encoding: .utf8), "{}")
    }

    func testRefreshAnonymousModeReturnsNil() async {
        // mode 1 = 匿名会话, 不是 SIGN_IN → 视作失效, 不换凭据.
        setRefreshResponse(#"{"accessToken":{"raw":"aaa.bbb.ccc","duration":1800,"mode":1},"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshMissingModeReturnsNil() async {
        // mode 缺失 → 不等于 2 (从严: 契约漂移时不写坏凭据).
        setRefreshResponse(#"{"accessToken":{"raw":"aaa.bbb.ccc","duration":1800},"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshUnauthorizedReturnsNil() async {
        setRefreshResponse(#"{"code":"unauthenticated"}"#, statusCode: 401)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshBadJSONReturnsNil() async {
        setRefreshResponse("not-json")
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshNonJWTResponseReturnsNil() async {
        // 服务端把 raw 返成非 JWT 垃圾: 拼出来也是坏凭据, 宁可放弃刷新.
        setRefreshResponse(#"{"accessToken":{"raw":"garbage","duration":1800,"mode":2},"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshNetworkErrorReturnsNil() async {
        mockNetwork.mockError = URLError(.notConnectedToInternet)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
        XCTAssertEqual(mockNetwork.requestCount, 1, "请求已发出但传输失败")
    }

    func testRefreshUnparseableCredentialReturnsNilWithoutRequest() async {
        // 凭据形态不对时直接放弃, 不发刷新请求 (发了也是 401).
        setRefreshResponse(#"{"accessToken":{"raw":"aaa.bbb.ccc","duration":1800,"mode":2},"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh("Oasis-Token=garbage")
        XCTAssertNil(result)
        XCTAssertEqual(mockNetwork.requestCount, 0, "不可解析凭据不应发刷新请求")
    }

    // MARK: - refresh: HTTP 200 但响应体不全 (P2-1 盲区)

    func testRefreshMissingAccessTokenReturnsNil() async {
        // HTTP 200 但体里没有 accessToken (只有 refreshToken): 契约异常 → 判失败,
        // 不崩、不写坏凭据.
        setRefreshResponse(#"{"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshNullAccessTokenReturnsNil() async {
        // HTTP 200 且 accessToken 显式为 null.
        setRefreshResponse(#"{"accessToken":null,"refreshToken":{"raw":"ddd.eee.fff"}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshMissingRefreshTokenReturnsNil() async {
        // 只有 accessToken: refresh 缺失同样判失败 (缺一半拼不出完整新凭据).
        setRefreshResponse(#"{"accessToken":{"raw":"aaa.bbb.ccc","duration":1800,"mode":2}}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }

    func testRefreshNullRefreshTokenReturnsNil() async {
        setRefreshResponse(#"{"accessToken":{"raw":"aaa.bbb.ccc","duration":1800,"mode":2},"refreshToken":null}"#)
        let result = await StepFunSessionRefresher(network: mockNetwork).refresh(expiringCredential(240))
        XCTAssertNil(result)
    }
}
