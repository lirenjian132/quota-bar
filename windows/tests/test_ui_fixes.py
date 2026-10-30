# -*- coding: utf-8 -*-
"""UI 层回归测试(离屏 Qt):面板开关、登录窗错误路径、健康度口径、空壳账号回收。

Qt 相关环境变量必须在 QApplication 创建前设好:
- QT_QPA_PLATFORM=offscreen:不弹真实窗口
- QT_QPA_PLATFORM_PLUGIN_PATH:项目路径含中文时 Qt 自带探测会失败(同 run.py 的处理)
"""

import os

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

import quotabar.ui.detail_window as detail_window_module  # noqa: E402
from quotabar.config import ConfigService  # noqa: E402
from quotabar.i18n import get_i18n  # noqa: E402
from quotabar.manager import PlatformManager  # noqa: E402
from quotabar.models import (  # noqa: E402
    LABEL_FIVE_HOUR,
    ErrorKind,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
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
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tray(tmp_path):
    config = ConfigService(home=str(tmp_path / "home"))
    manager = PlatformManager(config)
    return TrayApp(config, manager)


class _FakeMessageBox:
    """替掉 QMessageBox:模态弹窗会阻塞测试进程"""

    calls: list = []

    @classmethod
    def warning(cls, *args, **kwargs):
        cls.calls.append(args)


# ------------------------------------------------------------ 面板开关

def test_toggle_overlay_off_actually_hides_panel(qapp, tray):
    """关掉「任务栏数字面板」必须真的收起面板(只改配置的话面板一直挂在任务栏上)"""
    tray._overlay.show()
    assert tray._overlay.isVisible()

    tray._toggle_overlay(False)

    assert tray.config.overlay_enabled() is False
    assert not tray._overlay.isVisible()


def test_toggle_overlay_on_shows_panel_again(qapp, tray):
    tray._toggle_overlay(False)
    tray._toggle_overlay(True)
    assert tray.config.overlay_enabled() is True
    assert tray._overlay.isVisible()


def test_needs_data_covers_accounts_without_tray_icon(qapp, tray):
    """托盘图标默认关闭时,新增的已配置账号也要补拉一次(不能等下一个刷新周期)"""
    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    tray.config.set_api_key(inst.id, "sk-test")
    assert tray._icon_instance == {}  # 托盘图标关闭 → 图标集合为空
    assert tray._needs_data() is True

    tray._data[inst.id] = PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id=inst.id,
        display_name="x",
        metrics=[UsageMetric(LABEL_FIVE_HOUR, 80, 100, "%")],
    )
    assert tray._needs_data() is False


# ------------------------------------------------------------ 登录窗错误路径

def test_open_login_without_browser_does_not_raise(qapp, tray, monkeypatch):
    """没有可用浏览器:要弹提示,不能抛 NameError(原代码引用了未导入的 QMessageBox)"""
    import quotabar.ui.browser_login as browser_login_module

    monkeypatch.setattr(browser_login_module, "find_browser", lambda: None)
    monkeypatch.setattr(detail_window_module, "QMessageBox", _FakeMessageBox)
    _FakeMessageBox.calls = []

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    detail = DetailWindow(tray)
    detail._open_login(inst.platform_type)

    assert _FakeMessageBox.calls, "没有弹出任何提示"


def test_open_login_cancelled_does_not_harvest_cookie(qapp, tray, monkeypatch):
    """点「取消」不该去收割 cookie"""
    import quotabar.ui.browser_login as browser_login_module

    class _FakeSession:
        def __init__(self, platform):
            self.closed = False
            self.credential_called = False

        def start(self):
            return True

        def credential(self):
            self.credential_called = True
            return "Oasis-Webid=w; Oasis-Token=t"

        def close(self):
            self.closed = True

    session = _FakeSession(None)
    monkeypatch.setattr(browser_login_module, "find_browser", lambda: "C:/fake/chrome.exe")
    monkeypatch.setattr(browser_login_module, "BrowserLoginSession", lambda p: session)
    monkeypatch.setattr(detail_window_module, "QMessageBox", _FakeMessageBox)

    class _RejectedDialog:
        """替掉等待对话框:真实 exec_() 是模态的,会一直等用户点击"""

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

            return QDialog.Rejected

    monkeypatch.setattr(detail_window_module, "_WaitLoginDialog", _RejectedDialog)

    inst = tray.config.add_instance(PlatformType.STEPFUN)
    detail = DetailWindow(tray)
    detail._open_login(inst.platform_type)  # 对话框直接走 reject 路径

    assert session.credential_called is False
    assert session.closed is True  # 取消也要收尾:浏览器进程与临时目录不能泄漏
    assert detail._api_key_text == ""


