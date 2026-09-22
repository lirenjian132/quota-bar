import XCTest
@testable import QuotaBar

/// WebLoginRenewal 纯逻辑: 平台契约 (四平台覆盖) + 凭据拼装 + 存储形态转换.
/// 无 IO / 无 UI / 无 MainActor 依赖.
final class WebLoginRenewalTests: XCTestCase {

    // MARK: - 平台契约

    func testCookiePlatformsHaveRenewalContract() {
        let stepfun = WebLoginRenewalConfig.platform(for: .stepfun)
        XCTAssertNotNil(stepfun)
        XCTAssertEqual(stepfun?.loginURL.absoluteString, "https://platform.stepfun.com/")
        XCTAssertEqual(stepfun?.cookieNames, ["Oasis-Webid", "Oasis-Token"])

        let tokenrhythm = WebLoginRenewalConfig.platform(for: .tokenrhythm)
        XCTAssertNotNil(tokenrhythm)
        XCTAssertEqual(tokenrhythm?.loginURL.absoluteString, "https://tokenrhythm.studio/login")
        XCTAssertEqual(tokenrhythm?.cookieNames, ["tr_session"])
    }

    func testKeyPlatformsHaveNoRenewalContract() {
        // MiniMax/GLM 是 API key 鉴权: 没有会话可续, 必须 nil (调用方 no-op).
        XCTAssertNil(WebLoginRenewalConfig.platform(for: .minimax_cn))
        XCTAssertNil(WebLoginRenewalConfig.platform(for: .glm_cn))
    }

    func testWebLoginPlatformIsHashable() {
        let a = WebLoginRenewalConfig.platform(for: .stepfun)!
        var set = Set<WebLoginPlatform>()
        set.insert(a)
        set.insert(a)
        XCTAssertEqual(set.count, 1)
        XCTAssertNotEqual(a, WebLoginRenewalConfig.platform(for: .tokenrhythm)!)
    }

    // MARK: - 存储形态契约 (R-3)

    func testStorageFormIsExplicitlyDeclaredPerPlatform() {
        // R-3: storageForm 是平台契约的显式字段 — StepFun 整串入库, TokenRhythm 只存裸值.
        // 新增平台漏声明编译不过, 不再靠 cookieNames.count 隐式推断.
        XCTAssertEqual(WebLoginRenewalConfig.platform(for: .stepfun)?.storageForm, .whole)
        XCTAssertEqual(WebLoginRenewalConfig.platform(for: .tokenrhythm)?.storageForm, .bareValue)
    }

    func testStorageCredentialFollowsDeclaredStorageFormNotCookieCount() {
        // R-3: 转换严格按 storageForm 机械执行. 构造「.whole + 单 cookie」平台:
        // 旧实现 (count == 1 → 剥前缀) 会错剥成 "abc", 新实现必须整串保留.
        let singleWhole = WebLoginPlatform(
            loginURL: URL(string: "https://example.com/")!,
            cookieNames: ["solo"],
            storageForm: .whole
        )
        XCTAssertEqual(
            WebLoginRenewalConfig.storageCredential(from: "solo=abc", platform: singleWhole),
            "solo=abc",
            ".whole 平台必须整串入库, 即使只有一个 cookie"
        )

        // 反向: .bareValue + 单 cookie = 剥 "name=" 前缀 (tokenrhythm 实际行为).
        let singleBare = WebLoginPlatform(
            loginURL: URL(string: "https://example.com/")!,
            cookieNames: ["solo"],
            storageForm: .bareValue
        )
        XCTAssertEqual(
            WebLoginRenewalConfig.storageCredential(from: "solo=abc", platform: singleBare),
            "abc"
        )
    }

    // MARK: - 凭据拼装

    func testCredentialStringAssemblesInConfiguredOrder() {
        // 字典无序, 拼装必须按 platform.cookieNames 的顺序取 — StepFun 的 webid
        // 解析与人类核对都依赖这个顺序. 多余的 cookie (如 __cf_bm) 忽略.
        let stepfun = WebLoginRenewalConfig.platform(for: .stepfun)!
        XCTAssertEqual(
            WebLoginRenewalConfig.credentialString(from: [
                "Oasis-Token": "tok-123",
                "__cf_bm": "noise",
                "Oasis-Webid": "web-456"
            ], platform: stepfun),
            "Oasis-Webid=web-456; Oasis-Token=tok-123"
        )

        let tokenrhythm = WebLoginRenewalConfig.platform(for: .tokenrhythm)!
        XCTAssertEqual(
            WebLoginRenewalConfig.credentialString(from: ["tr_session": "sess_abc"], platform: tokenrhythm),
            "tr_session=sess_abc"
        )
    }

