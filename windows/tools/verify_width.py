# -*- coding: utf-8 -*-
"""手动验证:面板宽度随账号数自动伸缩(加号变长、删号变短)。

单测里不跑这个(在 pytest 内创建 Qt 窗口会挂住),需要时手动执行:
    QT_QPA_PLATFORM_PLUGIN_PATH=<plugins> .venv/Scripts/python.exe tools/verify_width.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main() -> int:
    home = tempfile.mkdtemp(prefix="quotabar-verify-width-")
    os.environ["QUOTABAR_HOME"] = home
    try:
        _run()
    finally:
        shutil.rmtree(home, ignore_errors=True)  # 临时目录用完即清,不越积越多
    return 0


def _run() -> None:
    from PyQt5.QtWidgets import QApplication

    app = QApplication([])
    from quotabar.config import ConfigService
    from quotabar.models import PlatformType
    from quotabar.ui.overlay import StatusOverlay

    class _Tray:
        def __init__(self, cfg):
            self.config = cfg

        def data(self, _id):
            return None

        def _visible_metrics(self, _id):
            return []

        def is_loading(self, _id):
            return False

    cfg = ConfigService()
    overlay = StatusOverlay(_Tray(cfg))
    widths = []
    for i in range(5):
        cfg.add_instance(PlatformType.MINIMAX_CN, f"A{i}")
        overlay.refresh()
        widths.append(overlay.width())
        print(f"账号数={i + 1}  宽度={widths[-1]}px")
    assert widths == sorted(widths) and widths[0] < widths[-1], widths
    cfg.delete_instance(cfg.instances()[0].id)
    overlay.refresh()
    print(f"删一个后 宽度={overlay.width()}px")
    assert overlay.width() < widths[-1]
    print("OK:宽度随账号数自动伸缩")
    app.quit()


if __name__ == "__main__":
    sys.exit(main())
