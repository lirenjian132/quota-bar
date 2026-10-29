# -*- coding: utf-8 -*-
"""详情弹窗:账号切换 + 指标卡 + API Key 配置面板。

功能语义对齐 macOS 版 Views/PopoverContentView,视觉与交互按 Windows 习惯:
- 普通 QDialog(系统标题栏),弹出时落在屏幕右下角、贴近任务栏上沿
- Qt 原生控件 + 默认外观,不自绘高亮/pill 效果
- 文案一律走 i18n,数值/倒计时一律调 render.* 纯函数,UI 不自行计算
- 取数全部经 TrayApp 的后台线程完成,本窗只读结果,不在 GUI 线程发请求
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PyQt5.QtCore import QMimeData, Qt, pyqtSignal
from PyQt5.QtGui import QCursor, QGuiApplication, QDrag
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import render
from ..i18n import get_i18n
from ..models import (
    LABEL_BALANCE,
    LABEL_WEEKLY_UNLIMITED,
    ErrorKind,
    PlatformError,
    PlatformInstance,
    PlatformType,
    UsageMetric,
)

if TYPE_CHECKING:  # 仅类型标注用,避免与 tray 模块形成导入环
    from .tray import TrayApp

#: 弹窗固定宽度
PANEL_WIDTH = 400

#: 健康行配色(与托盘状态点同一套色板)
_COLOR_GREEN = "#34C759"
_COLOR_RED = "#FF3B30"
_COLOR_ORANGE = "#FF9500"
_COLOR_GRAY = "#8E8E93"

#: 弹出定位留白:底边贴任务栏上沿(留 8),右侧离屏幕角留空一点(24)
_MARGIN_BOTTOM = 8
_MARGIN_RIGHT = 24


def _clear_layout(layout) -> None:
    """递归清空布局内的子控件(整体重建 UI 用)"""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)
            sub.deleteLater()


class DetailWindow(QDialog):
    """QuotaBar 详情窗:账号切换、指标卡、凭据配置。"""

    def __init__(self, tray: "TrayApp", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._tray = tray
        self._config = tray.config
        self._i18n = get_i18n()
        # 视图状态:"main" 主视图 / "config" 配置面板
        self._mode = "main"
        self._config_instance_id: Optional[str] = None
        # 配置面板输入(整体重建时保留,语言切换不丢用户已输内容)
        self._api_key_text = ""
        self.setWindowTitle(self._i18n.t("app.name"))
        self.setFixedWidth(PANEL_WIDTH)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(12, 12, 12, 12)
        self._root.setSpacing(8)
        self._build_ui()

    # ------------------------------------------------------------ 公开接口

    def reload(self) -> None:
        """数据/配置/语言变化后整体重建(保留配置面板的已输内容)。

        配置面板打开期间跳过重建:60 秒定时刷新会把输入框销毁重建,
        粘 API key 时焦点丢失、明文开关被重置——配置完保存/取消自然会回主视图。
        """
        if self._mode == "config":
            return
        self._build_ui()

    # ------------------------------------------------------------ 定位(Windows:贴屏幕右下角)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._move_to_bottom_right()

    def _move_to_bottom_right(self) -> None:
        """把窗口放到当前屏幕右下角、贴任务栏上沿。

        Windows 拿不到托盘图标的几何位置,退而求其次:光标所在屏幕的右下角。
        下界不用 availableGeometry(部分系统不会把任务栏让干净,窗口底边会被压住),
        直接取任务栏(Shell_TrayWnd)的真实上沿;拿不到再退回 availableGeometry。
        """
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        bottom_limit = self._taskbar_top_edge(screen)
        if bottom_limit is None:
            bottom_limit = geo.bottom()
        # 注意用 frameGeometry(整窗框,含标题栏+边框):move() 定位的是窗框左上角,
        # 用客户区 height() 会恒差一个标题栏高度,导致底边压进任务栏
        frame = self.frameGeometry()
        x = geo.right() - frame.width() - _MARGIN_RIGHT
        y = bottom_limit - frame.height() - _MARGIN_BOTTOM
        self.move(max(geo.left(), x), max(geo.top(), y))

    @staticmethod
    def _taskbar_top_edge(screen) -> Optional[int]:
        """底部任务栏的上沿坐标(委托 winutil 共用实现)"""
        from .winutil import taskbar_top_edge

        return taskbar_top_edge(screen.geometry())

    # ------------------------------------------------------------ 整体构建

    def _build_ui(self) -> None:
        """按当前视图模式重建整窗内容(配置面板输入已存成员变量,不丢)"""
        _clear_layout(self._root)
        if self._mode == "config":
            self._build_config_view()
        else:
            self._build_main_view()
        self.adjustSize()

    def _build_main_view(self) -> None:
        self._root.addWidget(self._build_header())
        self._root.addWidget(self._build_account_switcher())
        self._root.addWidget(self._build_content())
        self._root.addWidget(self._build_footer())

    # ------------------------------------------------------------ 头部

    def _build_header(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        title = QLabel(self._i18n.t("app.name"))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        row.addWidget(title)
        row.addStretch(1)
        active_id = self._config.active_instance_id()
        if active_id and self._tray.is_loading(active_id):
            loading = QLabel("…")
            loading.setStyleSheet(f"color: {_COLOR_GRAY};")
            row.addWidget(loading)
        box = QWidget()
        box.setLayout(row)
        return box

    # ------------------------------------------------------------ 账号切换区

    def _build_account_switcher(self) -> QWidget:
        """账号切换区:标签可**拖拽排序**(按住标签拖到另一个标签上松手即交换顺序)。"""
        self._reorder_area = _ReorderArea(self)
        self._reorder_area.reorder_requested.connect(self._reorder)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        active_id = self._config.active_instance_id()
        for inst in self._config.instances():
            btn = _ReorderButton(inst.id, inst.display_name)
            btn.setCheckable(True)
            btn.setChecked(inst.id == active_id)
            btn.setToolTip(self._i18n.t("detail.dragToReorder"))
            btn.clicked.connect(lambda checked=False, iid=inst.id: self._switch_account(iid))
            self._reorder_area.layout().addWidget(btn)
        self._reorder_area.layout().addStretch(1)  # 账号多时允许横向挤压
        return self._reorder_area

    def _reorder(self, source_id: str, target_id: str) -> None:
        """把 source 账号移动到 target 的位置(拖拽排序)"""
        ids = [i.id for i in self._config.instances()]
        if source_id not in ids or target_id not in ids or source_id == target_id:
            return
        ids.remove(source_id)
        ids.insert(ids.index(target_id), source_id)
        self._config.reorder_instances(ids)
        self._build_ui()

    def _switch_account(self, instance_id: str) -> None:
        self._config.set_active_instance_id(instance_id)
        # 新激活账号还没有数据时立刻后台拉一次,避免长时间显示空态
        if self._config.is_configured(instance_id) and self._tray.data(instance_id) is None:
            self._tray.fetch_instance_async(instance_id)
        self._build_ui()

    # ------------------------------------------------------------ 主体内容

    def _build_content(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        active_id = self._config.active_instance_id()
        inst = self._config.instance(active_id) if active_id else None
        if inst is None:
            self._fill_no_accounts(layout)
            return box
        if not self._config.is_configured(inst.id):
            self._fill_unconfigured(layout, inst)
            return box

        data = self._tray.data(inst.id)
        error = self._tray.error(inst.id)
        if data is not None and data.metrics:
            self._fill_metrics(layout, data, error)
        elif error is not None:
            self._fill_error(layout, error, inst)
        elif self._tray.is_loading(inst.id):
            self._fill_loading(layout)
        else:
            self._fill_no_data(layout)
        return box

    def _fill_no_accounts(self, layout: QVBoxLayout) -> None:
        """一个账号都没有:提示去托盘菜单添加"""
        label = QLabel(self._i18n.t("popover.selectPlatforms"))
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)

    def _fill_unconfigured(self, layout: QVBoxLayout, inst: PlatformInstance) -> None:
        title = QLabel(self._i18n.t("popover.platformNotConfigured", inst.display_name))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        layout.addWidget(title)
        btn = QPushButton(self._i18n.t("popover.configureNow"))
        btn.clicked.connect(lambda checked=False, iid=inst.id: self._open_config(iid))
        layout.addWidget(btn)

    def _fill_metrics(self, layout: QVBoxLayout, data, error: Optional[PlatformError]) -> None:
        """指标卡:全量展示,不过滤(对齐 macOS 弹窗口径)"""
        for metric in data.metrics:
            layout.addWidget(self._build_metric_card(metric))
        # 健康行:取数报错也按异常显示
        healthy = data.is_healthy and error is None
        text = self._i18n.t("popover.normal") if healthy else self._i18n.t("popover.low")
        health = QLabel(text)
        health.setStyleSheet(f"color: {_COLOR_GREEN if healthy else _COLOR_RED}; font-weight: bold;")
        layout.addWidget(health)
        if error is not None:
            # 有旧数据但最近一次刷新失败:提示当前数值可能不是最新
            stale = QLabel(self._i18n.t("popover.staleData"))
            stale.setStyleSheet(f"color: {_COLOR_ORANGE};")
            stale.setWordWrap(True)
            layout.addWidget(stale)

    def _build_metric_card(self, metric: UsageMetric) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(2)

        key = f"metric.{metric.label}"
        title = self._i18n.t(key)
        if title == key:  # 未命中 i18n key 时回退原始 label
            title = metric.label
        head = QLabel(title)
        head.setStyleSheet(f"color: {_COLOR_GRAY};")
        v.addWidget(head)

        value = QLabel(self._metric_value_text(metric))
        font = value.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        value.setFont(font)
        v.addWidget(value)

        if metric.reset_time is not None:
            countdown = QLabel(self._reset_countdown_text(metric))
            countdown.setStyleSheet(f"color: {_COLOR_GRAY};")
            v.addWidget(countdown)
        return card

    def _metric_value_text(self, metric: UsageMetric) -> str:
        """指标数值:一律调 render.*;无限套餐与状态栏同口径显示 ∞"""
        if metric.unit == "unlimited" or metric.label == LABEL_WEEKLY_UNLIMITED:
            return render.tray_text(metric, self._config.display_mode())
        return render.metric_detail_text(metric, self._i18n.locale)

    def _reset_countdown_text(self, metric: UsageMetric) -> str:
        """重置/到期倒计时:kind 由 render.reset_countdown 判定,文案走 i18n"""
        kind, payload = render.reset_countdown(metric.reset_time)
        if kind == "days":
            # 跨天:weekly.reset.remaining 自带"%d天%d小时后重置"完整语义,不再加前缀
            days, hours = payload
            return self._i18n.t("weekly.reset.remaining", days, hours)
        prefix = self._i18n.t("popover.expiry" if metric.label == LABEL_BALANCE else "popover.reset")
        if kind == "soon":
            body = self._i18n.t("daily.reset.soon")
        else:  # hm
            hours, minutes = payload
            if hours > 0:
                body = self._i18n.t("daily.reset.remaining", hours, minutes)
            else:
                body = self._i18n.t("daily.reset.minutesonly", minutes)
        return f"{prefix}: {body}"

    def _fill_error(self, layout: QVBoxLayout, error: PlatformError, inst: PlatformInstance) -> None:
        head = QLabel(self._i18n.t("popover.error"))
        head.setStyleSheet(f"color: {_COLOR_RED}; font-weight: bold;")
        layout.addWidget(head)
        body = QLabel(self._error_text(error, inst.platform_type))
        body.setWordWrap(True)
        layout.addWidget(body)

    def _error_text(self, error: PlatformError, platform: PlatformType) -> str:
        """错误文案:未授权按凭据类型分派;其余先试 i18n key,再按 kind 取通用文案"""
        i18n = self._i18n
        if error.kind == ErrorKind.UNAUTHORIZED:
            if platform.cookie_based():
                return i18n.t("error.unauthorized.session")
            return i18n.t("error.unauthorized.key")
        translated = i18n.t(error.message)
        if translated != error.message:
            return translated  # message 本身就是 i18n key(如 error.glm.emptyLimits)
        kind_keys = {
            ErrorKind.NOT_CONFIGURED: ("error.notConfigured", ()),
            ErrorKind.INVALID_RESPONSE: ("error.invalidResponse", ()),
            ErrorKind.NETWORK: ("error.networkError", (error.message,)),
            ErrorKind.DECODING: ("error.decodingError", (error.message,)),
            ErrorKind.API: ("error.apiError", (error.message,)),
        }
        entry = kind_keys.get(error.kind)
        if entry is not None:
            key, args = entry
            return i18n.t(key, *args)
        return error.message

    def _fill_loading(self, layout: QVBoxLayout) -> None:
        loading = QLabel("…")
        loading.setAlignment(Qt.AlignCenter)
        layout.addWidget(loading)

    def _fill_no_data(self, layout: QVBoxLayout) -> None:
        head = QLabel(self._i18n.t("popover.noData"))
        head.setStyleSheet(f"color: {_COLOR_GRAY}; font-weight: bold;")
        layout.addWidget(head)
        body = QLabel(self._i18n.t("error.invalidResponse"))
        body.setStyleSheet(f"color: {_COLOR_GRAY};")
        body.setWordWrap(True)
        layout.addWidget(body)

    # ------------------------------------------------------------ 底部按钮

    def _build_footer(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        refresh = QPushButton(self._i18n.t("menu.refreshNow"))
        refresh.clicked.connect(self._tray.fetch_all_async)
        row.addWidget(refresh)
        active_id = self._config.active_instance_id()
        configure = QPushButton(self._i18n.t("menu.configurePlatform"))
        configure.setEnabled(bool(active_id))
        configure.clicked.connect(lambda checked=False, iid=active_id: self._open_config(iid))
        row.addWidget(configure)
        row.addStretch(1)
        box = QWidget()
        box.setLayout(row)
        return box

    # ------------------------------------------------------------ 配置面板

    def _build_config_view(self) -> None:
        inst = self._config.instance(self._config_instance_id) if self._config_instance_id else None
        if inst is None:  # 配置对象没了(被删):退回主视图
            self._mode = "main"
            self._build_main_view()
            return
        i18n = self._i18n
        title_key, placeholder_key = _config_i18n_keys(inst.platform_type)

        title = QLabel(i18n.t(title_key, inst.display_name))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        self._root.addWidget(title)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        editor = QLineEdit()
        editor.setEchoMode(QLineEdit.Password)
        editor.setPlaceholderText(i18n.t(placeholder_key))
        editor.setText(self._api_key_text)
        editor.textChanged.connect(self._on_api_key_changed)
        row.addWidget(editor)
        toggle = QCheckBox("👁")
        toggle.setChecked(False)
        toggle.setToolTip(i18n.t("popover.configurePlatform", inst.display_name))
        toggle.toggled.connect(
            lambda checked: editor.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        row.addWidget(toggle)
        self._root.addLayout(row)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        # cookie 型平台:给「浏览器登录取凭据」入口(免去手抄 cookie)
        if inst.platform_type.cookie_based():
            browser_btn = QPushButton(i18n.t("login.browser"))
            browser_btn.setToolTip(i18n.t("login.hint"))
            browser_btn.clicked.connect(
                lambda checked=False, p=inst.platform_type: self._open_login(p)
            )
            buttons.addWidget(browser_btn)
        cancel = QPushButton(i18n.t("common.cancel"))
        cancel.clicked.connect(self._cancel_config)
        buttons.addWidget(cancel)
        save = QPushButton(i18n.t("common.save"))
        save.setEnabled(bool(self._api_key_text.strip()))
        save.clicked.connect(self._save_config)
        buttons.addWidget(save)
        buttons.addStretch(1)
        self._root.addLayout(buttons)

    def _open_login(self, platform: PlatformType) -> None:
        """打开系统浏览器登录,登录完成后把凭据填进配置框。

        优先用系统浏览器(Edge/Chrome):内置 WebEngine 的 Chromium 太旧,
        现代站点渲染不出来。系统浏览器起不来时退回内嵌方案。
        """
        from .browser_login import BrowserLoginSession, find_browser

        if find_browser() is None:
            self._login_with_embedded(platform)
            return

        session = BrowserLoginSession(platform)
        if not session.start():
            session.close()
            self._login_with_embedded(platform)
            return

        dlg = _WaitLoginDialog(self)
        try:
            dlg.exec_()   # 用户在浏览器里登录完,回这里点「完成并提取」
            credential = session.credential()
        finally:
            session.close()
        if credential:
            self._api_key_text = credential
            self._build_ui()

    def _login_with_embedded(self, platform: PlatformType) -> None:
        """兜底:内嵌浏览器登录窗"""
        from .login_window import LoginWindow

        win = LoginWindow(platform, self)
        if win.exec_() == QDialog.Accepted:
            credential = win.credential()
            if credential:
                self._api_key_text = credential
                self._build_ui()

    def _open_config(self, instance_id: Optional[str]) -> None:
        if not instance_id:
            return
        self._config_instance_id = instance_id
        self._api_key_text = self._config.api_key(instance_id)
        self._mode = "config"
        self._build_ui()

    def _on_api_key_changed(self, text: str) -> None:
        self._api_key_text = text

    def _cancel_config(self) -> None:
        self._mode = "main"
        self._config_instance_id = None
        self._api_key_text = ""
        self._build_ui()

    def _save_config(self) -> None:
        instance_id = self._config_instance_id
        key = self._api_key_text.strip()
        if not instance_id or not key:
            return
        self._config.set_api_key(instance_id, key)
        # 换 key 后立刻清该实例缓存并后台拉取(不清则旧账号数据还会残留一个缓存窗口)
        self._tray.refresh_instance(instance_id)
        self._mode = "main"
        self._config_instance_id = None
        self._api_key_text = ""
        self._build_ui()


class _ReorderButton(QPushButton):
    """可拖拽的账号标签:按住拖动超过阈值即进入拖拽(短按仍是切换账号)。"""

    def __init__(self, instance_id: str, text: str, parent=None):
        super().__init__(text, parent)
        self.instance_id = instance_id
        self._press_pos = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None and (event.buttons() & Qt.LeftButton):
            if (event.globalPos() - self._press_pos).manhattanLength() > 8:
                mime = QMimeData()
                mime.setText(self.instance_id)
                drag = self.createDrag() if hasattr(self, "createDrag") else QDrag(self)
                drag.setMimeData(mime)
                drag.exec_(Qt.MoveAction)
                self._press_pos = None
                return
        super().mouseMoveEvent(event)


class _ReorderArea(QWidget):
    """账号标签容器:接收拖放,放下时发 reorder_requested(源 id, 目标 id)。"""

    reorder_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.setLayout(row)

    def dragEnterEvent(self, event) -> None:
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        source_id = event.mimeData().text()
        target = self.childAt(event.pos())
        target_id = getattr(target, "instance_id", None)
        if source_id and target_id and source_id != target_id:
            self.reorder_requested.emit(source_id, target_id)
        event.acceptProposedAction()


class _WaitLoginDialog(QDialog):
    """提示用户:浏览器已打开,登录完点「完成并提取」"""

    def __init__(self, parent=None):
        super().__init__(parent)
        i18n = get_i18n()
        self.setWindowTitle(i18n.t("login.title", ""))
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        label = QLabel(i18n.t("login.browserHint"))
        label.setWordWrap(True)
        label.setStyleSheet("font-size:13px;")
        layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(i18n.t("common.cancel"))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        done = QPushButton(i18n.t("login.complete"))
        done.setDefault(True)
        done.clicked.connect(self.accept)
        row.addWidget(done)
        layout.addLayout(row)
        self.resize(420, 150)


def _config_i18n_keys(platform: PlatformType) -> tuple[str, str]:
    """配置面板标题/占位文案的 key:TokenRhythm/Stepfun 用变体,其余用通用 API Key 文案"""
    if platform == PlatformType.TOKENRHYTHM:
        return "popover.configurePlatform.tokenrhythm", "popover.inputPlaceholder.tokenrhythm"
    if platform == PlatformType.STEPFUN:
        return "popover.configurePlatform.stepfun", "popover.inputPlaceholder.stepfun"
    return "popover.configurePlatform", "popover.inputPlaceholder"