# ------------------------------------------------------------ 详情窗健康度口径

def test_health_row_ignores_transient_refresh_error(qapp, tray):
    """有旧数据 + 最近一次刷新失败:健康行仍按数据判定,陈旧提示单独一行"""
    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    tray.config.set_api_key(inst.id, "sk-test")
    tray._data[inst.id] = PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id=inst.id,
        display_name="主力",
        metrics=[UsageMetric(LABEL_FIVE_HOUR, 80, 100, "%")],
        is_healthy=True,
    )
    tray._errors[inst.id] = PlatformError(ErrorKind.NETWORK, "boom")

    detail = DetailWindow(tray)
    detail._build_ui()

    texts = [lbl.text() for lbl in detail.findChildren(QLabel)]
    i18n = get_i18n()
    assert i18n.t("popover.normal") in texts
    assert i18n.t("popover.staleData") in texts
    assert i18n.t("popover.low") not in texts


# ------------------------------------------------------------ 保存按钮与空壳账号

def test_save_button_follows_input(qapp, tray):
    """清空输入框后保存按钮要禁用(否则点了没反应又没提示)"""
    inst = tray.config.add_instance(PlatformType.MINIMAX_CN)
    detail = DetailWindow(tray)
    detail._open_config(inst.id)

    detail._on_api_key_changed("sk-abc")
    assert detail._save_button.isEnabled()
    detail._on_api_key_changed("")
    assert not detail._save_button.isEnabled()


def test_new_account_cancelled_is_reclaimed(qapp, tray):
    """新建账号在配置前取消:回收空壳账号(对齐 macOS,菜单不留幽灵账号)"""
    assert tray.config.instances() == []
    tray._add_account(PlatformType.GLM_CN)
    assert len(tray.config.instances()) == 1

    tray._detail._cancel_config()

    assert tray.config.instances() == []


def test_new_account_closed_with_escape_is_reclaimed(qapp, tray):
    """按 ESC / 点 X 关窗也算取消:空壳账号照样要回收"""
    tray._add_account(PlatformType.GLM_CN)
    assert len(tray.config.instances()) == 1

    tray._detail.reject()  # ESC 的标准路径

    assert tray.config.instances() == []


def test_single_instance_lock_allows_first_instance(qapp):
    """单实例锁:第一个实例必须拿得到锁(曾经的反探测写法会让应用永远起不来)

    用独立的管道名,免得撞上本机正在跑的已安装版 QuotaBar。
    """
    import run as runmod

    key = "QuotaBarWinSingleInstance-test-%d" % os.getpid()
    assert runmod._acquire_single_instance_lock(qapp, key) is True
    # 第二个进程视角:锁已被占用 → 拿不到
    assert runmod._acquire_single_instance_lock(qapp, key) is False


def test_overlay_stays_hidden_when_disabled_and_embedded(qapp, tray):
    """用户关掉面板后,切嵌入/浮层不能把面板带回来"""
    tray._toggle_overlay(False)
    assert not tray._overlay.isVisible()

    tray._overlay.attach()      # 嵌入:窗口形态变了,但不该显示
    tray._overlay.detach()      # 脱离:也不该显示
    tray._overlay._ensure_visible()

    assert not tray._overlay.isVisible()


def test_tooltip_second_metric_uses_same_mode_as_first(qapp, tray):
    """tooltip 一行里两个指标不能一个已用一个剩余"""
    inst = tray.config.add_instance(PlatformType.GLM_CN)
    tray.config.set_api_key(inst.id, "sk-test")
    tray.config.set_enabled_metrics(inst.id, ["five_hour", "weekly_limit"])
    tray._data[inst.id] = PlatformUsageData(
        platform=PlatformType.GLM_CN,
        instance_id=inst.id,
        display_name="主力",
        metrics=[
            UsageMetric(LABEL_FIVE_HOUR, 80, 100, "%"),
            UsageMetric("weekly_limit", 59, 100, "%"),
        ],
    )
    tray.config.set_display_mode("used")

    tip = tray._make_tooltip()
    assert "(周限额 41%)" in tip  # 剩余 59% → 已用 41%,与主指标同口径
    assert "59%" not in tip


