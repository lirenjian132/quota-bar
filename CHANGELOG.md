# Changelog

本项目的所有重要变更记录。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [2.2.0] - 未发布

> 2.1.0 曾以 fork DMG 形式存在（未进官方 appcast），内容并入本版。

### Added
- **一键续期**：cookie 型平台（Stepfun/基元律动）会话过期后，app 内弹内嵌登录窗（WKWebView 加载官网登录页），用户在网页里正常登录（含滑块/验证码，无自动化），点「完成并提取」即自动提取 cookie 写入凭据存储并刷新数据，不再需要手动抄 DevTools。入口：右键菜单「平台」→ 账号 →「重新登录…」、popover 错误区「重新登录」按钮（均仅 cookie 型平台显示）
- **多账号管理**：同一平台可添加多个账号（如主备两个 MiniMax），右键菜单「平台」→「添加账号…」；支持重命名、删除（带确认，连带清理凭据）、左移/右移调整状态栏显示顺序；新建账号取消未填 key 时自动回收，不留空壳；已删除账号的 id 永不复用（防凭据撞 key）
- **基元律动（TokenRhythm）平台：余额型多账号监控**。查询网页端用户中心同款接口 `GET /api/wallet/summary`，鉴权用登录会话 `tr_session` cookie（当作凭据存入 FileKeyStore，文件权限 0600）；每账号显示整数余额（四舍五入），低于 10 元状态栏变红提示换号
- **赠金到期监控**：余额由多笔赠金叠加、每笔独立到期，到期即蒸发。追加查询 `GET /api/wallet/expiring-credits` 取最早到期时刻：弹窗显示"最早到期"日期，状态栏 7 天内变黄、3 天内变红（到期警示优先于低余额）；该接口失败时优雅降级为纯余额显示
- TokenRhythm 凭据即"登录一次抄一次"：浏览器登录 → 从 DevTools/CDP 抄 `tr_session` 值（约 30 天有效）→ 粘贴到账号配置框即可；**绝不能在网页点「退出登录」**（会在服务端作废会话），换号采集用"本地清 cookie + 直接登录下一账号"流程
- TokenRhythm 请求按账号 5 分钟节流（service 内缓存），约 10 个账号高频刷新也不易触发风控；401 时显示"认证失败"提示重粘
- 配置界面按平台区分文案：TokenRhythm 输入框提示"粘贴 tr_session 会话值"而非"API Key"
- **Stepfun（阶跃星辰）套餐平台**：显示套餐月 Credits 余量百分比 + 月度重置日，订阅失效标红。接口契约来自官方开源客户端 Step Code（`platform.stepfun.com` gRPC-Connect `step.openapi.devcenter.Dashboard`：`QueryStepPlanRateLimit` + `GetStepPlanStatus`），非公开 API；此前曾实现 Oasis-Token 内部接口版（参照 CodexBar 逆向文档）并撤下，本版以 Step Code 开源客户端的 gRPC-Connect 契约重新实现
- Stepfun 鉴权 = 网页 SSO 双 cookie（`Oasis-Webid` + `Oasis-Token`）+ `oasis-appid/platform/webid` 三头（缺头会被服务端判「令牌挪用」401）。凭据同 TokenRhythm 存 FileKeyStore；**双 JWT 结构：用户票据 30 分钟（服务端不校验）+ 设备票据 30 天（实际有效期，实测跨夜有效）**，登录一次粘贴即可
- 凭据采集流程：登录 platform.stepfun.com → 打开 DevTools/应用程序面板（Application → Cookies）→ 复制 `Oasis-Webid` 与 `Oasis-Token` 两个值，按 `Oasis-Webid=…; Oasis-Token=…` 两段格式粘贴到账号配置框
- Stepfun 非 credit 套餐族（`plan_family != 2`）降级显示 5 小时 / 周窗口剩余率（`five_hour_usage_left_rate` / `weekly_usage_left_rate`），右键菜单「显示指标」可勾选；cookie 格式错误早失败
- 可配置指标显示：每个平台可勾选最多 2 个指标（5 小时窗口 / 周限额 / MCP 月度等）显示在菜单栏，右键菜单「显示指标」多选
- MiniMax 无限套餐（∞）渲染：weekly_status 非 1 时显示 ∞ 而非百分比
- MiniMax 周额度加成（boost）检测：加成套餐显示专属标签
- 立即刷新菜单项：一键清除所有平台缓存并重新拉取（平台卡住时自愈）
- 开机启动项：状态栏右键菜单开关（基于 SMAppService，自 v2.0.4 沿用保留）

