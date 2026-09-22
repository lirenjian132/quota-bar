import XCTest
@testable import QuotaBar

final class PlatformProtocolTests: XCTestCase {
    func testPlatformTypeDisplayNames() {
        XCTAssertEqual(PlatformType.minimax_cn.displayName, "MiniMax")
        XCTAssertEqual(PlatformType.glm_cn.displayName, "GLM")
        XCTAssertEqual(PlatformType.tokenrhythm.displayName, "基元律动")
        XCTAssertEqual(PlatformType.stepfun.displayName, "Stepfun")
    }

    func testPlatformTypeAllCases() {
        XCTAssertEqual(PlatformType.allCases.count, 4)
        XCTAssertTrue(PlatformType.allCases.contains(.minimax_cn))
        XCTAssertTrue(PlatformType.allCases.contains(.glm_cn))
        XCTAssertTrue(PlatformType.allCases.contains(.tokenrhythm))
        XCTAssertTrue(PlatformType.allCases.contains(.stepfun))
    }

    func testPlatformTypeRawValues() {
        XCTAssertEqual(PlatformType.minimax_cn.rawValue, "minimax_cn")
        XCTAssertEqual(PlatformType.glm_cn.rawValue, "glm_cn")
        XCTAssertEqual(PlatformType.tokenrhythm.rawValue, "tokenrhythm")
        XCTAssertEqual(PlatformType.stepfun.rawValue, "stepfun")
    }

    func testPlatformUsageDataEquality() {
        let metric = UsageMetric(label: "Balance", currentValue: 10, totalValue: nil, unit: "USD", resetTime: nil)
        let date = Date()
        let data1 = PlatformUsageData(platform: .glm_cn, instanceID: "glm_cn", displayName: "GLM", metrics: [metric], lastUpdated: date, isHealthy: true)
        let data2 = PlatformUsageData(platform: .glm_cn, instanceID: "glm_cn", displayName: "GLM", metrics: [metric], lastUpdated: date, isHealthy: true)
        XCTAssertEqual(data1, data2)
    }

    func testUsageMetricEquality() {
        let date = Date()
        let metric1 = UsageMetric(label: "Daily", currentValue: 45, totalValue: 100, unit: "requests", resetTime: date)
        let metric2 = UsageMetric(label: "Daily", currentValue: 45, totalValue: 100, unit: "requests", resetTime: date)
        XCTAssertEqual(metric1, metric2)
    }

    func testUsageMetricWithNilValues() {
        let metric = UsageMetric(label: "Balance", currentValue: 4.5, totalValue: nil, unit: "USD", resetTime: nil)
        XCTAssertNil(metric.totalValue)
        XCTAssertNil(metric.resetTime)
    }

    func testPlatformErrorEquality() {
        XCTAssertEqual(PlatformError.notConfigured(.minimax_cn), PlatformError.notConfigured(.minimax_cn))
        XCTAssertNotEqual(PlatformError.notConfigured(.minimax_cn), PlatformError.notConfigured(.glm_cn))
    }

    func testUnauthorizedDescriptionDispatchesByCredentialType() {
        // P1-3: 401 文案按凭据类型分派. cookie 型平台 (网页登录会话) 提示重新登录粘贴;
        // API key 型平台提示检查 key — 旧版共用一句"重新登录"对 MiniMax/GLM 词不达.
        let i18n = I18nService.shared
        i18n.loadTranslations()  // 测试宿主里的 app delegate 可能被测试提前拦截, 显式加载保证确定性

        i18n.setLocale("zh-Hans")
        let sessionZh = PlatformError.unauthorized(.tokenrhythm).errorDescription
        let keyZh = PlatformError.unauthorized(.minimax_cn).errorDescription
        XCTAssertEqual(sessionZh, "登录已过期，请重新登录并粘贴新的凭据")
        XCTAssertEqual(keyZh, "认证失败，请检查 API Key 是否正确或已失效")
        XCTAssertNotEqual(sessionZh, keyZh, "两类凭据的提示必须不同")
        // 两个 cookie 型平台共用 session 文案, 两个 key 型平台共用 key 文案
        XCTAssertEqual(PlatformError.unauthorized(.stepfun).errorDescription, sessionZh)
        XCTAssertEqual(PlatformError.unauthorized(.glm_cn).errorDescription, keyZh)

        i18n.setLocale("en")
        XCTAssertEqual(
            PlatformError.unauthorized(.stepfun).errorDescription,
            "Login expired. Please sign in again and paste the new credentials."
        )
        XCTAssertEqual(
            PlatformError.unauthorized(.glm_cn).errorDescription,
            "Authentication failed. Please check that your API key is correct and still valid."
        )

        i18n.setLocale("en")  // 复位, 不影响其它用例
    }

    /// i18n 守卫生 (R3): en/zh-Hans 键集合必须完全一致 — 漏配一种语言时
    /// translate 会回退返回 key 自身, 用户界面直接裸秀 "menu.metric.xxx".
    func testI18nKeySetsMatchAcrossLocales() throws {
        func keys(forResource name: String) throws -> Set<String> {
            let url = try XCTUnwrap(Bundle.main.url(forResource: name, withExtension: "json"),
                                    "\(name).json 应在 Bundle 里")
            let data = try Data(contentsOf: url)
            let json = try XCTUnwrap(
                JSONSerialization.jsonObject(with: data) as? [String: String],
                "\(name).json 顶层应为字符串字典"
            )
            return Set(json.keys)
        }

        let en = try keys(forResource: "en")
        let zh = try keys(forResource: "zh-Hans")
        XCTAssertEqual(en, zh, "两种语言的键集合必须一致; en 独有: \(en.subtracting(zh)), zh 独有: \(zh.subtracting(en))")

        // 本轮新增 key 必须双语都在 (R3-2 菜单项 / R3-8 cookie 格式提示).
        for key in [
            "menu.metric.weekly_limit_boosted",
            "menu.metric.weekly_limit_unlimited",
            "error.stepfun.cookieFormat"
        ] {
            XCTAssertTrue(en.contains(key), "en.json 缺 key: \(key)")
            XCTAssertTrue(zh.contains(key), "zh-Hans.json 缺 key: \(key)")
        }
    }

    /// R3-2 端到端语义守卫: 菜单清单里的每个 label 都要有菜单标题 i18n key,
    /// 否则右键「显示指标」子菜单会裸秀 label 原文.
    func testAvailableMetricLabelsHaveMenuI18nKeys() throws {
        func translations(forResource name: String) throws -> [String: String] {
            let url = try XCTUnwrap(Bundle.main.url(forResource: name, withExtension: "json"))
            let data = try Data(contentsOf: url)
            return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: String])
        }
        let en = try translations(forResource: "en")
        let zh = try translations(forResource: "zh-Hans")
        for type in PlatformType.allCases {
            for label in ConfigService.availableMetricLabels(for: type) {
                let key = "menu.metric.\(label)"
                XCTAssertNotNil(en[key], "en.json 缺 \(key) (平台 \(type.rawValue) 的可勾选项)")
                XCTAssertNotNil(zh[key], "zh-Hans.json 缺 \(key) (平台 \(type.rawValue) 的可勾选项)")
            }
        }
    }
}
