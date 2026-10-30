# -*- coding: utf-8 -*-
"""第 5 轮全量审查(四席并行)的回归测试。

钉住本轮修复:配置读取的瞬时 IO 错误防护、实例 id 去重、add_instance 消毒、
set_api_key 存在性校验、重定向禁跟随、深层嵌套 JSON、UI 联动(面板刷新)、
登录提取自动保存、停用账号可查看、"--%" 防护、错误卡按钮口径、i18n 键集同步。
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("PyQt5")

import quotabar  # noqa: E402
from quotabar.config import ConfigService  # noqa: E402
from quotabar.models import (  # noqa: E402
    LABEL_FIVE_HOUR,
    ErrorKind,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
from quotabar.net import Request, Response  # noqa: E402
from quotabar.platforms._base import parse_json  # noqa: E402

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # 强制离屏:外部环境变量不能把测试带成真开窗


# ------------------------------------------------------------ 配置读取防护

def test_missing_settings_is_fresh_start_without_backup(tmp_path):
    """文件不存在 = 首次运行:回落默认,不留 .corrupt/.unreadable"""
    home = tmp_path / "home"
    home.mkdir()
    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert not (home / "settings.json.corrupt").exists()
    assert not (home / "settings.json.unreadable").exists()


def test_unreadable_settings_blocks_saves_and_keeps_file(tmp_path, monkeypatch):
    """真实读失败(权限/占用):只读运行、拒绝保存、内存与磁盘都不变。

    不能用"目录冒充 settings.json"来制造失败:即使删掉禁写防护,os.replace
    本身也会失败,测试照样绿(变异验证已证实是假覆盖)——这里直接让 open 抛权限错。
    """
    home = tmp_path / "home"
    home.mkdir()
    real_settings = {
        "instances": [{"id": "glm_cn-1", "platform_type": "glm_cn", "display_name": "A"}],
        "globals": {"display_mode": "remaining"},
    }
    (home / "settings.json").write_text(json.dumps(real_settings), encoding="utf-8")
    raw_before = (home / "settings.json").read_bytes()

    import builtins

    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file).replace("\\", "/").endswith("settings.json"):
            raise PermissionError(13, "locked by another process")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    config = ConfigService(home=str(home))

    assert config.read_failed() is not None  # UI 据此给可见提示
    assert config.instances() == []  # 按默认值只读运行
    assert (home / "settings.json").read_bytes() == raw_before  # 原文件一字未动

    # 禁写:抛错且**内存不被改写**(不会留下幽灵设置/账号)
    with pytest.raises(OSError):
        config.set_display_mode("used")
    assert config.display_mode() == "used"  # 失败后仍是默认值,不是"已改成 used"

    # 读取恢复后 reload 解锁,真实配置回来,保存恢复正常
    monkeypatch.setattr(builtins, "open", real_open)
    config.reload()
    assert config.read_failed() is None
    assert [i.id for i in config.instances()] == ["glm_cn-1"]
    assert config.display_mode() == "remaining"
    config.set_display_mode("used")
    assert config.display_mode() == "used"


def test_add_instance_during_read_failure_leaves_memory_untouched(tmp_path, monkeypatch):
    """读失败期间新增账号:抛错且内存里不出现"幽灵账号"(重启就消失的那种)"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text('{"instances": []}', encoding="utf-8")

    import builtins

    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file).replace("\\", "/").endswith("settings.json"):
            raise PermissionError(13, "locked")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    config = ConfigService(home=str(home))

    with pytest.raises(OSError):
        config.add_instance(PlatformType.GLM_CN)
    assert config.instances() == []  # 内存与磁盘一致(都为空)


def test_unknown_root_keys_do_not_crash(tmp_path):
    """根层未知键里的深层嵌套垃圾也要被丢弃:不能让启动崩在 deepcopy"""
    home = tmp_path / "home"
    home.mkdir()
    deep = "[" * 500 + "]" * 500
    (home / "settings.json").write_text('{"instances": [], "junk": ' + deep + '}', encoding="utf-8")

    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert config.read_failed() is None
    config.set_display_mode("remaining")  # 白名单丢弃垃圾后保存照常
    assert config.display_mode() == "remaining"


