# -*- coding: utf-8 -*-
"""QuotaBar Windows 版入口。

职责(对齐 macOS 版 App/main.swift + AppDelegate):
1. 修正 Qt 平台插件探测(项目路径含中文时 Qt 自带探测会失败)
2. 组装 QApplication / ConfigService / PlatformManager / TrayApp
3. 启动即拉一次数据,之后按配置的刷新间隔自动循环
"""
from __future__ import annotations

import os
import sys


def _fix_qt_plugin_path() -> None:
    """把 PyQt5 自带的平台插件目录写进 QT_QPA_PLATFORM_PLUGIN_PATH。

    项目路径含中文(用量/quota-bar)时 Qt 的平台插件自动探测会失败,报
    "This application failed to start because no Qt platform plugin could
    be initialized"。必须在 QApplication 创建前设置。
    """
    import PyQt5

    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(
        os.path.dirname(PyQt5.__file__), "Qt5", "plugins", "platforms"
    )


def _acquire_single_instance_lock(app) -> bool:
    """单实例锁:已有实例在跑就返回 False(双击两次会写坏同一个 settings.json)"""
    from PyQt5.QtNetwork import QLocalServer, QLocalSocket

    key = "QuotaBarWinSingleInstance"
    probe = QLocalSocket(app)
    probe.connectToServer(key)
    if probe.waitForConnected(150):
        return False  # 已有实例
    QLocalServer.removeServer(key)
    server = QLocalServer(app)
    server.listen(key)
    return True


def main() -> int:
    _fix_qt_plugin_path()

    from PyQt5.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    from quotabar.config import ConfigService
    from quotabar.i18n import get_i18n
    from quotabar.manager import PlatformManager
    from quotabar.ui.tray import TrayApp

    app = QApplication(sys.argv)
    app.setApplicationName("QuotaBar")
    # 详情窗关了不退程序:托盘常驻
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "QuotaBar", "此系统不支持系统托盘,无法运行。")
        return 1
    if not _acquire_single_instance_lock(app):
        return 0  # 已有实例在运行,静默退出

    config = ConfigService()
    manager = PlatformManager(config)
    # 按持久化 locale 初始化 i18n 单例
    get_i18n().set_locale(config.locale())

    tray = TrayApp(config, manager)
    tray.start()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
