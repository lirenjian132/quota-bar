import Foundation

/// cookie 型平台的续期契约: 登录页 URL + 要从 WKHTTPCookieStore 提取的 cookie 名
/// + 存储形态 (storageForm).
///
/// cookie 名顺序即拼装顺序 (StepFun 的凭据串对字段顺序无要求, 但保持稳定顺序
/// 便于用户肉眼核对与回归测试). API key 型平台 (MiniMax/GLM) 无会话可续,
/// `WebLoginRenewalConfig.platform(for:)` 对它们返回 nil.
struct WebLoginPlatform: Hashable {
    let loginURL: URL
    let cookieNames: [String]
    let storageForm: StorageForm
}

/// 凭据串 → PlatformConfigStore 的存储形态 (编译期契约, 每个平台显式声明).
///
/// 两个 cookie 平台的 template 鉴权方式不同 (见 Resources/ConfigTemplates):
///   - StepFun: auth_prefix 为空, service 原样把 apiKey 放 Cookie 头 → `.whole`
///     整串即存储形态;
///   - TokenRhythm: auth_prefix 是 "tr_session=", service 拼前缀后再放 Cookie 头
///     → `.bareValue` 只存裸值, 否则请求头会变成 "tr_session=tr_session=xxx".
/// 新增平台必须在 platform(for:) 里显式选一种 — 编译期强制, 不靠隐式推断
/// (历史上靠 "cookieNames.count == 1" 猜, 声明错了会静默存错形态).
enum StorageForm {
    /// 整串凭据即存储形态 ("Oasis-Webid=web; Oasis-Token=tok").
    case whole
    /// 只存裸 cookie 值 (去掉 "name=" 前缀, 配合 template 的 auth_prefix).
    case bareValue
}

enum WebLoginRenewalConfig {
    /// cookie 型平台 (stepfun / tokenrhythm) 返回续期契约; 其余平台 nil.
    ///
    /// 每个 case 显式声明 storageForm — 新增平台若漏声明编译不过, 存储形态
    /// 不再靠 cookieNames.count 隐式推断. URL 用 guard let: 配置录入写错字符时
    /// 返回 nil (调用方 no-op), 不 crash — 这里是未来 URL 可配置化的落点.
    static func platform(for type: PlatformType) -> WebLoginPlatform? {
        switch type {
        case .stepfun:
            // 网页 SSO 双 cookie: Oasis-Webid (设备票据, 30 天) + Oasis-Token (用户票据).
            // 登录页用首页而不是 /login: 已登录会直达控制台 (cookie 已在), 未登录会跳登录.
            // 存储形态 .whole: auth_prefix 为空, 整串 cookie 原样进 Cookie 头.
            guard let url = URL(string: "https://platform.stepfun.com/") else { return nil }
            return WebLoginPlatform(
                loginURL: url,
                cookieNames: ["Oasis-Webid", "Oasis-Token"],
                storageForm: .whole
            )
        case .tokenrhythm:
            // 存储形态 .bareValue: template 的 auth_prefix = "tr_session=", 只存裸值,
            // 否则请求头会拼成 "tr_session=tr_session=xxx".
            guard let url = URL(string: "https://tokenrhythm.studio/login") else { return nil }
            return WebLoginPlatform(
                loginURL: url,
                cookieNames: ["tr_session"],
                storageForm: .bareValue
            )
        case .minimax_cn, .glm_cn:
            return nil
        }
    }

    /// 把抓到的 cookie 拼成凭据串 ("Oasis-Webid=x; Oasis-Token=y"), 缺任一必需 cookie 返回 nil.
    ///
    /// 规则: 按 platform.cookieNames 顺序, "name=value" 以 "; " 连接; 全部存在且非空白
    /// 才返回. cookie 值是原样拼接, 不解析 — 值里的 "=" / ";" 原样保留 (cookie 值
    /// 不允许裸 ";" 出现, 真有也是服务端编码后的形态, 拼装层不加工).
    /// 值空白视为没登录 (cookie 存在但没种上值), 同样返回 nil — 调用方据此提示
    /// "未检测到登录凭据".
    static func credentialString(from cookies: [String: String], platform: WebLoginPlatform) -> String? {
        var parts: [String] = []
        for name in platform.cookieNames {
            guard let value = cookies[name], !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                return nil
            }
            parts.append("\(name)=\(value)")
        }
        return parts.joined(separator: "; ")
    }

    /// 凭据串 → 写入 PlatformConfigStore 的存储形态, 按 platform.storageForm 机械转换.
    ///
    ///   - .whole: 整串原样入库 (StepFun: auth_prefix 为空, service 原样放 Cookie 头);
    ///   - .bareValue: 去掉第一个 "name=" 前缀只存裸值 (TokenRhythm: auth_prefix
    ///     是 "tr_session=", 存整串会让请求头变成 "tr_session=tr_session=xxx").
    /// 前缀形状不符 (防御: 不产出空凭据) 时原样返回.
    static func storageCredential(from credential: String, platform: WebLoginPlatform) -> String {
        switch platform.storageForm {
        case .whole:
            return credential
        case .bareValue:
            guard let name = platform.cookieNames.first,
                  credential.hasPrefix("\(name)=") else {
                return credential
            }
            return String(credential.dropFirst(name.count + 1))
        }
    }
}