def test_corrupt_settings_still_backs_up_as_corrupt(tmp_path):
    """JSON 损坏走原有 .corrupt 备份路径,不受新逻辑影响"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{not json", encoding="utf-8")
    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert (home / "settings.json.corrupt").exists()


# ------------------------------------------------------------ 数据自愈与消毒

def test_duplicate_instance_ids_are_healed_on_load(tmp_path):
    """重复实例 id(手改坏了):保留第一条,其余剔除并留备份"""
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "instances": [
            {"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "A"},
            {"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "B"},
        ]
    }
    (home / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    config = ConfigService(home=str(home))
    assert [i.display_name for i in config.instances()] == ["A"]
    assert (home / "settings.json.corrupt").exists()  # 剔了东西要留备份


def test_add_instance_sanitizes_display_name(tmp_path):
    """加账号的名字与改名同口径:代理字符消毒,落盘不再炸"""
    home = tmp_path / "home"
    config = ConfigService(home=str(home))
    inst = config.add_instance(PlatformType.MINIMAX_CN, "主\ud800力")
    inst.display_name.encode("utf-8")  # 不抛 UnicodeEncodeError
    config.set_display_mode("remaining")  # 后续写入照常成功
    assert config.display_mode() == "remaining"


def test_reorder_instances_deduplicates_input(tmp_path):
    """传入重复 id 的排序序列:同一实例不能被排进列表两次"""
    config = ConfigService(home=str(tmp_path / "home"))
    a = config.add_instance(PlatformType.MINIMAX_CN, "A")
    b = config.add_instance(PlatformType.MINIMAX_CN, "B")
    config.reorder_instances([b.id, a.id, a.id, b.id])
    assert [i.id for i in config.instances()] == [b.id, a.id]


def test_set_enabled_metrics_deduplicates(tmp_path):
    """重复勾选同一指标:去重保序,不出现「菜单勾 2 个面板显 1 个」的错位"""
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)
    config.set_enabled_metrics(inst.id, ["five_hour", "five_hour"])
    assert config.enabled_metrics(inst.id) == ["five_hour"]


def test_set_api_key_rejects_missing_instance(tmp_path):
    """给不存在的实例写凭据:抛 KeyError,不留孤儿 key"""
    config = ConfigService(home=str(tmp_path / "home"))
    with pytest.raises(KeyError):
        config.set_api_key("minimax_cn-404", "sk-secret")
    assert config.api_key("minimax_cn-404") == ""


# ------------------------------------------------------------ 网络与解析

def test_requests_network_disables_redirects(monkeypatch):
    """手工 Cookie 头不能跟随跨域重定向外泄:allow_redirects 必须是 False"""
    import quotabar.net as net_module

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return Response(status_code=200, text="{}")

    monkeypatch.setattr(net_module.requests, "request", fake_request)
    net_module.requests_network(Request(url="https://example.com/api"))
    assert captured.get("allow_redirects") is False


def test_parse_json_recursion_error_is_decoding():
    """超深嵌套 body 报「解析错误」而不是被兜成误导性的「网络错误」"""
    with pytest.raises(PlatformError) as e:
        parse_json("[" * 100000 + "]" * 100000)
    assert e.value.kind == ErrorKind.DECODING


# ------------------------------------------------------------ UI 联动(沿用 offscreen Qt)

from PyQt5.QtWidgets import QLabel, QPushButton  # noqa: E402

from quotabar.i18n import get_i18n  # noqa: E402
from quotabar.manager import PlatformManager  # noqa: E402
from quotabar.ui.detail_window import DetailWindow  # noqa: E402
from quotabar.ui.tray import TrayApp  # noqa: E402


def _prepare_qt_env() -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import PyQt5

    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(
        os.path.dirname(PyQt5.__file__), "Qt5", "plugins", "platforms"
    )


_prepare_qt_env()


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tray(tmp_path):
    from tests.conftest import FakeNetwork

    config = ConfigService(home=str(tmp_path / "home"))
    # 必须注入 FakeNetwork:登录自动保存等路径会触发真实取数线程,
    # 默认的 requests_network 会拿假凭据打生产接口(非 hermetic)
    manager = PlatformManager(config, network=FakeNetwork())
    return TrayApp(config, manager)


def test_config_change_refreshes_overlay(qapp, tray, monkeypatch):
    """删除/停用/排序/改指标/切显示模式后面板必须立刻重画(不能等下个刷新周期)"""
    calls = []
    monkeypatch.setattr(tray._overlay, "refresh", lambda: calls.append(1))
    tray._on_config_changed()
    tray._set_display_mode("remaining")
    assert len(calls) == 2


def test_batch_fetch_failure_marks_unfed_accounts(qapp, tray, monkeypatch):
    """整批取数失败:无数据的账号要挂上错误,不能 loading 消了却毫无反馈"""
    from quotabar.manager import FetchResult

    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    tray.config.set_api_key(inst.id, "sk-test")
    monkeypatch.setattr(tray, "_rebuild_trays", lambda: None)
    tray._on_fetch_done(
        ([FetchResult(instance_id="", error=PlatformError(ErrorKind.NETWORK, "pool"))], [inst.id], {inst.id: 1})
    )
    assert tray.error(inst.id) is not None


def test_tooltip_fingerprint_includes_stale(qapp):
    """stale 变化必须进内容指纹:刷新失败后橙点要能出现,不能因指标没变被跳过。

    用**两个不同对象**传两次:若指纹不含 stale,第二次会被判"内容没变"直接早退,
    `_sig` 不变;原地改同一对象再断言会恒真(第一版测试就栽在这)。
    """
    from PyQt5.QtCore import QRect

    from quotabar.ui.overlay import TipAccount, _TooltipWindow

    tip = _TooltipWindow()
    rect = QRect(0, 900, 100, 20)  # 屏幕底缘:卡片往上弹
    fresh = TipAccount(initial="M", name="a", color="#ffffff")
    tip.show_accounts([fresh], rect)
    sig_before = tip._sig

    stale = TipAccount(initial="M", name="a", color="#ffffff", stale=True)
    tip.show_accounts([stale], rect)
    assert tip._sig != sig_before  # 指纹纳入 stale
    assert tip._accounts[0].stale is True
    tip.hide()


def test_chip_text_never_shows_double_percent(qapp, tray):
    """inf/NaN 的托盘文本是 "--":不能再拼 % 变成 "--%"。"""
    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    tray.config.set_api_key(inst.id, "sk-test")
    tray._data[inst.id] = PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id=inst.id,
        display_name="x",
        metrics=[UsageMetric(LABEL_FIVE_HOUR, float("inf"), 100, "%")],
    )
    _, text = tray._overlay._chip_texts(inst)
    assert text == "--"


def test_detail_falls_back_to_active_account(qapp, tray):
    """未指定账号时回落激活账号(不是列表第一个):托盘主图标显示的就是它。

    第一版修复把 `active in enabled` 比较写成了 str in [实例列表](恒 False),
    回落永远选第一个启用账号 — 主图标数值与详情窗内容会错位。
    """
    a = tray.config.add_instance(PlatformType.MINIMAX_CN, "甲")
    b = tray.config.add_instance(PlatformType.MINIMAX_CN, "乙")
    tray.config.set_active_instance_id(b.id)  # 激活第二个

    detail = DetailWindow(tray)  # _selected_id 为 None → 走回落
    assert detail._selected_id == b.id


def test_detail_disabled_account_stays_viewable(qapp, tray):
    """点停用账号的「查看详情」:留在该账号,不被重定向到别的账号"""
    a = tray.config.add_instance(PlatformType.MINIMAX_CN, "启用")
    b = tray.config.add_instance(PlatformType.MINIMAX_CN, "停用")
    tray.config.set_api_key(a.id, "sk-test")
    tray.config.set_enabled(b.id, False)

    detail = DetailWindow(tray)
    detail._selected_id = b.id
    detail._build_ui()

    assert detail._selected_id == b.id
    names = [lbl.text() for lbl in detail.findChildren(QLabel)]
    assert "停用" in names


def test_error_card_renew_button_only_for_unauthorized(qapp, tray):
    """错误卡的「重新登录」只在未授权时出现:网络错误引导重登是无效指引。

    只看 errorCard 里的按钮:页脚的「重新登录」是常驻入口(cookie 平台),不在口径内。
    """
    from PyQt5.QtWidgets import QFrame

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    tray.config.set_api_key(inst.id, "Oasis-Webid=w; Oasis-Token=t")

    detail = DetailWindow(tray)
    detail._selected_id = inst.id

    def error_card_buttons():
        from PyQt5.QtWidgets import QApplication

        QApplication.processEvents()  # 冲掉上一轮 deleteLater 的旧卡,别拿残影断言
        cards = [f for f in detail.findChildren(QFrame) if f.objectName() == "errorCard"]
        assert cards, "错误卡没建出来"
        return [b.text() for b in cards[-1].findChildren(QPushButton)]

    tray._errors[inst.id] = PlatformError(ErrorKind.NETWORK, "boom")
    detail._build_ui()
    assert get_i18n().t("popover.renewSession") not in error_card_buttons()

    tray._errors[inst.id] = PlatformError(ErrorKind.UNAUTHORIZED)
    detail._view_sig = None
    detail.reload()
    assert get_i18n().t("popover.renewSession") in error_card_buttons()


def test_login_extraction_auto_saves_credential(qapp, tray, monkeypatch, tmp_path):
    """浏览器提取成功后凭据立即落盘:取消/关窗不再丢,新建账号不再被当空壳回收"""
    import quotabar.ui.browser_login as browser_login_module
    import quotabar.ui.detail_window as detail_window_module

    sessions = []

    class _FakeSession:
        def __init__(self, platform):
            self.closed = False
            sessions.append(self)

        def start(self):
            return True

        def credential(self):
            return "Oasis-Webid=w; Oasis-Token=t"

        def close(self):
            self.closed = True

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def set_phase(self, text):
            pass

        def show(self):
            pass

        def isVisible(self):
            return True

        def setWindowModality(self, mode):
            pass

        def result(self):
            return 0

        def deleteLater(self):
            pass

        def exec_(self):
            from PyQt5.QtWidgets import QDialog

            return QDialog.Accepted

    monkeypatch.setattr(browser_login_module, "find_browser", lambda: "C:/fake/chrome.exe")
    monkeypatch.setattr(browser_login_module, "BrowserLoginSession", _FakeSession)
    monkeypatch.setattr(detail_window_module, "_WaitLoginDialog", _AcceptedDialog)
    monkeypatch.setattr(detail_window_module, "QMessageBox", type(
        "_NoBox", (), {"warning": staticmethod(lambda *a, **k: None), "critical": staticmethod(lambda *a, **k: None)}
    ))

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    tray._pending_instance_id = inst.id

    detail = DetailWindow(tray)
    detail._open_config(inst.id)
    detail._open_login(inst.platform_type)

    assert tray.config.api_key(inst.id) == "Oasis-Webid=w; Oasis-Token=t"
    assert tray._pending_instance_id is None  # 已保存:不会被空壳回收
    assert tray.config.is_configured(inst.id)
    assert sessions and sessions[0].closed is True  # 浏览器会话必须收尾
    # "立即落盘"要看磁盘:重开一个实例从盘上读
    reloaded = ConfigService(home=str(tmp_path / "home"))
    assert reloaded.api_key(inst.id) == "Oasis-Webid=w; Oasis-Token=t"


# ------------------------------------------------------------ 资源一致性

def test_absurd_finite_balance_renders_placeholder():
    """离谱有限值(1e300)不能变成几百位数字把布局撑爆:与 inf/NaN 同样显示 "--"。"""
    from quotabar import render
    from quotabar.models import LABEL_BALANCE

    metric = UsageMetric(LABEL_BALANCE, 1e300, None, "CNY")
    assert render.format_balance(1e300) == "--"
    assert render.tray_text(metric, "used") == "--"
    assert render.metric_detail_text(metric, "zh-Hans") == "--"
    # 正常余额不受影响
    assert render.format_balance(253.76) == "254"


def test_windows_i18n_key_parity():
    """windows/Resources 两份语言包键集必须一致(防止只改一边漏翻译)"""
    res = os.path.join(os.path.dirname(quotabar.__file__), "..", "Resources")
    with open(os.path.join(res, "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    with open(os.path.join(res, "zh-Hans.json"), encoding="utf-8") as f:
        zh = json.load(f)
    assert set(en) == set(zh)
    assert set(en) >= {"login.starting", "login.startFailed", "menu.startupBubble", "detail.disabled", "error.noTray"}


def test_i18n_keys_used_by_code_exist():
    """扫描包内所有字面量 t("...") 引用,确认每个 key 都有翻译(拼错 key 会静默显示原文)"""
    import re

    i18n = get_i18n()
    pkg_dir = os.path.dirname(quotabar.__file__)
    used: set[str] = set()
    for root, _dirs, files in os.walk(pkg_dir):
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as f:
                text = f.read()
            used.update(re.findall(r'\bt\(\s*"([^"]+)"', text))
    assert used, "没扫到任何 t(...) 调用,扫描逻辑失效"
    missing = sorted(key for key in used if i18n.t(key) == key)
    assert not missing, f"代码引用但资源缺失的 i18n key: {missing}"


# ------------------------------------------------------------ 对抗审查后的覆盖补强

def test_timer_triggers_background_fetch(qapp, tray, monkeypatch):
    """定时器到点必须真的发取数(变异:把 _on_timer 改成 pass,旧套件全绿)"""
    calls = []
    monkeypatch.setattr(tray, "fetch_all_async", lambda: calls.append(1))
    tray._on_timer()
    assert calls == [1]


def test_fetch_cookies_parses_cdp_response(monkeypatch):
    """CDP 提取链路:选平台页面 + 只认 id==1 的响应 + cookie→凭据映射。

    旧套件把整个 BrowserLoginSession 换掉,提取本身零覆盖 —— 整段打死也全绿。
    """
    import json as jsonlib

    import websocket as websocket_module

    import quotabar.ui.browser_login as bl

    class _FakeResp:
        def read(self):
            return jsonlib.dumps(
                [
                    {"type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://other"},
                    {"type": "page", "url": "https://platform.stepfun.com/x", "webSocketDebuggerUrl": "ws://stepfun"},
                ]
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(bl.urllib.request, "urlopen", lambda url, timeout=0: _FakeResp())

    sent = []

    class _FakeWS:
        def __init__(self):
            self._msgs = [
                jsonlib.dumps({"id": 2, "result": {"cookies": [{"name": "wrong", "value": "x"}]}}),
                jsonlib.dumps({
                    "id": 1,
                    "result": {"cookies": [
                        {"name": "Oasis-Webid", "value": "w"},
                        {"name": "Oasis-Token", "value": "t"},
                    ]},
                }),
            ]

        def send(self, data):
            sent.append(jsonlib.loads(data))

        def recv(self):
            return self._msgs.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(websocket_module, "create_connection", lambda url, timeout=0: _FakeWS())

    session = bl.BrowserLoginSession(PlatformType.STEPFUN)
    session._port = 12345
    cookies = session._fetch_cookies()
    assert sent and sent[0]["method"] == "Network.getAllCookies"
    assert {c["name"] for c in cookies} == {"Oasis-Webid", "Oasis-Token"}
    assert session.credential() == "Oasis-Webid=w; Oasis-Token=t"


def test_deep_nested_globals_are_dropped_not_fatal(tmp_path):
    """globals 里的深层嵌套垃圾(能过 json.loads)不能让启动崩在 deepcopy"""
    home = tmp_path / "home"
    home.mkdir()
    deep = "[" * 500 + "]" * 500
    payload = '{"instances": [], "globals": {"junk": ' + deep + '}}'
    (home / "settings.json").write_text(payload, encoding="utf-8")

    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert config.read_failed() is None
    # 垃圾键被白名单丢弃,后续保存正常
    config.set_display_mode("remaining")
    assert config.display_mode() == "remaining"


def test_infinite_overlay_pos_does_not_crash(tmp_path):
    """overlay_pos 里的 Infinity(手写 1e999/json 扩展)不能让 TrayApp 构造崩"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        '{"instances": [], "globals": {"overlay_pos": [Infinity, 0]}}', encoding="utf-8"
    )
    config = ConfigService(home=str(home))
    assert config.overlay_pos() is None  # 非法位置当作没记过