### Changed
- 平台精简：移除 DeepSeek / MiMo；Stepfun 由旧内部接口方案替换为新契约实现（Step Code 开源客户端 gRPC-Connect），现支持 MiniMax + GLM + TokenRhythm + Stepfun
- API key / 会话凭据存储改用应用私有文件（FileKeyStore，文件权限 0600），彻底消除钥匙串登录密码弹窗：启动时自动从 Keychain 一次性迁移（仅最后一次授权），此后不再触碰 Keychain
- 测试隔离根治：测试进程改走独立 UserDefaults suite + 内存 Keychain，不再触碰用户真实配置，也不再因构建签名变化触发系统登录密码弹窗
- 老用户升级：按平台类型的旧配置 key 一次性迁移到按账号实例的新结构（含启用/钉选/指标勾选/激活状态），已有 key 无需迁移
- 余额型指标（无 total 的绝对金额）统一整数显示：状态栏与弹窗同口径（不再出现"状态栏 254 / 弹窗 253.76"的割裂）；仅影响余额型，百分比/次数型不变
- 右键菜单「显示指标」可选项按平台分组（此前为全局硬编码列表，余额型平台会得到空子菜单）；MiniMax 周额度勾选（族名）后套餐在标准/加成/∞ 间切换时按同族匹配渲染（取产出的实际 label），不再被精确匹配过滤；勾选与产出同族匹配后仍无交集才回退前 2 个指标
- 状态栏圆点颜色统一按 `isHealthy` 判定（此前百分比够看时直接返回绿）：订阅失效/停订即使余量 >50% 也标红；MiniMax 5 小时窗口 <15% 同样红（与弹窗「状态异常」同口径）；空 metrics 优先兜底为灰色（无数据不谎报红）
- 弹窗 `isHealthy=false` 行的文案由「偏低」改为「状态异常」（Stepfun 停订等非低余额场景不再误导）
- 401/认证失败文案按凭据类型分派：cookie 型平台（TokenRhythm/Stepfun）提示重新登录粘贴，API key 型平台（MiniMax/GLM）提示检查 API Key
- 版本号体系：合并 v2.0.4 的发布线，build number 单调递增（→ 7）

