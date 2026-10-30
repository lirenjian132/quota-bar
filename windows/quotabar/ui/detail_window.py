# -*- coding: utf-8 -*-
"""详情弹窗:一次只看一个账号(点哪个看哪个) + 指标卡 + API Key 配置。

视觉按 Windows 习惯重做:
- 深/浅色卡片跟随 Windows 应用模式(不跟壁纸)
- 账号切换条:彩色状态点 + 名称,点一下切换;标签可拖拽排序
- 指标卡:标题 + 大号数值 + 重置倒计时 + 进度条
- 错误态:直接给「重新登录」/「立即配置」按钮
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from PyQt5.QtCore import QMimeData, QSize, Qt, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QCursor,
    QDrag,
    QGuiApplication,
    QIcon,
    QPainter,
    QPalette,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
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
from .theme import palette, status_color

if TYPE_CHECKING:  # 仅类型标注用,避免与 tray 模块形成导入环
    from .tray import TrayApp

#: 弹窗固定宽度
PANEL_WIDTH = 420

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


def _dot_icon(color: str, size: int = 10) -> QIcon:
    """账号切换条左侧的小状态点"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(1, 1, size - 2, size - 2)
    finally:
        painter.end()
    return QIcon(pixmap)


class DetailWindow(QDialog):
    """QuotaBar 详情窗:单账号视图、账号切换、指标卡、凭据配置。"""

    def __init__(self, tray: "TrayApp", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._tray = tray
        self._config = tray.config
        self._i18n = get_i18n()
        self._pal = palette()
        #: 当前查看的账号;点面板 chip 时由 open_detail_for 指定
        self._selected_id: Optional[str] = None
        #: 上次构建时的内容指纹:定时刷新时内容没变就不整窗重建(防闪)
        self._view_sig = None
        # 视图状态:"main" 主视图 / "config" 配置面板
        self._mode = "main"
        self._config_instance_id: Optional[str] = None
        # 配置面板输入(整体重建时保留,语言切换不丢用户已输内容)
        self._api_key_text = ""
        # 配置面板的「保存」按钮:输入框内容变化时同步启用/禁用
        self._save_button: Optional[QPushButton] = None
        self.setWindowTitle(self._i18n.t("app.name"))
        # Qt.Tool:不出现在任务栏/Alt+Tab,但仍保留标题栏与关闭按钮。
        # 普通 QDialog 会给任务栏多出一个按钮,用户反馈「怎么还出现个图标」。
        self.setWindowFlags(
            Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTitleHint
            | Qt.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        # 顶层 QDialog 的 stylesheet 背景需要这个属性才会真正绘制;
        # 同时显式设一套深/浅 palette,避免系统亮色 palette 在 stylesheet
        # 未覆盖到的地方透出来(标签、输入框、默认背景)。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(True)
        self.setPalette(self._dialog_palette())
        self.setFixedWidth(PANEL_WIDTH)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(12, 12, 12, 12)
        self._root.setSpacing(10)
        self.setStyleSheet(self._stylesheet())
        self._build_ui()

    # ------------------------------------------------------------ 样式

    def _dialog_palette(self) -> QPalette:
        p = self._pal
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor(p.bg))
        pal.setColor(QPalette.WindowText, QColor(p.text))
        pal.setColor(QPalette.Base, QColor(p.card))
        pal.setColor(QPalette.AlternateBase, QColor(p.bg))
        pal.setColor(QPalette.Text, QColor(p.text))
        pal.setColor(QPalette.Button, QColor(p.card))
        pal.setColor(QPalette.ButtonText, QColor(p.text))
        pal.setColor(QPalette.Highlight, QColor(p.accent))
        pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        return pal

    def _stylesheet(self) -> str:
        p = self._pal
        red = status_color("red", p.is_light)
        return f"""
        QDialog {{ background: {p.bg}; }}
        QLabel {{ color: {p.text}; }}
        QLabel#title {{ color: {p.text}; font-size: 15px; font-weight: 700; }}
        QLabel#sectionTitle {{ color: {p.text}; font-size: 14px; font-weight: 700; }}
        QLabel#muted {{ color: {p.muted}; font-size: 11px; }}
        QLabel#metricLabel {{ color: {p.muted}; font-size: 11px; }}
        QLabel#metricValue {{ color: {p.value}; font-size: 20px; font-weight: 700; }}
        QLabel#metricCount {{ color: {p.muted}; font-size: 10px; }}
        QLabel#health {{ font-size: 11px; font-weight: 600; }}
        QFrame#metricCard {{
            background: {p.card};
            border: 1px solid {p.card_border};
            border-radius: 9px;
        }}
        QFrame#errorCard {{
            background: {p.card};
            border: 1px solid {red};
            border-radius: 9px;
        }}
        QPushButton {{
            background: {p.card};
            color: {p.text};
            border: 1px solid {p.card_border};
            border-radius: 7px;
            padding: 5px 10px;
        }}
        QPushButton:hover {{ border-color: {p.accent}; }}
        QPushButton:checked {{ background: {p.accent}; color: #FFFFFF; border-color: {p.accent}; }}
        QPushButton#accountChip {{ border-radius: 11px; padding: 4px 9px; font-size: 11px; }}
        QLineEdit {{
            background: {p.card};
            color: {p.text};
            border: 1px solid {p.card_border};
            border-radius: 6px;
            padding: 5px;
        }}
        QProgressBar {{ background: {p.track}; border: none; border-radius: 2px; }}
        QProgressBar::chunk {{ border-radius: 2px; }}
        """

    # ------------------------------------------------------------ 公开接口

    def is_configuring(self) -> bool:
        return self._mode == "config"

    def reload(self) -> None:
        """数据/配置/语言变化后重建;内容没变就不动(防每分钟刷新闪一下)"""
        if self._mode == "config":
            return
        if self._view_sig is not None and self._view_sig == self._view_signature():
            return
        self._build_ui()

    def show_account(self, instance_id: str) -> None:
        """显示指定账号(点任务栏 chip 的入口)"""
        if self._config.instance(instance_id) is None:
            return
        if self._mode == "config":
            self._cancel_config()
        self._selected_id = instance_id
        self._mode = "main"
        self._build_ui()
        self.show()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ 定位

    def paintEvent(self, event) -> None:
        """强制自绘底色:不依赖原生样式是否吃 palette/stylesheet"""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self._pal.bg))
        painter.end()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._pal.is_light:
            from .winutil import set_dark_title_bar

            set_dark_title_bar(int(self.winId()), True)
        self._move_to_bottom_right()

    def resizeEvent(self, event) -> None:
        """窗口因切换账号变高/变矮后重新贴任务栏上沿。

        在 resize 事件里做,此时 height() 已经是新值;放在 _build_ui 里同步
        move 会用到旧高度,把窗口算到任务栏下面。
        """
        super().resizeEvent(event)
        if self.isVisible() and not getattr(self, "_repositioning", False):
            self._repositioning = True
            try:
                self._move_to_bottom_right()
            finally:
                self._repositioning = False

    def _move_to_bottom_right(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        bottom_limit = self._taskbar_top_edge(screen)
        if bottom_limit is None:
            bottom_limit = geo.bottom()
        # 用「客户区尺寸 + 原生边框」算整窗尺寸,不要用 frameGeometry():
        # 刚刚 adjustSize 后 frameGeometry 可能还是旧的,会把窗口算到任务栏下面。
        margins = None
        if self.windowHandle() is not None:
            margins = self.windowHandle().frameMargins()
        if margins is not None and (margins.top() or margins.bottom()):
            frame_w = self.width() + margins.left() + margins.right()
            frame_h = self.height() + margins.top() + margins.bottom()
        else:
            frame = self.frameGeometry()
            frame_w, frame_h = frame.width(), frame.height()
        x = geo.right() - frame_w - _MARGIN_RIGHT
        y = bottom_limit - frame_h - _MARGIN_BOTTOM
        # 内容比可用高度还高时先贴顶,至少标题栏和顶部内容可见
        y = max(geo.top() + 4, y)
        self.move(max(geo.left(), x), y)

    @staticmethod
    def _taskbar_top_edge(screen) -> Optional[int]:
        from .winutil import taskbar_top_edge

        return taskbar_top_edge(screen.geometry())

    # ------------------------------------------------------------ 整体构建

    def _build_ui(self) -> None:
        # 整窗重建期间关掉刷新,一次画完再开:否则切账号时能看到中间态闪烁
        self.setUpdatesEnabled(False)
        try:
            _clear_layout(self._root)
            self._save_button = None
            if self._mode == "config":
                self._build_config_view()
            else:
                self._ensure_selected()
                self._build_main_view()
            # 先让布局算完新尺寸再 adjustSize:否则 height() 还是旧值,
            # 按旧高度定位会把窗口算到任务栏下面
            self._root.invalidate()
            self._root.activate()
            self.adjustSize()
            self._view_sig = self._view_signature()
            # 位置由 resizeEvent 在窗口真正变高后校正;这里不提前 move,
            # 否则 height() 还是旧值,会把窗口算到任务栏下面
        finally:
            self.setUpdatesEnabled(True)
        self.update()

    def _view_signature(self):
        """详情窗当前内容指纹:用于跳过无变化的整窗重建。"""
        inst = self._selected_instance()
        if inst is None:
            return ("no-account", self._mode, self._i18n.locale)
        data = self._tray.data(inst.id)
        error = self._tray.error(inst.id)
        metrics = tuple(
            (m.label, m.current_value, m.total_value, m.unit, m.reset_time)
            for m in (data.metrics if data else [])
        )
        return (
            self._mode,
            self._selected_id,
            inst.display_name,
            inst.platform_type.value,
            self._i18n.locale,
            self._config.display_mode(),
            self._config.is_configured(inst.id),
            data.is_healthy if data else None,
            metrics,
            error.kind.value if error else None,
            error.message if error else None,
            # 倒计时按分钟走:同一分钟内无变化不重建,跨分钟才刷新文案
            int(time.time() // 60),
        )

    def _build_main_view(self) -> None:
        self._root.addWidget(self._build_header())
        self._root.addWidget(self._build_account_switcher())
        self._root.addWidget(self._build_content())
        self._root.addWidget(self._build_footer())

    def _ensure_selected(self) -> None:
        enabled = [i for i in self._config.instances() if self._config.is_enabled(i.id)]
        ids = {i.id for i in enabled}
        if self._selected_id in ids:
            return
        active = self._config.active_instance_id()
        self._selected_id = active if active in ids else (enabled[0].id if enabled else None)

    def _selected_instance(self) -> Optional[PlatformInstance]:
        self._ensure_selected()
        if self._selected_id is None:
            return None
        return self._config.instance(self._selected_id)

    # ------------------------------------------------------------ 头部

    def _build_header(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        title = QLabel(self._i18n.t("app.name"))
        title.setObjectName("title")
        row.addWidget(title)
        row.addStretch(1)
        if self._selected_id and self._tray.is_loading(self._selected_id):
            loading = QLabel("…")
            loading.setObjectName("muted")
            row.addWidget(loading)
        box = QWidget()
        box.setLayout(row)
        return box

    # ------------------------------------------------------------ 账号切换

    def _build_account_switcher(self) -> QWidget:
        """账号切换条:点一下切换;按住标签拖到另一个上可交换顺序。"""
        self._reorder_area = _ReorderArea(self)
        self._reorder_area.reorder_requested.connect(self._reorder)
        row = self._reorder_area.layout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        selected = self._selected_id or self._config.active_instance_id()
        for inst in self._config.instances():
            if not self._config.is_enabled(inst.id):
                continue
            btn = _ReorderButton(inst.id, inst.display_name)
            btn.setObjectName("accountChip")
            btn.setCheckable(True)
            btn.setChecked(inst.id == selected)
            btn.setIcon(_dot_icon(self._account_color(inst)))
            btn.setIconSize(QSize(8, 8))
            btn.setToolTip(self._i18n.t("detail.dragToReorder"))
            btn.clicked.connect(lambda checked=False, iid=inst.id: self._switch_account(iid))
            row.addWidget(btn)
        row.addStretch(1)
        return self._reorder_area

    def _account_color(self, inst: PlatformInstance) -> str:
        data = self._tray.data(inst.id)
        if data is None and self._tray.error(inst.id) is not None:
            return status_color("red", self._pal.is_light)
        return status_color(render.status_color(data), self._pal.is_light)

    def _reorder(self, source_id: str, target_id: str) -> None:
        ids = [i.id for i in self._config.instances()]
        if source_id not in ids or target_id not in ids or source_id == target_id:
            return
        ids.remove(source_id)
        ids.insert(ids.index(target_id), source_id)
        with self._tray._config_write() as st:
            self._config.reorder_instances(ids)
        if not st.ok:
            return
        self._build_ui()
        self._tray._on_config_changed()

    def _switch_account(self, instance_id: str) -> None:
        if self._config.instance(instance_id) is None:
            return
        previous = self._selected_id
        self._selected_id = instance_id
        with self._tray._config_write() as st:
            self._config.set_active_instance_id(instance_id)
        if not st.ok:
            self._selected_id = previous
            self._build_ui()
            return
        if self._config.is_configured(instance_id) and self._tray.data(instance_id) is None:
            self._tray.fetch_instance_async(instance_id)
        self._build_ui()

    # ------------------------------------------------------------ 主体

    def _build_content(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        inst = self._selected_instance()
        if inst is None:
            self._fill_no_accounts(layout)
        else:
            self._fill_account_section(layout, inst)
        return box

    def _fill_account_section(self, layout: QVBoxLayout, inst: PlatformInstance) -> None:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {self._account_color(inst)}; font-size: 12px;")
        header.addWidget(dot)
        name = QLabel(inst.display_name)
        name.setObjectName("sectionTitle")
        header.addWidget(name)
        platform = QLabel(inst.platform_type.display_name())
        platform.setObjectName("muted")
        header.addWidget(platform)
        header.addStretch(1)

        data = self._tray.data(inst.id)
        error = self._tray.error(inst.id)
        if error is not None and data is None:
            state = QLabel(self._error_text(error, inst.platform_type))
            state.setObjectName("muted")
            state.setStyleSheet(f"color: {status_color('red', self._pal.is_light)};")
            header.addWidget(state)
        elif data is not None:
            state = QLabel(
                self._i18n.t("popover.normal" if data.is_healthy else "popover.low")
            )
            state.setStyleSheet(
                f"color: {status_color('green' if data.is_healthy else 'red', self._pal.is_light)};"
                " font-weight: 600;"
            )
            header.addWidget(state)
        layout.addLayout(header)

        if not self._config.is_configured(inst.id):
            self._fill_unconfigured(layout, inst)
            return
        if data is not None and data.metrics:
            self._fill_metrics(layout, data, error)
        elif error is not None:
            self._fill_error(layout, error, inst)
        elif self._tray.is_loading(inst.id):
            self._fill_loading(layout)
        else:
            self._fill_no_data(layout)

    def _fill_metrics(self, layout: QVBoxLayout, data, error: Optional[PlatformError]) -> None:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        metrics = data.metrics
        for index, metric in enumerate(metrics):
            card = self._build_metric_card(metric)
            row, col = divmod(index, 2)
            span = 2 if (len(metrics) == 1 or (index == len(metrics) - 1 and col == 0)) else 1
            grid.addWidget(card, row, col, 1, span)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        wrapper = QWidget()
        wrapper.setLayout(grid)
        layout.addWidget(wrapper)

        health_row = QHBoxLayout()
        health_row.setContentsMargins(0, 0, 0, 0)
        healthy = data.is_healthy
        dot = QLabel("●")
        dot.setStyleSheet(
            f"color: {status_color('green' if healthy else 'red', self._pal.is_light)}; font-size: 11px;"
        )
        health_row.addWidget(dot)
        health = QLabel(self._i18n.t("popover.normal") if healthy else self._i18n.t("popover.low"))
        health.setObjectName("health")
        health.setStyleSheet(
            f"color: {status_color('green' if healthy else 'red', self._pal.is_light)};"
        )
        health_row.addWidget(health)
        health_row.addStretch(1)
        layout.addLayout(health_row)

        if error is not None:
            stale = QLabel(self._i18n.t("popover.staleData"))
            stale.setObjectName("muted")
            stale.setStyleSheet(f"color: #FF9500;")
            stale.setWordWrap(True)
            layout.addWidget(stale)

    def _build_metric_card(self, metric: UsageMetric) -> QWidget:
        card = QFrame()
        card.setObjectName("metricCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        key = f"metric.{metric.label}"
        title_text = self._i18n.t(key)
        if title_text == key:
            title_text = metric.label
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        title = QLabel(title_text)
        title.setObjectName("metricLabel")
        title.setWordWrap(True)
        top.addWidget(title, 1)
        hint = ""
        if metric.unit == "%" and metric.label != LABEL_WEEKLY_UNLIMITED:
            hint = self._i18n.t(
                "popover.used" if self._config.display_mode() == "used" else "popover.remaining"
            )
        elif metric.total_value is not None and metric.total_value > 0:
            hint = self._i18n.t("popover.remaining")
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("metricCount")
            top.addWidget(hint_label, 0, Qt.AlignRight | Qt.AlignTop)
        v.addLayout(top)

        value_text, ratio = self._metric_card_value(metric)
        value_color = self._pal.value
        if ratio is not None:
            if ratio < 0.1:
                value_color = status_color("red", self._pal.is_light)
            elif ratio < 0.5:
                value_color = status_color("yellow", self._pal.is_light)
        value = QLabel(value_text)
        value.setObjectName("metricValue")
        size = 20 if len(value_text) <= 9 else 15
        value.setStyleSheet(f"font-size: {size}px; font-weight: 700; color: {value_color};")
        v.addWidget(value)

        if metric.reset_time is not None:
            countdown = QLabel(self._reset_countdown_text(metric))
            countdown.setObjectName("metricCount")
            countdown.setWordWrap(True)
            v.addWidget(countdown)

        if ratio is not None and metric.unit == "%" and metric.label != LABEL_WEEKLY_UNLIMITED:
            shown = ratio if self._config.display_mode() == "remaining" else 1.0 - ratio
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(max(0, min(1000, int(shown * 1000))))
            bar.setFixedHeight(4)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {self._pal.track}; border: none;"
                " border-radius: 2px; }"
                f"QProgressBar::chunk {{ background: {value_color}; border-radius: 2px; }}"
            )
            v.addWidget(bar)
        elif ratio is not None and metric.unit == "times":
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(max(0, min(1000, int(ratio * 1000))))
            bar.setFixedHeight(4)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {self._pal.track}; border: none;"
                " border-radius: 2px; }"
                f"QProgressBar::chunk {{ background: {value_color}; border-radius: 2px; }}"
            )
            v.addWidget(bar)
        return card

    def _metric_card_value(self, metric: UsageMetric) -> tuple[str, Optional[float]]:
        """(卡片大号数值, 剩余比例用于进度条/配色)"""
        if metric.unit == "unlimited" or metric.label == LABEL_WEEKLY_UNLIMITED:
            return "∞", None
        ratio = render.metric_ratio(metric)
        if metric.unit == "%":
            number = render.tray_text(metric, self._config.display_mode())
            return f"{number}%", ratio
        if metric.unit == "times":
            return render.metric_detail_text(metric, self._i18n.locale), ratio
        return render.metric_detail_text(metric, self._i18n.locale), None

    def _reset_countdown_text(self, metric: UsageMetric) -> str:
        kind, payload = render.reset_countdown(metric.reset_time)
        if kind == "days":
            days, hours = payload
            return self._i18n.t("weekly.reset.remaining", days, hours)
        prefix = self._i18n.t("popover.expiry" if metric.label == LABEL_BALANCE else "popover.reset")
        if kind == "soon":
            body = self._i18n.t("daily.reset.soon")
        else:
            hours, minutes = payload
            if hours > 0:
                body = self._i18n.t("daily.reset.remaining", hours, minutes)
            else:
                body = self._i18n.t("daily.reset.minutesonly", minutes)
        return f"{prefix}: {body}"

    # ------------------------------------------------------------ 空态/错误

    def _fill_no_accounts(self, layout: QVBoxLayout) -> None:
        label = QLabel(self._i18n.t("popover.selectPlatforms"))
        label.setObjectName("sectionTitle")
        label.setWordWrap(True)
        layout.addWidget(label)

    def _fill_unconfigured(self, layout: QVBoxLayout, inst: PlatformInstance) -> None:
        title = QLabel(self._i18n.t("popover.platformNotConfigured", inst.display_name))
        title.setObjectName("sectionTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        btn = QPushButton(self._i18n.t("popover.configureNow"))
        btn.clicked.connect(lambda checked=False, iid=inst.id: self._open_config(iid))
        layout.addWidget(btn)

    def _fill_error(self, layout: QVBoxLayout, error: PlatformError, inst: PlatformInstance) -> None:
        card = QFrame()
        card.setObjectName("errorCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)
        head = QLabel(self._i18n.t("popover.error"))
        head.setStyleSheet(
            f"color: {status_color('red', self._pal.is_light)}; font-weight: 700;"
        )
        v.addWidget(head)
        body = QLabel(self._error_text(error, inst.platform_type))
        body.setWordWrap(True)
        v.addWidget(body)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        if inst.platform_type.cookie_based():
            relogin = QPushButton(self._i18n.t("popover.renewSession"))
            relogin.clicked.connect(
                lambda checked=False, iid=inst.id: self._begin_relogin(iid)
            )
            actions.addWidget(relogin)
        configure = QPushButton(self._i18n.t("popover.configureNow"))
        configure.clicked.connect(lambda checked=False, iid=inst.id: self._open_config(iid))
        actions.addWidget(configure)
        actions.addStretch(1)
        v.addLayout(actions)
        layout.addWidget(card)

    def _fill_loading(self, layout: QVBoxLayout) -> None:
        loading = QLabel("…")
        loading.setAlignment(Qt.AlignCenter)
        loading.setObjectName("muted")
        layout.addWidget(loading)

    def _fill_no_data(self, layout: QVBoxLayout) -> None:
        head = QLabel(self._i18n.t("popover.noData"))
        head.setObjectName("sectionTitle")
        layout.addWidget(head)
        body = QLabel(self._i18n.t("error.invalidResponse"))
        body.setObjectName("muted")
        body.setWordWrap(True)
        layout.addWidget(body)

    def _error_text(self, error: PlatformError, platform: PlatformType) -> str:
        i18n = self._i18n
        if error.kind == ErrorKind.UNAUTHORIZED:
            if platform.cookie_based():
                return i18n.t("error.unauthorized.session")
            return i18n.t("error.unauthorized.key")
        translated = i18n.t(error.message)
        if translated != error.message:
            return translated
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

    # ------------------------------------------------------------ 底部

    def _build_footer(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        refresh = QPushButton(self._i18n.t("menu.refreshNow"))
        refresh.clicked.connect(self._tray.fetch_all_async)
        row.addWidget(refresh)
        inst = self._selected_instance()
        configure = QPushButton(self._i18n.t("menu.configurePlatform"))
        configure.setEnabled(inst is not None)
        configure.clicked.connect(
            lambda checked=False, iid=inst.id if inst else None: self._open_config(iid)
        )
        row.addWidget(configure)
        if inst is not None and inst.platform_type.cookie_based():
            renew = QPushButton(self._i18n.t("popover.renewSession"))
            renew.clicked.connect(lambda checked=False, iid=inst.id: self._begin_relogin(iid))
            row.addWidget(renew)
        row.addStretch(1)
        box = QWidget()
        box.setLayout(row)
        return box

    # ------------------------------------------------------------ 配置面板

    def _build_config_view(self) -> None:
        inst = self._config.instance(self._config_instance_id) if self._config_instance_id else None
        if inst is None:
            self._mode = "main"
            self._build_main_view()
            return
        i18n = self._i18n
        title_key, placeholder_key = _config_i18n_keys(inst.platform_type)

        title = QLabel(i18n.t(title_key, inst.display_name))
        title.setObjectName("sectionTitle")
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
        self._save_button = save

    def _begin_relogin(self, instance_id: str) -> None:
        """错误态的「重新登录」:进配置面板并直接开浏览器登录"""
        inst = self._config.instance(instance_id)
        if inst is None:
            return
        self._open_config(instance_id)
        self._open_login(inst.platform_type)

    def _open_login(self, platform: PlatformType) -> None:
        from .browser_login import BrowserLoginSession, find_browser

        i18n = self._i18n
        if find_browser() is None:
            QMessageBox.warning(self, i18n.t("app.name"), i18n.t("login.noBrowser"))
            return

        session = BrowserLoginSession(platform)
        if not session.start():
            session.close()
            QMessageBox.warning(self, i18n.t("app.name"), i18n.t("login.noBrowser"))
            return

        dlg = _WaitLoginDialog(self, platform.display_name())
        accepted = False
        credential = None
        try:
            accepted = dlg.exec_() == QDialog.Accepted
            if accepted:
                credential = session.credential()
        finally:
            session.close()
        if not accepted:
            return
        if credential:
            self._api_key_text = credential
            self._build_ui()
        else:
            QMessageBox.warning(
                self,
                i18n.t("login.missing.title"),
                i18n.t("login.missing.message"),
            )

    def _open_config(self, instance_id: Optional[str]) -> None:
        if not instance_id:
            return
        self._selected_id = instance_id
        self._config_instance_id = instance_id
        self._api_key_text = self._config.api_key(instance_id)
        self._mode = "config"
        self._build_ui()

    def _on_api_key_changed(self, text: str) -> None:
        self._api_key_text = text
        if self._save_button is not None:
            self._save_button.setEnabled(bool(text.strip()))

    def _cancel_config(self) -> None:
        instance_id = self._config_instance_id
        self._mode = "main"
        self._config_instance_id = None
        self._api_key_text = ""
        self._build_ui()
        if instance_id:
            self._tray._on_config_cancelled(instance_id)

    def _save_config(self) -> None:
        instance_id = self._config_instance_id
        key = self._api_key_text.strip()
        if not instance_id or not key:
            return
        if self._config.instance(instance_id) is None:
            self._mode = "main"
            self._config_instance_id = None
            self._api_key_text = ""
            self._build_ui()
            return
        try:
            self._config.set_api_key(instance_id, key)
        except (OSError, TypeError, UnicodeEncodeError) as e:
            QMessageBox.warning(
                self,
                self._i18n.t("app.name"),
                self._i18n.t("error.saveFailed", str(e)),
            )
            return
        self._tray._on_config_saved(instance_id)
        self._tray.refresh_instance(instance_id)
        self._selected_id = instance_id
        self._mode = "main"
        self._config_instance_id = None
        self._api_key_text = ""
        self._build_ui()

    def reject(self) -> None:
        if self._mode == "config":
            self._cancel_config()
            return
        super().reject()


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

    def __init__(self, parent=None, platform_name: str = ""):
        super().__init__(parent)
        i18n = get_i18n()
        self.setWindowTitle(i18n.t("login.title", platform_name))
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        label = QLabel(i18n.t("login.browserHint"))
        label.setWordWrap(True)
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
    if platform == PlatformType.TOKENRHYTHM:
        return "popover.configurePlatform.tokenrhythm", "popover.inputPlaceholder.tokenrhythm"
    if platform == PlatformType.STEPFUN:
        return "popover.configurePlatform.stepfun", "popover.inputPlaceholder.stepfun"
    return "popover.configurePlatform", "popover.inputPlaceholder"
