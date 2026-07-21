# 可配置指标显示 - 设计文档

## 项目概述

**项目名称**: QuotaBar
**变更范围**: 菜单栏指标显示的可配置化
**核心改动**: 让用户能通过右键菜单选择每个平台要显示哪些指标（5 小时额度 / 周额度 / MCP 月度），并根据勾选数量自适应字号 / 布局；同时让 MiniMax 在周限额为 ∞ 时显示 ∞ 而不是隐藏。

**目标用户**: 使用 MiniMax、GLM API 的开发者，希望根据当前套餐灵活选择菜单栏显示哪些指标。

---

## 动机与背景

### 当前痛点

1. **MiniMax 套餐已切换为"无周限制"**：当前 service 在 `currentWeeklyStatus != 1` 时直接不返回周指标，菜单栏只显示一个 5h 数字，丢失了"周额度"这一行的视觉锚点。
2. **GLM 有 3 个指标**（5h、周、MCP），但 `StatusBarView` 只取 `metrics[0]` 和 `metrics[1]`，第 3 个直接被丢弃。用户既看不到 MCP，也无法选择看什么。
3. **菜单栏字号固定**：用户在中途尝试过改字号（曾回滚），希望"勾得少就大、勾得多就小"，但又不希望字号过大导致被菜单栏裁切。

### 设计目标

- 用户能按平台**独立勾选**要显示的指标
- 每个平台**最多勾 2 个**（再多数字太小看不见）
- 勾 1 个时**重排为大字居中**（用户实测驱动字号上限）
- MiniMax 在周限额为 ∞ 时**显示 ∞**，便于切回老套餐时自动恢复双圈

---

## 数据契约

### 不变项

- `UsageMetric { label, currentValue, totalValue?, unit, resetTime? }` 结构不变
- `PlatformUsageData { platform, displayName, metrics, lastUpdated, isHealthy }` 结构不变
- `PlatformAPIService` 协议签名不变

### 新增 1：∞ 虚拟 metric（service 层产生）

**触发条件**：`MiniMaxPlatformAPIService` 在 `model.currentWeeklyStatus != 1` 时，除了 5h metric，再 append 一个**周无限**的 metric。

**字段约定**：
- `label = "weekly_limit_unlimited"`
- `currentValue = 0`（不参与颜色判定）
- `totalValue = nil`（与现有 `formatBalance` "无 total" 分支兼容）
- `unit = "unlimited"`（UI 识别用，显示 ∞）
- `resetTime = nil`

**好处**：上层永远不需要"如果只有 1 个就显示 1 个"这种特例，按"metrics 列表里有啥画啥"。

### 新增 2：`ConfigService` 配置项

```swift
// 每个平台存用户勾选的 metric label, 顺序即显示顺序.
var enabledMetrics(for platform: PlatformType) -> [String]
func setEnabledMetrics(_ labels: [String], for platform: PlatformType)
```

**存储**：UserDefaults 单 key `"quotabar.platform.{rawValue}.enabledMetrics"`，存 `[String]`（label 数组）。

**线程安全**：与现有 `displayMode` / `activePlatform` 一致，加 `configLock` 保护。

**默认值**（首次安装 / 无配置时返回）：
- MiniMax：`["five_hour"]`
- GLM：`["five_hour", "weekly_limit"]`

**边界规则**：
- setter 拒绝空数组（保留上一次非空值；如果内存中也是空，回退到该平台的默认值）
- setter 拒绝长度 > 2 的数组（菜单层也应禁用）

**通知**：setter 成功后发本地通知 `Notification.Name.enabledMetricsChanged`，`StatusBarController` 监听并触发重绘。

---

## UI 渲染

### `StatusBarView` 改造

接收 `enabledMetrics: [String]?` 参数（默认 `nil` 时按"显示全部 metrics"兼容旧调用）。

**输入**：`platformData: PlatformUsageData?`、`enabledMetrics: [String]?`、`displayMode: DisplayMode`

**处理流程**：
1. `let visible = platformData?.metrics.filter { enabledMetrics?.contains($0.label) ?? true } ?? []`
2. 按 `enabledMetrics` 的顺序排序（保留用户期望顺序）
3. 根据 `visible.count` 选布局：