    func testCredentialStringMissingAnyCookieReturnsNil() {
        let stepfun = WebLoginRenewalConfig.platform(for: .stepfun)!
        // 只抓到一半 = 登录没完成 / cookie 还没种上.
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: ["Oasis-Webid": "web-456"], platform: stepfun))
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: ["Oasis-Token": "tok-123"], platform: stepfun))
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: [:], platform: stepfun))
        // cookie 名精确匹配, 不做大小写模糊 (webid() 解析前的原文拼装).

        let tokenrhythm = WebLoginRenewalConfig.platform(for: .tokenrhythm)!
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: ["other": "sess_abc"], platform: tokenrhythm))
    }

    func testCredentialStringEmptyValueReturnsNil() {
        // cookie 在但值是空白 = 没真正登录上, 同样算"未检测到登录凭据".
        let stepfun = WebLoginRenewalConfig.platform(for: .stepfun)!
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: ["Oasis-Webid": "", "Oasis-Token": "tok"], platform: stepfun))
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: ["Oasis-Webid": "   ", "Oasis-Token": "tok"], platform: stepfun))

        let tokenrhythm = WebLoginRenewalConfig.platform(for: .tokenrhythm)!
        XCTAssertNil(WebLoginRenewalConfig.credentialString(from: ["tr_session": " \n"], platform: tokenrhythm))
    }

    func testCredentialStringPreservesSpecialCharactersInValue() {
        // 拼装不是解析: 值里的 "=" / ";" 原样保留 (JWT/padding cookie 值常含 "=").
        // 上游 StepFun service 的 webid() 用 split(maxSplits: 1) 拆第一个 "=",
        // 所以带 "=" 的值也能正确拆回.
        let stepfun = WebLoginRenewalConfig.platform(for: .stepfun)!
        XCTAssertEqual(
            WebLoginRenewalConfig.credentialString(from: [
                "Oasis-Webid": "web==",
                "Oasis-Token": "header.payload.sig=="
            ], platform: stepfun),
            "Oasis-Webid=web==; Oasis-Token=header.payload.sig=="
        )

        let tokenrhythm = WebLoginRenewalConfig.platform(for: .tokenrhythm)!
        XCTAssertEqual(
            WebLoginRenewalConfig.credentialString(from: ["tr_session": "sess=a=b"], platform: tokenrhythm),
            "tr_session=sess=a=b"
        )
    }

    // MARK: - 存储形态

    func testStorageCredentialStripsNamePrefixForSingleCookiePlatform() {
        // TokenRhythm 的 template auth_prefix = "tr_session=": store 层会拼前缀,
        // 存整串会让请求头变成 "tr_session=tr_session=…" — 必须只存裸值.
        let tokenrhythm = WebLoginRenewalConfig.platform(for: .tokenrhythm)!
        XCTAssertEqual(
            WebLoginRenewalConfig.storageCredential(from: "tr_session=abc", platform: tokenrhythm),
            "abc"
        )
        // 只去第一个 "name=" 前缀, 值里剩余的 "=" 不丢.
        XCTAssertEqual(
            WebLoginRenewalConfig.storageCredential(from: "tr_session=a=b", platform: tokenrhythm),
            "a=b"
        )
        // 不是预期前缀形状时原样返回 (防御: 不产出空凭据).
        XCTAssertEqual(
            WebLoginRenewalConfig.storageCredential(from: "plumbing", platform: tokenrhythm),
            "plumbing"
        )
    }

    func testStorageCredentialKeepsWholeStringForMultiCookiePlatform() {
        // StepFun 的 auth_prefix 为空, service 原样把 apiKey 放 Cookie 头:
        // 整串 cookie 即存储形态.
        let stepfun = WebLoginRenewalConfig.platform(for: .stepfun)!
        let credential = "Oasis-Webid=web-456; Oasis-Token=tok-123"
        XCTAssertEqual(
            WebLoginRenewalConfig.storageCredential(from: credential, platform: stepfun),
            credential
        )
    }
}
