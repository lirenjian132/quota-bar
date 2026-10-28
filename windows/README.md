# QuotaBar Windows 版

macOS 菜单栏应用 QuotaBar 的 Windows 移植版(本仓库 `windows` 分支)。常驻系统托盘,一眼查看 AI 平台 API 用量。

功能与 macOS 版对齐(分阶段):

- **4 个平台**:MiniMax、GLM(智谱)、基元律动(TokenRhythm)、Stepfun(阶跃星辰)
- **多账号**:同一平台可加多个账号,支持重命名/删除/排序,每个账号钉选独立托盘图标
- **可配置指标**:每账号最多勾 2 个指标显示在托盘图标上(5 小时额度/周额度/余额/套餐余量等)
- **状态颜色**:绿(充足)→ 黄(<50%)→ 红(<10% 或订阅失效/赠金临期)
- **中英双语**右键菜单
- 定时自动刷新(30s~10m 可配)+ 立即刷新

暂未移植(后续迭代):cookie 平台网页登录一键续期、Stepfun 会话自动续期、开机自启、自动更新。

## 环境要求

- Windows 10/11
- Python 3.10+(本仓库开发环境为 3.14)

## 安装与运行

```bash
cd windows
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

## 凭据获取方式

| 平台 | 凭据 | 获取方式 |
|---|---|---|
| MiniMax | API Key | 开放平台控制台 → API Key |
| GLM | API Key | open.bigmodel.cn 控制台 → API Key |
| Stepfun | 登录 cookie 两段 | 登录 platform.stepfun.com → DevTools → Application → Cookies,复制 `Oasis-Webid` 与 `Oasis-Token`,按 `Oasis-Webid=…; Oasis-Token=…` 格式粘贴 |
| 基元律动 | tr_session 会话值 | 登录 tokenrhythm.studio → DevTools → Cookies,复制 `tr_session` 值(约 30 天有效;**不要在网页点退出登录**,会使会话作废) |

右键托盘图标 →「平台」→「添加账号…」→ 配置框粘贴凭据。

## 数据存储

- `%APPDATA%\QuotaBar\settings.json` — 账号与配置
- `%APPDATA%\QuotaBar\keys.json` — 凭据(明文存储,请勿分享)

## 开发

```bash
cd windows
.venv\Scripts\python -m pytest tests/ -q
```

代码结构:

```
quotabar/
  models.py       平台/指标/错误等核心模型(对齐 Swift 版 PlatformProtocol)
  config.py       配置与凭据存储(settings.json / keys.json)
  render.py       渲染纯函数(状态色/托盘文本/数值格式化)
  i18n.py         中英文案(复用原版 en.json / zh-Hans.json)
  net.py          网络抽象(可注入 Mock)
  manager.py      多账号编排(每账号独立缓存、并发拉取)
  platforms/      四个平台取数服务(接口契约 1:1 移植自 Swift 版)
  ui/             托盘图标与详情窗(PyQt5)
tests/            pytest 单测
```
