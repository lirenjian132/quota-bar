# -*- coding: utf-8 -*-
"""新版 UI 回归:无底色面板、悬停错误提示、详情窗单账号视图。"""

import os

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

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
from quotabar.ui.theme import status_color  # noqa: E402
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


def _usage(platform, iid, name, value):
    return PlatformUsageData(
        platform=platform,
        instance_id=iid,
        display_name=name,
        metrics=[UsageMetric(LABEL_FIVE_HOUR, value, 100, "%")],
    )


def test_panel_is_transparent_text_only(qapp, tray):
    """面板不再采样/铺底色:窗口逐像素透明,且没有主题重采样定时器"""
    from PyQt5.QtCore import Qt

    overlay = tray._overlay
    assert overlay.testAttribute(Qt.WA_TranslucentBackground) is True
    assert overlay.testAttribute(Qt.WA_NoSystemBackground) is True
    assert not hasattr(overlay, "_theme_timer")
    assert not hasattr(overlay, "_bg_color")


def test_tooltip_shows_error_reason_instead_of_no_data(qapp, tray):
    """Stepfun 登录过期:悬停卡片要写原因,不能只显示「无数据 --」"""
    inst = tray.config.add_instance(PlatformType.STEPFUN)
    tray.config.set_api_key(inst.id, "Oasis-Webid=w; Oasis-Token=a...b")
    tray._errors[inst.id] = PlatformError(ErrorKind.UNAUTHORIZED)

    accounts = tray._overlay._tooltip_accounts()
    account = next(a for a in accounts if a.name == inst.display_name)
    assert account.metrics == []
    assert account.error == get_i18n().t("error.unauthorized.session")
    assert account.color == status_color("red")


def test_detail_shows_only_selected_account(qapp, tray):
    """点哪个账号就只看哪个账号,不再把所有账号挤在一个窗口里"""
    one = tray.config.add_instance(PlatformType.MINIMAX_CN, "账号一")
    two = tray.config.add_instance(PlatformType.MINIMAX_CN, "账号二")
    for inst in (one, two):
        tray.config.set_api_key(inst.id, "sk-test")
    tray.config.set_display_mode("remaining")
    tray._data[one.id] = _usage(PlatformType.MINIMAX_CN, one.id, "账号一", 89)
    tray._data[two.id] = _usage(PlatformType.MINIMAX_CN, two.id, "账号二", 23)

    detail = DetailWindow(tray)
    detail._selected_id = one.id
    detail._build_ui()

    texts = [lbl.text() for lbl in detail.findChildren(QLabel)]
    assert "89%" in texts
    assert "23%" not in texts


def test_open_detail_for_switches_account(qapp, tray, monkeypatch):
    """点面板账号块:详情窗选中该账号,并把它设为激活账号"""
    one = tray.config.add_instance(PlatformType.MINIMAX_CN, "账号一")
    two = tray.config.add_instance(PlatformType.MINIMAX_CN, "账号二")
    # 两个取数入口都要挡掉:只挡 fetch_all 的话,open_detail_for 里没数据的
    # 账号会走 fetch_instance_async,拿假 key 打真实生产接口
    monkeypatch.setattr(tray, "fetch_all_async", lambda: None)
    monkeypatch.setattr(tray, "fetch_instance_async", lambda iid: None)

    tray.open_detail_for(two.id)

    assert tray._detail is not None
    assert tray._detail._selected_id == two.id
    assert tray.config.active_instance_id() == two.id