| 勾选数 | 布局 |
|---|---|
| 0 | 兜底显示 "—" 单字符 |
| 1 | 大字居中（字号放大、居中对齐） |
| 2 | 现状：左圆点 + 上下两行数字 |

### 字号自适应

- 2 个指标时：`size = 9pt`（当前值，不变）
- 1 个指标时：`size = 18pt`（当前 2 倍，作为起始值）
- **硬上限**：必须 ≤ `NSStatusBar.system.thickness - 4`（即 ≤ 18pt 留 2pt 内边距）
- **实测优先**：用户 16 寸 MacBook 实测后定夺。宁可不达 2 倍也别裁切
- 用 `font(.system(size:, weight:, design:))`，固定 `monospaced` 保证数字宽度稳定

### ∞ 渲染

- `unit == "unlimited"` 时 `Text("∞")`
- 颜色固定 `.secondary`（不参与绿/黄/红判定）
- 字号与同位置的百分比数字一致
- **不走 `formatBalance` 分支**：现有 `formatBalance(value, unit: nil)` 在 `totalValue == nil` 时输出数字（"1234"），所以 ∞ 渲染必须独立判断 `unit == "unlimited"` 在百分比格式化之前；不在 `formatBalance` 里改

### 颜色判定（不变，仅调整作用域）

只看 `visible.first`（即最重要的指标 5h）：

| remainingRatio | 颜色 |
|---|---|
| `< 0.10` | `.red` |
| `0.10 ..< 0.50` | `.yellow` |
| `≥ 0.50` | `.green` |
| ∞ 唯一指标时 | `.secondary` |

无 metrics（API 异常）→ `.secondary`。

### 兼容性

- `PopoverContentView`（点开后的详情面板）**不动** — 仍然显示全部 metrics
- `StatusBarController.updateAll(data:)` 在构造 `StatusBarView` 时传入 `enabledMetrics(platform:)`；钉选多平台时按各自平台的配置渲染

---

## 配置入口

### 右键菜单结构（追加 1 项）

```
立即刷新
─────────
显示设置    ▶
刷新频率    ▶
平台        ▶
显示指标    ▶    ← 🆕 新增
语言        ▶
─────────
关于
─────────
检查更新    ⌘U
打开更新页
─────────
退出        ⌘Q
```

### "显示指标"子菜单

```
显示指标    ▶
   ├── MiniMax    ▶
   │      ☑ 5 小时额度
   │      ☐ 周额度
   │      ☐ MCP 月度
   ├── GLM        ▶
   │      ☑ 5 小时额度
   │      ☑ 周额度
   │      ☐ MCP 月度
   └── ─────────
```

### 行为

| 操作 | 结果 |
|---|---|
| 勾选 / 取消某指标 | `ConfigService.setEnabledMetrics` → 发通知 → 菜单栏重绘 |
| 同一平台已勾 2 个 | 第 3 个菜单项 `state = .off` 且 `isEnabled = false`（不让勾） |
| 同一平台取消到 0 个 | setter 拒绝 → 菜单项状态不变，用户看到的勾选保留 |
| 切换语言 | 菜单文字跟着切换（中/英） |

### i18n 新增 key

- `menu.enabledMetrics` — "显示指标" / "Enabled Metrics"
- `menu.metric.{label}` — 每个 metric 在右键菜单里的本地化名（与 popover 已有的 `metric.{label}` 区分；popover 用 `metric.five_hour`，菜单用 `menu.metric.five_hour`）
- `metric.five_hour`（已存在） / `metric.weekly_limit`（已存在） / `metric.mcp_monthly`（已存在） / 新增 `metric.weekly_limit_unlimited` = "无限" / "Unlimited"

---

## 持久化与默认值

### 存储位置

`UserDefaults`，key 格式：`quotabar.platform.{platformRaw}.enabledMetrics`，值为 `[String]` 序列化为 JSON 数组。

### 默认值

| 平台 | 默认 enabledMetrics |
|---|---|
| MiniMax | `["five_hour"]` |
| GLM | `["five_hour", "weekly_limit"]` |

### 升级迁移

- 老用户没有这个 key → 直接返回默认值
- 不主动猜测历史配置（避免猜错）

### 配置损坏