def test_rule_rejection_is_not_reported_as_save_failure(qapp, tray, monkeypatch):
    """业务规则拒绝(禁用最后一个账号)要给规则提示,不能混成「保存失败」"""
    import quotabar.ui.tray as tray_module

    calls = []

    class _Recorder:
        @staticmethod
        def warning(*args, **kwargs):
            calls.append(args)

    monkeypatch.setattr(tray_module, "QMessageBox", _Recorder)
    inst = tray.config.add_instance(PlatformType.GLM_CN)

    class _Act:
        def setChecked(self, value):
            pass

    tray._toggle_enabled(inst.id, _Act())

    assert calls, "没有给出任何提示"
    assert "保存失败" not in calls[0][2]
    assert "至少" in calls[0][2]


def test_hiding_detail_window_in_config_mode_reclaims_account(qapp, tray):
    """配置面板被程序化隐藏(点面板 chip):等同取消,空壳账号要回收"""
    tray._add_account(PlatformType.GLM_CN)
    assert len(tray.config.instances()) == 1
    assert tray._detail.is_configuring()

    tray._toggle_detail()  # 可见 → 隐藏

    assert tray.config.instances() == []
    assert not tray._detail.is_configuring()


def test_start_heals_missing_entry_point(qapp, tmp_path):
    """面板与托盘图标都关着(历史遗留/手改):启动要自愈,否则用户没有入口"""
    config = ConfigService(home=str(tmp_path / "home"))
    config.set_entry_visibility(overlay=False, tray=False)
    manager = PlatformManager(config)
    app = TrayApp(config, manager)

    app.start()

    assert config.overlay_enabled() is True
    assert app._overlay.isVisible()


def test_detach_does_not_show_a_disabled_panel(qapp, tray, monkeypatch):
    """脱离任务栏时不能把用户关掉的面板带回来(detach 原来无条件 show)"""
    import quotabar.ui.overlay as overlay_module

    calls = []
    monkeypatch.setattr(overlay_module, "detach_from_taskbar", lambda hwnd: calls.append(hwnd))
    tray.config.set_overlay_embedded(True)
    tray.config.set_overlay_enabled(False)

    ov = tray._overlay
    ov._embed_host = 12345  # 假句柄:让 detach 真的走主体逻辑
    ov.detach()

    assert calls, "detach 没有真正执行"
    assert not ov.isVisible()


def test_metric_menu_greys_out_third_option(qapp, tray):
    """勾满 2 个后,第 3 个指标要置灰(与 macOS 一致)"""
    from PyQt5.QtWidgets import QMenu

    inst = tray.config.add_instance(PlatformType.GLM_CN)
    tray.config.set_enabled_metrics(inst.id, ["five_hour", "weekly_limit"])

    menu = QMenu()
    tray._fill_metrics_menu(menu)

    # 指标菜单是「每个账号一个子菜单」,动作在子菜单里
    states = {}
    for sub_act in menu.actions():
        inner = sub_act.menu()
        if inner is None:
            continue
        for act in inner.actions():
            label = act.data()
            if label:
                states[label] = (act.isChecked(), act.isEnabled())
    assert states["mcp_monthly"][1] is False  # 第 3 个置灰
    assert states["five_hour"][1] is True


def test_tooltip_truncates_long_account_list(qapp, tray):
    """账号很多时 tooltip 要自己截断并说明总数(系统会静默截到 127 字符)"""
    for i in range(8):
        inst = tray.config.add_instance(PlatformType.MINIMAX_CN, f"很长很长的账号名字{i}" * 3)
        tray.config.set_api_key(inst.id, "sk-test")

    tip = tray._make_tooltip()
    assert len(tip) <= 130
    assert "个账号" in tip  # 补一行说明还有多少