### Fixed
- **迁移安全加固**：Keychain 条目只有在文件写入确认成功后才删除（曾因测试宿主误执行迁移销毁过用户 key，已加双层测试守卫 + 写失败保留源数据 + 回归测试）
- **Stepfun 非 credit 套餐状态栏恒显 "--" 无法自救**：右键菜单「显示指标」可勾选项补齐 `five_hour`/`weekly_limit`（默认勾选仍为 `credits`，credit 套餐是主场景）；勾选与产出无交集时状态栏回退显示前 2 个指标（防呆，不再恒显 "--" 逼用户翻菜单）
- **MiniMax/GLM「显示指标」清单与 service 产出错位**：MiniMax 周额度三态（标准 `weekly_limit` / 加成 `weekly_limit_boosted` / 无限 `weekly_limit_unlimited`）全部可勾（此前加成/无限套餐用户勾"周额度"被精确匹配过滤 → 状态栏 "--"），移除 MiniMax 不产出的"MCP 月度"死选项；GLM 清单为 5 小时 + 周限额 + MCP 月度
- **已下架指标 label 占用勾选名额**：老用户落盘的 `enabledMetrics` 含已下架 label（如 MiniMax 的 `mcp_monthly`）时，读取与写入均按各平台可勾选清单（`ConfigService.availableMetricLabels`）过滤，不再占满 2 个名额、菜单其余项全灰且无法清理（全部下架时回退平台默认勾选）
- **删除账号实例时在途任务未取消**：删除即取消该实例的 fetchUsage 任务，`fetchAllUsage` 写回前校验实例仍存在，双向防止"已删除的账号"数据复活；`fetchAllUsage` 启动时取消全部 per-instance 在途任务（全量刷新替代局部刷新）
- **换 API key 与在途请求竞态（双向）**：`saveAPIKey` 取消该实例在途的旧 fetch（旧 key 结果被丢弃）+ 取消在途的 `fetchAllUsage` 全量任务（旧 key 的全量结果不得覆盖刚换的新 key 数据）+ 清 service usage 缓存（不清则 5 分钟内拉到的仍是旧 key 结果）；`fetchAllUsage` 写回前逐项检查取消；生产环境中任务取消会中止在途请求，旧账号数据既不会覆写状态栏也不会落进 service 缓存（300s 窗口），不再出现最长 300s 显示旧账号数据
- **`isLoading` 永久残留转圈**：`fetchAllUsage` 只标记"实际会请求"的实例（已启用且已配置，禁用实例被跳过不发请求却曾标记 loading 且无人清除）；`fetchUsage` 退出即清 loading（defer 覆盖成功/失败/取消全部路径），取消后不再永久转圈
- **GLM `limits` 处理后为空时静默无数据**：limits 有元素但全未知 type、或全部因字段缺失被跳过时改报 `apiError`（文案走 i18n key `error.glm.emptyLimits`，中英双语）；`percentage` 缺失的 TOKENS_LIMIT、`usage`/`remaining` 缺失的 TIME_LIMIT 逐条跳过，不再谎报 100% 剩余 / 0 次可用
- **菜单勾选与落盘状态脱节**：勾选数量超上限被 `setEnabledMetrics` 静默拒绝时，菜单按落盘后的实际返回值刷新，消除用户看到的"临时勾选"（重开菜单消失）；拒写时也发 `.enabledMetricsChanged` 通知，监听方能感知"设置被拒"
- **Stepfun 订阅状态语义修正**：`GetStepPlanStatus` 外层 `status != 1`（查询异常/账户停用）时不再采信 `subscription` 字段判失效，静默降级为仅显示余量（把「查询失败」误判为「停订」的 bug）
- Stepfun cookie 粘错格式（缺 `Oasis-Webid`）由 `invalidResponse`（「无效的响应数据」）改报 `apiError` 并给出粘贴格式提示（文案走 i18n key `error.stepfun.cookieFormat`，中英双语）
- GLM `limits` 缺失/空数组由"空 metrics 静默显示无数据"改报 `invalidResponse`（用户可分清"没额度"与"接口异常"）
- legacy 清理不再删除现役平台 `stepfun` 的 UserDefaults 残留（2.0.x 直升级用户的老配置改由实例迁移接管，避免先删后搬不到）
- **Stepfun 自动刷新覆盖用户新保存的凭据（CAS 缺失，P0）**：预刷新 POST 在途期间用户在主线程保存新 key / 一键续期写新凭据，刷新完成时旧写法无条件写回 → 用户的新凭据被旧凭据刷出的串覆盖（表现为"刚改的 key 自己变回去"）。修复：写回前 CAS 校验 `store.apiKey == 本次刷新用的凭据`，不等则放弃写回（别人的更新优先）；主请求与落库都以用户新值为准
- **`PlatformConfigStore.setAPIKey`/`resetAPIKey` 非线程安全（P0）**：四步（keychain 写 → 内存 apiKey → defaults 落盘 → 清明文）无锁，自动刷新（TaskGroup 子线程）与用户保存（主线程）可交错成"keychain 是新值 / 内存是旧值"甚至 defaults 残留明文的分裂态。修复：`writeLock`（NSLock）包住整个方法，锁内不 await、无重入
- **同实例 Stepfun 会话双刷新 POST（P1）**：预刷新无单飞门控，`saveAPIKey` 后的新 fetch 与 `fetchAllUsage` 在途可对同一账号双发 RefreshToken（多耗一次令牌轮换，且两次写回竞态）。修复：`PlatformManager` per-instance in-flight 刷新任务字典（仿 services 字典 + serviceLock 模式），已有在途则 await 复用其结果，任务结束摘除字典项；门控键带凭据维度（在途刷新期间换了新凭据时不误复用旧结果，否则新凭据会被旧凭据刷出的串覆盖）；与 CAS 叠加后落库值仍一致
- **Stepfun `exp` 契约漂移静默退化（P1）**：JWT payload 的 `exp` 只接受 JSON 数字，服务端改成字符串 `"1790000000"` 时静默不刷新，拖到主请求 401 才暴露。修复：数字优先、字符串 `Double(s)` fallback；非数字字符串仍按"无 exp"处理
- Stepfun 预刷新接入处同一凭据解析两遍（needsRefresh + refresh 各 parse 一次）合并为一次 parse，`needsRefresh` 增加接收已解析结果的重载；HTTP 200 但 `accessToken` 缺失/null 判刷新失败不阻断主请求（原无测试覆盖，已补齐，含 `refreshToken` 缺失/null 同路径）

## [2.0.4] - 2026-07-16

### Added
- 开机时启动开关（状态栏右键菜单，基于 SMAppService）
- API key 持久化迁移到 macOS Keychain，UserDefaults 明文清除（含一次性迁移与降级策略）
- Sparkle appcast 构建版本号自动化（update-appcast 脚本 + 测试）

## [2.0.3] - 2026-06-03

### Added
- 状态栏右键菜单「关于」项（版本信息 + 打开发布页）

### Fixed
- fetchAllUsage() 异步化，消除虚假 await 警告
- 鼠标拖出状态栏视图时隐藏按压高亮

## [2.0.2] - 2026-06-02

### Fixed
- MiniMax 对齐 cc-switch 新版 API 适配
- 清理死 i18n key、修正过期测试断言

## [2.0.0] - 2026-05-25

### Added
- 多平台架构：协议驱动（PlatformAPIService），新增 GLM / DeepSeek / SiliconFlow / OpenRouter / Novita 等平台
- Sparkle 自动更新（EdDSA 签名校验）
- DMG 打包脚本（含 Applications 拖拽安装别名）

## [1.0.x] - 2026-04 ~ 2026-05

- 1.0.0：首个版本，MiniMax 用量菜单栏显示（原名 MiniMaxBar）
- 1.0.2：显示模式切换（已用 / 剩余）、中英双语 i18n
- 1.0.3：刷新间隔配置、平台区域（国内 / 国际）支持