- getter 读到非法 JSON → 返回默认值 + 静默覆盖坏值
- 不会崩溃

### 显示顺序

固定按 `["five_hour", "weekly_limit_boosted"|"weekly_limit"|"weekly_limit_unlimited", "mcp_monthly"]` 顺序展示，service 层已有排序（`GLMPlatformService` 写死 sort order；`MiniMaxPlatformService` 隐式顺序）。本次改动**不引入用户拖拽排序**（YAGNI）。

---

## 错误处理与边界

| 情况 | 行为 |
|---|---|
| 勾选到 0 个 | setter 拒绝，保留上次值 |
| 勾选到 3 个 | setter 拒绝，菜单层也禁用第 3 项 |
| metric label 不在当前 API 响应里 | filter 跳过，不显示，不报错 |
| 配置 JSON 损坏 | 返回默认值并覆盖 |
| 老配置迁移 | 不主动迁移，返默认值 |
| API 临时失败 | 复用旧缓存（已有逻辑） |
| 多平台独立配置 | 按平台分别存，互不影响 |
| 字号超菜单栏高度 | 实测定硬上限，宁可字号小也不裁切 |

---

## 测试策略

按 CLAUDE.md 要求 TDD：先写测试，再改代码。

| 测试 | 验证 |
|---|---|
| `ConfigService.enabledMetrics` 默认值 | MiniMax=`[five_hour]`、GLM=`[five_hour, weekly_limit]` |
| `ConfigService` 持久化 | 写 → 读 → 一致 |
| `ConfigService` 拒绝 | 写空数组被拒、写 3 个被拒 |
| `MiniMaxPlatformAPIService` ∞ metric | `currentWeeklyStatus != 1` → metrics 含 `weekly_limit_unlimited` |
| `MiniMaxPlatformAPIService` 正常 | `currentWeeklyStatus == 1` → metrics 含 `weekly_limit` 或 `weekly_limit_boosted` |
| `GLMPlatformAPIService` MCP | 响应有 `TIME_LIMIT` → metrics 含 `mcp_monthly` |
| `StatusBarView` 渲染 | 0/1/2 个指标时布局正确（snapshot 或 unit test） |
| 右键菜单 | 勾选 / 取消 → 配置更新 + 重绘 |
| `Notification.Name.enabledMetricsChanged` | 通知发布 + `StatusBarController` 收到后重绘 |
| i18n key | 中英文都覆盖所有 metric 名 |

---

## 实现顺序

1. **写测试**（按上述测试清单） — 锁死行为预期
2. **改 `ConfigService`** — `enabledMetrics` getter/setter + 默认值 + 通知
3. **改 `MiniMaxPlatformAPIService`** — 永远返回周 metric（有限 → 现有 label；无限 → `weekly_limit_unlimited`）
4. **改 `StatusBarView`** — 按勾选数重排 + 字号自适应 + ∞ 渲染
5. **改 `StatusBarController`** — 构造 `StatusBarView` 时传入 `enabledMetrics`；右键菜单加"显示指标"子菜单；监听通知触发重绘
6. **加 i18n key** — 中英文 metric 名 + 菜单文案
7. **实测** — build → 跑起来 → 各种勾选组合截图 → 字号不裁切验证（用户 16 寸 MacBook）
8. **写 / 更新 spec 文档 + README** — 同步文案

---

## 不在本次范围内（YAGNI）

- 勾选数量上限做成可配置（固定 2 即可）
- 拖拽排序 / 自定义每个平台的 metric 显示顺序（暂用固定顺序：5h → 周 → MCP）
- 给菜单栏图标加动画过渡（避免引入新视觉问题）
- popover 内详情面板的"只显示勾选的"过滤（popover 仍然全显示）

---

## 参考

- 现状 spec：`docs/superpowers/specs/2026-04-12-minimax-status-bar-design.md`
- 现状 spec：`docs/superpowers/specs/2026-04-30-multi-platform-support-design.md`
- 现状代码：
  - `Services/ConfigService.swift`
  - `Services/Platforms/MiniMaxPlatform/MiniMaxPlatformService.swift`
  - `Services/Platforms/GLMPlatform/GLMPlatformService.swift`
  - `Views/StatusBarView.swift`
  - `StatusBar/StatusBarController.swift`