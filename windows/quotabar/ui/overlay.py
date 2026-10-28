# -*- coding: utf-8 -*-
"""任务栏数字面板(StatusOverlay):Windows 上 Mac 菜单栏的对等物。

单行精简设计(TrafficMonitor 式层级):「首字母 数字」,如 M1 100 / G 42:
- 账号标签用平台首字母(M/G/S/J),同平台多账号加序号(M1、M2),不显示中文名
- 数字直接用状态色(绿/黄/红/灰)——省掉状态圆点,少一个元素多一分简洁
- 全行统一基线;块间距固定,宽度按字宽度量精确排布
- 常驻置顶、无边框、可整体拖拽到任务栏任意位置(位置持久化)
- 左键账号块:切换激活账号并打开详情窗;右键:完整菜单
- 深浅任务栏主题自适应(读系统 AppsUseLightTheme)
- 托盘菜单「任务栏数字面板」可整体开关
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PyQt5.QtWidgets import QWidget

from .. import render
from ..models import PlatformInstance, PlatformType
from .winutil import taskbar_rect

#: 面板尺寸(逻辑像素)
BAR_HEIGHT = 26
CHIP_H = 22
#: 块间距(组与组之间的结构性分隔,读成两组数据的关键)
CHIP_GAP = 14
PADDING = 5

#: 平台首字母(J = 基元律动 TokenRhythm)
_PLATFORM_INITIAL = {
    PlatformType.MINIMAX_CN: "M",
    PlatformType.GLM_CN: "G",
    PlatformType.STEPFUN: "S",
    PlatformType.TOKENRHYTHM: "J",
}

#: 深色任务栏配色
_DARK = {"label": "#C8C8C8", "hover": QColor(255, 255, 255, 26)}
#: 浅色任务栏配色
_LIGHT = {"label": "#3A3A3A", "hover": QColor(0, 0, 0, 24)}

#: 数字用状态色(与托盘徽章同一套语义色)
_STATUS_QCOLOR = {
    render.COLOR_GRAY: QColor("#8E8E93"),
    render.COLOR_RED: QColor("#FF453A"),
    render.COLOR_YELLOW: QColor("#FFD60A"),
    render.COLOR_GREEN: QColor("#32D74B"),
}

#: 任务栏右侧托盘区预留宽度(面板默认位置避让"^"与托盘图标)
_TRAY_RESERVE = 340

#: 标签→数字间距
_GAP_LABEL_NUM = 5
_NUM_FONT_PT = 11
_LABEL_FONT_PT = 8


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
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._tray = tray
        self._chips: list[tuple[QRect, str]] = []    # [(QRect, instance_id)]
        self._hover_index = -1
        self._press_pos: Optional[QPoint] = None     # 按下时光标全局位置
        self._press_win_pos: Optional[QPoint] = None # 按下时窗口位置
        self._dragging = False
        self._placed = False                         # 是否已定过位(默认或记忆)
        self.refresh()

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
        self.setFixedSize(max(width, 56), BAR_HEIGHT)

    @staticmethod
    def _num_font() -> QFont:
        font = QFont("Segoe UI", _NUM_FONT_PT)
        font.setBold(True)
        return font

    @staticmethod
    def _label_font() -> QFont:
        # DemiBold 而非 Bold:8px 字号下 Bold 会糊成墨团
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
        # 全行统一基线:以数字行的居中基线为准,标签落在同一条线上
        baseline = band_top + (CHIP_H - num_fm.height()) // 2 + num_fm.ascent()
        self._chips = []

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

            label_w = label_fm.horizontalAdvance(label)
            num_w = num_fm.horizontalAdvance(text)
            chip_w = label_w + _GAP_LABEL_NUM + num_w

            if index == self._hover_index:  # 悬停高亮画在文字下层
                painter.setPen(Qt.NoPen)
                painter.setBrush(palette["hover"])
                painter.drawRoundedRect(
                    chip_left - PADDING, band_top, chip_w + PADDING * 2, CHIP_H, 4, 4
                )

            # 首字母标签(小号,灰色,与数字同基线)
            painter.setFont(self._label_font())
            painter.setPen(QColor(palette["label"]))
            painter.drawText(QPoint(x, baseline), label)
            x += label_w + _GAP_LABEL_NUM

            # 数字(大号粗体,直接用状态色)
            color = _STATUS_QCOLOR.get(render.status_color(data), _STATUS_QCOLOR[render.COLOR_GRAY])
            painter.setFont(self._num_font())
            painter.setPen(color)
            painter.drawText(QPoint(x, baseline), text)
            x += num_w

            self._chips.append(
                (QRect(chip_left - PADDING, band_top - 2, chip_w + PADDING * 2, CHIP_H + 4), inst.id)
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