def test_orphan_key_prevents_id_reuse(tmp_path):
    """settings 丢失但 keys.json 还在:新账号 id 不能撞上孤儿凭据"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "keys.json").write_text('{"glm_cn-1": "sk-OLD"}', encoding="utf-8")
    config = ConfigService(home=str(home))

    inst = config.add_instance(PlatformType.GLM_CN)
    assert inst.id != "glm_cn-1"  # 撞上就会静默继承旧账号的凭据
    assert config.api_key(inst.id) == ""


def test_compare_and_set_api_key_is_atomic(tmp_path):
    """CAS:预期值不匹配时不写;匹配时写入并落盘"""
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.STEPFUN)
    config.set_api_key(inst.id, "old")

    assert config.compare_and_set_api_key(inst.id, "stale", "fresh") is False
    assert config.api_key(inst.id) == "old"

    assert config.compare_and_set_api_key(inst.id, "old", "fresh") is True
    assert config.api_key(inst.id) == "fresh"
    reloaded = ConfigService(home=str(tmp_path / "home"))
    assert reloaded.api_key(inst.id) == "fresh"


def test_status_color_thresholds_are_exact():
    """边界口径:<10% 红、<50% 黄,恰好 10%/50% 不误判"""
    from quotabar import render
    from quotabar.models import LABEL_FIVE_HOUR, PlatformType

    def color_for(percent):
        data = PlatformUsageData(
            platform=PlatformType.MINIMAX_CN,
            instance_id="x",
            display_name="x",
            metrics=[UsageMetric(LABEL_FIVE_HOUR, percent, 100, "%")],
            is_healthy=True,
        )
        return render.status_color(data)

    assert color_for(9.99) == render.COLOR_RED
    assert color_for(10) == render.COLOR_YELLOW  # 恰好 10%:不红
    assert color_for(49.99) == render.COLOR_YELLOW
    assert color_for(50) == render.COLOR_GREEN  # 恰好 50%:不黄


def test_format_balance_threshold_is_exact():
    """余额阈值:恰好 1000 走 K 缩写,999 不带单位"""
    from quotabar import render

    assert render.format_balance(1000) == "1.0K"
    assert render.format_balance(999) == "999"


# ------------------------------------------------------------ 核心层对抗审查后的回归

def _fetch_service(tmp_path, service, platform, *responses):
    from tests.conftest import FakeNetwork, json_response

    cfg = ConfigService(home=str(tmp_path / "home"))
    inst = cfg.add_instance(platform)
    cfg.set_api_key(inst.id, "test-credential")
    net = FakeNetwork(*[json_response(r) for r in responses])
    return service.fetch_usage(cfg.config_data(inst.id), net)


def test_glm_dedupes_duplicate_label_and_parses_string_unit(tmp_path):
    """GLM:同 label 去重(详情窗不能出现两张矛盾的周限额) + unit "6" 仍算周额度"""
    from quotabar.platforms.glm import GLMPlatformService

    body = {
        "success": True,
        "data": {
            "limits": [
                {"type": "TOKENS_LIMIT", "unit": "6", "percentage": 10},
                {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 90},
            ]
        },
    }
    data = _fetch_service(tmp_path, GLMPlatformService(), PlatformType.GLM_CN, body)

    labels = [m.label for m in data.metrics]
    assert labels == ["weekly_limit"]  # 去重且字符串 "6" 被正确识别
    assert data.metrics[0].current_value == 90  # 保留响应首条


def test_minimax_accepts_string_status_fields(tmp_path):
    """MiniMax:status_code/current_weekly_status 序列化成字符串时不能误判"""
    from quotabar.platforms.minimax import MiniMaxPlatformService

    body = {
        "base_resp": {"status_code": "0"},
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_remaining_percent": 80,
                "current_weekly_status": "1",
                "current_weekly_remaining_percent": 50,
                "end_time": 1_800_000_000_000,
            }
        ],
    }
    data = _fetch_service(tmp_path, MiniMaxPlatformService(), PlatformType.MINIMAX_CN, body)
    labels = [m.label for m in data.metrics]
    assert "weekly_limit" in labels  # 不是 unlimited(字符串 "1" 被识别成激活)
    assert data.is_healthy


def test_stepfun_accepts_string_status(tmp_path):
    """Stepfun:status "1" 不能误报业务错误"""
    from quotabar.platforms.stepfun import StepFunPlatformService

    cookie = "Oasis-Webid=w; Oasis-Token=a.b.c"
    cfg = ConfigService(home=str(tmp_path / "home"))
    inst = cfg.add_instance(PlatformType.STEPFUN)
    cfg.set_api_key(inst.id, cookie)

    from tests.conftest import FakeNetwork, json_response

    net = FakeNetwork(
        json_response({
            "status": "1",
            "plan_credit_rate_limit": {"subscription_credit_left_rate": 0.9,
                                       "subscription_credit_reset_time": 1_800_000_000},
        }),
        json_response({"status": "1", "subscription": {"status": "1", "expired_at": "1800000000"}}),
    )
    data = StepFunPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert data.metrics[0].label == "credits"
    assert data.metrics[0].current_value == 90


def test_tokenrhythm_naive_iso_is_utc(tmp_path):
    """无时区 ISO 串按 UTC 解释:不能按本地时区算差 8 小时"""
    from datetime import datetime, timezone

    from quotabar.platforms.tokenrhythm import TokenRhythmPlatformService

    cfg = ConfigService(home=str(tmp_path / "home"))
    inst = cfg.add_instance(PlatformType.TOKENRHYTHM)
    cfg.set_api_key(inst.id, "sess_abc")

    from tests.conftest import FakeNetwork, json_response

    net = FakeNetwork(
        json_response({"code": "0", "data": {"availableBalanceCny": "10.5"}}),
        json_response({"code": "0", "data": {"summary": {"nextExpiryAt": "2099-01-01T00:00:00"}}}),
    )
    data = TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    expected = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    assert data.metrics[0].reset_time == expected


def test_stepfun_refresh_giant_exp_is_ignored():
    """exp 是超大整数时解析层返回 None,不能把 OverflowError 包装成网络错误。"""
    from quotabar.platforms.stepfun_refresh import _expiry_from_payload

    assert _expiry_from_payload({"exp": 10 ** 400}) is None
    assert _expiry_from_payload({"exp": "1e999"}) is None


def test_metric_ratio_rejects_absurd_finite_values():
    """有限但离谱的比例(1e308/100)必须当作无数据:进度条 int() 会溢出"""
    from quotabar import render

    metric = UsageMetric(LABEL_FIVE_HOUR, 1e308, 100, "%")
    assert render.metric_ratio(metric) is None


def test_detail_window_survives_absurd_metric(qapp, tray):
    """上游给出 1e308 时详情窗仍能构造/渲染(旧代码 int(inf) 直接崩)"""
    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    tray.config.set_api_key(inst.id, "sk")
    tray._data[inst.id] = PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id=inst.id,
        display_name="x",
        metrics=[UsageMetric(LABEL_FIVE_HOUR, 1e308, 100, "%")],
    )
    detail = DetailWindow(tray)
    detail._selected_id = inst.id
    detail._build_ui()  # 不抛异常即通过


def test_status_color_follows_visible_metrics():
    """状态点按勾选的可见指标判色(与 macOS 对齐),不按未展示的首指标"""
    from quotabar import render
    from quotabar.models import LABEL_MCP_MONTHLY

    metrics = [
        UsageMetric(LABEL_FIVE_HOUR, 20, 100, "%"),
        UsageMetric(LABEL_MCP_MONTHLY, 900, 1000, "times"),
    ]
    data = PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id="x",
        display_name="x",
        metrics=metrics,
        is_healthy=True,
    )
    assert render.status_color(data) == render.COLOR_YELLOW  # 按首指标 20%
    assert render.status_color(data, [metrics[1]]) == render.COLOR_GREEN  # 只勾 MCP:90%


def test_reset_countdown_clamps_absurd_timestamp():
    """离谱时间戳的倒计时封顶,不能画出几百位数字把布局撑爆"""
    from quotabar import render

    kind, payload = render.reset_countdown(1e308)
    assert kind == "days"
    assert payload[0] <= 9999


def test_partial_template_is_merged_with_defaults(tmp_path, monkeypatch):
    """模板是部分字典(缺 api_base_url)时,用内置默认值补齐,账号不能永远停留在未配置。"""
    cfg = ConfigService(home=str(tmp_path / "home"))
    real_load = ConfigService._load_json

    def fake_load(path, protect=False):
        if path.endswith("glm.template.json"):
            return {"region": "international"}  # 故意缺 api_base_url
        return real_load(path, protect)

    monkeypatch.setattr(ConfigService, "_load_json", staticmethod(fake_load))
    inst = cfg.add_instance(PlatformType.GLM_CN)
    state = cfg._state(inst.id)
    assert state["api_base_url"] == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    assert state["region"] == "international"  # 模板值仍然覆盖默认


# ------------------------------------------------------------ UI 对抗审查后的回归

def test_config_cancel_is_blocked_during_login(qapp, tray):
    """登录等待期间点托盘图标/取消不能撤掉配置态:否则新建账号被回收、提取结果无处可写"""
    inst = tray.config.add_instance(PlatformType.STEPFUN)
    detail = DetailWindow(tray)
    detail._open_config(inst.id)
    detail._login_busy = True

    detail._cancel_config()

    assert detail.is_configuring()
    assert detail._config_instance_id == inst.id
    detail._login_busy = False


def test_account_switch_is_blocked_during_login(qapp, tray):
    """登录等待期间不能切账号/开别的配置:否则提取的凭据会落到别的账号上"""
    a = tray.config.add_instance(PlatformType.STEPFUN, "A")
    b = tray.config.add_instance(PlatformType.STEPFUN, "B")
    detail = DetailWindow(tray)
    detail._open_config(a.id)
    detail._login_busy = True

    detail._open_config(b.id)
    detail.show_account(b.id)
    detail._switch_account(b.id)

    assert detail._config_instance_id == a.id
    assert detail._selected_id == a.id
    detail._login_busy = False


def test_extracted_credential_goes_to_originating_account(qapp, tray):
    """提取结果写到**发起登录的账号**,不写"当前"账号(等待期间可能被切换)"""
    a = tray.config.add_instance(PlatformType.STEPFUN, "A")
    b = tray.config.add_instance(PlatformType.STEPFUN, "B")
    detail = DetailWindow(tray)
    detail._open_config(a.id)
    detail._api_key_text = "Oasis-Webid=w; Oasis-Token=t"
    detail._config_instance_id = b.id  # 模拟面板已被切到 B(守卫之外的第二道防线)

    detail._apply_extracted_credential(a.id)

    assert tray.config.api_key(a.id) == "Oasis-Webid=w; Oasis-Token=t"
    assert tray.config.api_key(b.id) == ""
    assert detail._api_key_text == ""  # 不污染当前账号的输入框


def test_extracted_credential_warns_when_account_gone(qapp, tray, monkeypatch):
    """落点账号已删:明确告知用户,不能静默丢弃一次登录"""
    import quotabar.ui.detail_window as dw

    calls = []
    fake_box = type(
        "_Box", (), {"warning": staticmethod(lambda *a, **k: calls.append(a)),
                     "critical": staticmethod(lambda *a, **k: None)}
    )
    monkeypatch.setattr(dw, "QMessageBox", fake_box)

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    detail = DetailWindow(tray)
    detail._open_config(inst.id)
    detail._api_key_text = "Oasis-Webid=w; Oasis-Token=t"
    tray.config.delete_instance(inst.id)

    detail._apply_extracted_credential(inst.id)

    assert calls, "账号已删除时必须提示,不能静默丢弃凭据"


def test_tooltip_hover_stops_hide_timer(qapp, tray):
    """鼠标能停在卡片上:进入卡片停收起计时,离开再计时(否则卡片永远只能远观)"""
    from PyQt5.QtCore import QEvent, QRect

    overlay = tray._overlay
    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    tray._data[inst.id] = PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id=inst.id,
        display_name="x",
        metrics=[UsageMetric(LABEL_FIVE_HOUR, 80, 100, "%")],
    )
    overlay._tip.show_accounts(overlay._tooltip_accounts(), QRect(0, 900, 100, 20))
    overlay._hide_timer.start(250)

    overlay._tip.enterEvent(QEvent(QEvent.Enter))
    assert not overlay._hide_timer.isActive()  # 停在卡片上不会被收起

    overlay._tip.leaveEvent(QEvent(QEvent.Leave))
    assert overlay._hide_timer.isActive()  # 离开卡片恢复收起计时
    overlay._tip.hide()


# ------------------------------------------------------------ 终审(对抗第 2 轮)回归

def test_login_busy_resets_when_session_construction_fails(qapp, tray, monkeypatch):
    """登录会话构造异常(临时目录不可用):busy 必须复位,否则登录永久失效、窗口关不掉"""
    import quotabar.ui.browser_login as bl

    class _Boom:
        def __init__(self, platform):
            raise OSError("temp dir unavailable")

    monkeypatch.setattr(bl, "find_browser", lambda: "C:/fake/chrome.exe")
    monkeypatch.setattr(bl, "BrowserLoginSession", _Boom)

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    detail = DetailWindow(tray)
    detail._open_config(inst.id)

    with pytest.raises(OSError):
        detail._open_login(inst.platform_type)
    assert detail._login_busy is False  # 构造阶段异常也要复位
    # 复位后能正常取消配置(不会被 busy 守卫挡住)
    detail._cancel_config()
    assert not detail.is_configuring()


def test_accept_during_start_phase_still_extracts(qapp, tray, monkeypatch):
    """启动等待期点「完成并提取」:继续走提取,不能静默丢弃这次登录"""
    import quotabar.ui.browser_login as bl
    import quotabar.ui.detail_window as dw

    class _Session:
        def __init__(self, platform):
            pass

        def start(self):
            return True

        def credential(self):
            return "Oasis-Webid=w; Oasis-Token=t"

        def close(self):
            pass

    class _EarlyAcceptDialog:
        def __init__(self, *args, **kwargs):
            pass

        def set_phase(self, text):
            pass

        def show(self):
            pass

        def isVisible(self):
            return False  # 用户在启动阶段就点了「完成并提取」,对话框已隐藏

        def result(self):
            return dw.QDialog.Accepted

        def setWindowModality(self, mode):
            pass

        def deleteLater(self):
            pass

        def exec_(self):
            raise AssertionError("已提前完成,不应再进模态等待")

    monkeypatch.setattr(bl, "find_browser", lambda: "C:/fake/chrome.exe")
    monkeypatch.setattr(bl, "BrowserLoginSession", _Session)
    monkeypatch.setattr(dw, "_WaitLoginDialog", _EarlyAcceptDialog)

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    detail = DetailWindow(tray)
    detail._open_config(inst.id)
    detail._open_login(inst.platform_type)

    assert tray.config.api_key(inst.id) == "Oasis-Webid=w; Oasis-Token=t"


def test_open_detail_for_blocked_during_login(qapp, tray, monkeypatch):
    """登录等待期间点别的账号:不切 active、不切详情窗(避免图标与登录账号错位)"""
    a = tray.config.add_instance(PlatformType.STEPFUN, "A")
    b = tray.config.add_instance(PlatformType.STEPFUN, "B")
    tray.config.set_active_instance_id(a.id)

    detail = DetailWindow(tray)
    tray._detail = detail
    detail._login_busy = True
    monkeypatch.setattr(detail, "show", lambda: None)
    monkeypatch.setattr(detail, "raise_", lambda: None)
    monkeypatch.setattr(detail, "activateWindow", lambda: None)

    tray.open_detail_for(b.id)

    assert tray.config.active_instance_id() == a.id  # active 不被改
    detail._login_busy = False


def test_add_account_blocked_during_login(qapp, tray):
    """登录等待期间「添加账号」:不加空壳账号(否则会变成回收不掉的幽灵)"""
    detail = DetailWindow(tray)
    tray._detail = detail
    detail._login_busy = True

    before = len(tray.config.instances())
    tray._add_account(PlatformType.GLM_CN)

    assert len(tray.config.instances()) == before
    detail._login_busy = False


def test_refresh_cooldown_blocks_repeat_and_clears_on_new_credential(tmp_path, monkeypatch):
    """续期失败冷却:失败后不再每个周期白打 RefreshToken;保存新凭据立即解除"""
    import quotabar.manager as manager_module

    cfg = ConfigService(home=str(tmp_path / "home"))
    inst = cfg.add_instance(PlatformType.STEPFUN)
    cfg.set_api_key(inst.id, "Oasis-Webid=w; Oasis-Token=a...b")

    calls = []

    def always_fail(credential, network, parsed=None):
        calls.append(1)
        return None

    monkeypatch.setattr(manager_module, "refresh_credential", always_fail)
    # 让解析结果判定为"需要刷新"
    monkeypatch.setattr(manager_module, "needs_refresh", lambda parsed: True)

    manager = manager_module.PlatformManager(cfg, network=lambda req: None)
    manager._refresh_stepfun_if_needed(inst.id)
    manager._refresh_stepfun_if_needed(inst.id)
    assert len(calls) == 1  # 第二次被冷却挡住

    manager.clear_refresh_cooldown(inst.id)
    manager._refresh_stepfun_if_needed(inst.id)
    assert len(calls) == 2  # 新凭据后恢复尝试


def test_fullscreen_detection_no_false_positive(qapp, tray):
    """没有全屏前台窗口时不能误判,否则面板会无故消失"""
    from quotabar.ui.winutil import foreground_covers_rect

    # 一个任何窗口都不可能覆盖的屏幕外矩形
    assert foreground_covers_rect(-100000, -100000, -99999, -99999) is False
    assert tray._overlay.sync_fullscreen() is False
    assert tray._overlay._hidden_for_fullscreen is False
