# -*- coding: utf-8 -*-
"""任务栏数字面板(StatusOverlay):Windows 上 Mac 菜单栏的对等物。

单行设计:「首字母(状态色) 数值(白色)」,如 M1 81 / M2 100:
- 账号标签用平台首字母(M/G/S/J),同平台多账号加序号(M1、M2),不显示中文名
- 首字母随额度变色(绿/黄/红/灰),数值恒为白色——标签提示是哪个号,数字才是主角
- 字号放大、行距舒展,配淡色圆角底板;右下角空间充裕,不必挤成一小条
- 不抢焦点(WA_ShowWithoutActivating + WindowDoesNotAcceptFocus),点别处不会导致它消失
- 常驻置顶、可整体拖拽到任务栏任意位置(位置持久化),仅在真被任务栏盖住时才重新置顶
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from PyQt5.QtCore import QPoint, QRect, QTimer, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PyQt5.QtWidgets import QWidget

from .. import render
from ..models import PlatformInstance, PlatformType
from .winutil import ensure_topmost, is_covered_by_taskbar, taskbar_rect

#: 面板尺寸(逻辑像素)
BAR_HEIGHT = 34
CHIP_H = 28
#: 块间距(组与组之间的结构性分隔)
CHIP_GAP = 20
PADDING = 8

#: 平台首字母(J = 基元律动 TokenRhythm)
_PLATFORM_INITIAL = {
    PlatformType.MINIMAX_CN: "M",
    PlatformType.GLM_CN: "G",
    PlatformType.STEPFUN: "S",
    PlatformType.TOKENRHYTHM: "J",
}

#: 深色任务栏配色(底板略深于任务栏,形成卡片感)
_DARK = {
    "plate": QColor(255, 255, 255, 16),
    "edge": QColor(255, 255, 255, 28),
    "text": QColor("#FFFFFF"),
    "hover": QColor(255, 255, 255, 38),
}
#: 浅色任务栏配色
_LIGHT = {
    "plate": QColor(0, 0, 0, 18),
    "edge": QColor(0, 0, 0, 28),
    "text": QColor("#1E1E1E"),
    "hover": QColor(0, 0, 0, 40),
}

#: 首字母用状态色(与托盘点同一套语义色)
_STATUS_QCOLOR = {
    render.COLOR_GRAY: QColor("#8E8E93"),
    render.COLOR_RED: QColor("#FF453A"),
    render.COLOR_YELLOW: QColor("#FFD60A"),
    render.COLOR_GREEN: QColor("#32D74B"),
}

#: 任务栏右侧托盘区预留宽度(面板默认位置避让"^"与托盘图标)
_TRAY_RESERVE = 340

#: 标签→数字间距
_GAP_LABEL_NUM = 8
_NUM_FONT_PT = 15
_LABEL_FONT_PT = 11


def _taskbar_is_light() -> bool:
    """系统任务栏是否浅色主题(浅色时用深字,否则白字)"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "SystemAppsUseLightTheme")
            return bool(value)
    except OSError:
        return False


def account_initials(instances: list[PlatformInstance]) -> dict[str, str]:
    """账号短标签:平台首字母;同平台多账号加序号(M1/M2),单账号不带号(G/S/J)"""
    totals = Counter(_PLATFORM_INITIAL[i.platform_type] for i in instances)
    seen: Counter = Counter()
    labels: dict[str, str] = {}
    for inst in instances:
        base = _PLATFORM_INITIAL[inst.platform_type]
        seen[base] += 1
        labels[inst.id] = base if totals[base] == 1 else f"{base}{seen[base]}"
    return labels


class StatusOverlay(QWidget):
    """任务栏常驻数字面板;数据/配置一律读 TrayApp,自身不取数。"""

    def __init__(self, tray):
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 显示但不激活:点面板/点别处都不改变焦点,也不会因此被重绘掉
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self._tray = tray
        self._chips: list[tuple[QRect, str]] = []    # [(QRect, instance_id)]
        self._hover_index = -1
        self._press_pos: Optional[QPoint] = None     # 按下时光标全局位置
        self._press_win_pos: Optional[QPoint] = None # 按下时窗口位置
        self._dragging = False
        self._placed = False                         # 是否已定过位(默认或记忆)
        # 置顶守护:任务栏本身、以及 ZCode 这类置顶窗口都会盖住面板。
        # 用 500ms 的轻量检测(仅 GetWindow + GetClassName)判断是否真被盖,
        # 被盖才重新置顶——无条件 SetWindowPos 会让透明窗口闪一下。
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._ensure_visible)
        self._topmost_timer.start(500)
        self.refresh()

    def _ensure_visible(self) -> None:
        """仅在真被任务栏盖住时重新置顶(避免透明窗口周期性闪烁)"""
        if not self.isVisible():
            return
        if is_covered_by_taskbar(int(self.winId())):
            ensure_topmost(int(self.winId()))

    # ------------------------------------------------------------ 数据与布局

    def refresh(self) -> None:
        """数据/配置变化后重建;保持用户拖过的位置"""
        self._hover_index = -1
        self._measure()
        self.update()
        if not self._placed:
            self._place_default()
            self._placed = True
        else:
            pos = self._tray.config.overlay_pos()
            if pos is not None:
                self.move(*pos)
        # 提到最前:任务栏是 topmost 窗口,不提就被压在下面看不见
        ensure_topmost(int(self.winId()))

    def overlay_instances(self) -> list[PlatformInstance]:
        return [it for it in self._tray.config.instances() if self._tray.config.is_enabled(it.id)]

    def _chip_texts(self, inst: PlatformInstance) -> tuple[str, str]:
        """(首字母标签, 数字文本)"""
        data = self._tray.data(inst.id)
        metrics = self._tray._visible_metrics(inst.id)
        text = render.tray_text(metrics[0], self._tray.config.display_mode()) if metrics else "--"
        if self._tray.is_loading(inst.id) and not metrics:
            text = "…"
        label = account_initials(self.overlay_instances()).get(inst.id, "?")
        return label, text

    def _measure(self) -> None:
        """按字宽度量算总宽(绘制与命中用同一套坐标)"""
        num_fm = QFontMetrics(self._num_font())
        label_fm = QFontMetrics(self._label_font())
        instances = self.overlay_instances()
        width = PADDING * 2
        for index, inst in enumerate(instances):
            label, text = self._chip_texts(inst)
            width += label_fm.horizontalAdvance(label) + _GAP_LABEL_NUM
            width += num_fm.horizontalAdvance(text)
            if index < len(instances) - 1:
                width += CHIP_GAP
        # 宽度不变时不要重设尺寸:setFixedSize 会触发窗口重建与闪烁
        target = max(width, 80)
        if self.width() != target:
            self.setFixedSize(target, BAR_HEIGHT)

    @staticmethod
    def _num_font() -> QFont:
        font = QFont("Segoe UI", _NUM_FONT_PT)
        font.setBold(True)
        return font

    @staticmethod
    def _label_font() -> QFont:
        # DemiBold 而非 Bold:标签与数字同粗会抢戏
        font = QFont("Segoe UI", _LABEL_FONT_PT)
        font.setWeight(QFont.DemiBold)
        return font

    def _place_default(self) -> None:
        """默认位置:任务栏右段、托盘区左侧;用户拖拽过则用记忆位置"""
        remembered = self._tray.config.overlay_pos()
        if remembered is not None:
            self.move(*remembered)
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        sgeo = screen.geometry()
        tb = taskbar_rect()
        if tb is not None:
            # +2px 下沉:对齐托盘图标的光学中心(纯居中会偏上)
            y = tb.top + max(0, (tb.bottom - tb.top - self.height()) // 2 + 2)
            x = tb.right - _TRAY_RESERVE - self.width() - 12
        else:
            y = sgeo.bottom() - self.height() - 4
            x = sgeo.right() - _TRAY_RESERVE - self.width() - 12
        self.move(max(sgeo.left(), x), max(sgeo.top(), y))

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = _LIGHT if _taskbar_is_light() else _DARK
        num_fm = QFontMetrics(self._num_font())
        label_fm = QFontMetrics(self._label_font())
        band_top = (self.height() - CHIP_H) // 2
        # 全行统一基线:以数字行的基线为准,标签落在同一条线上
        baseline = band_top + (CHIP_H - num_fm.height()) // 2 + num_fm.ascent()
        self._chips = []

        # 底板:淡色圆角卡片,让面板在任务栏上"有块"、不飘
        plate = QRect(PADDING // 2, band_top - 4, self.width() - PADDING, CHIP_H + 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(palette["plate"])
        painter.drawRoundedRect(plate, 7, 7)

        x = PADDING
        instances = self.overlay_instances()
        labels = account_initials(instances)
        for index, inst in enumerate(instances):
            chip_left = x
            data = self._tray.data(inst.id)
            metrics = self._tray._visible_metrics(inst.id)
            text = render.tray_text(metrics[0], self._tray.config.display_mode()) if metrics else "--"
            if self._tray.is_loading(inst.id) and not metrics:
                text = "…"
            label = labels.get(inst.id, "?")
            status = render.status_color(data)
            color = _STATUS_QCOLOR.get(status, _STATUS_QCOLOR[render.COLOR_GRAY])

            label_w = label_fm.horizontalAdvance(label)
            num_w = num_fm.horizontalAdvance(text)
            chip_w = label_w + _GAP_LABEL_NUM + num_w

            if index == self._hover_index:  # 悬停高亮画在文字下层
                painter.setPen(Qt.NoPen)
                painter.setBrush(palette["hover"])
                painter.drawRoundedRect(
                    chip_left - 6, band_top - 2, chip_w + 12, CHIP_H + 4, 5, 5
                )

            # 首字母标签:状态色(随额度绿/黄/红)
            painter.setFont(self._label_font())
            painter.setPen(color)
            painter.drawText(QPoint(x, baseline), label)
            x += label_w + _GAP_LABEL_NUM

            # 数值:纯白粗体(数字才是主角)
            painter.setFont(self._num_font())
            painter.setPen(palette["text"])
            painter.drawText(QPoint(x, baseline), text)
            x += num_w

            self._chips.append(
                (QRect(chip_left - 6, band_top - 2, chip_w + 12, CHIP_H + 4), inst.id)
            )
            x += CHIP_GAP

        painter.end()

    # ------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPos()
            self._press_win_pos = self.pos()
            self._dragging = False
        elif event.button() == Qt.RightButton:
            self._tray._show_menu()

    def mouseMoveEvent(self, event) -> None:
        index = self._chip_at(event.pos())
        if index != self._hover_index:
            self._hover_index = index
            self.setCursor(Qt.PointingHandCursor if index is not None else Qt.ArrowCursor)
        if self._press_pos is not None:
            delta = event.globalPos() - self._press_pos
            if self._dragging or delta.manhattanLength() > 4:
                self._dragging = True
                self.move(self._press_win_pos + delta)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            moved = (event.globalPos() - self._press_pos).manhattanLength()
            if self._dragging and moved > 4:
                self._tray.config.set_overlay_pos((self.x(), self.y()))  # 记住位置
            elif not self._dragging:
                index = self._chip_at(event.pos())
                if index is not None:
                    instance_id = self._chips[index][1]
                    self._tray.config.set_active_instance_id(instance_id)
                    self._tray._toggle_detail()
                    self.update()
        self._press_pos = None
        self._press_win_pos = None
        self._dragging = False

    def _chip_at(self, pos) -> Optional[int]:
        for index, (rect, _) in enumerate(self._chips):
            if rect.contains(pos):
                return index
        return None
