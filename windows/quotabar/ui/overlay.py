# -*- coding: utf-8 -*-
"""任务栏数字面板(StatusOverlay):Windows 上 Mac 菜单栏的对等物。

托盘图标只有 16px,塞数字物理上不清晰;本面板直接悬浮在任务栏上(类似
TrafficMonitor 的小组件),每个账号一块「彩色状态点 + 大号粗体数字」:
- 常驻置顶、无边框、可整体拖拽到任务栏任意位置(位置持久化)
- 左键账号块:切换激活账号并打开详情窗;右键:完整菜单
- 深浅任务栏主题自适应(读系统 AppsUseLightTheme)
- 托盘菜单「任务栏数字面板」可整体开关
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .. import render
from ..models import PlatformInstance
from .winutil import taskbar_rect, taskbar_top_edge

#: 面板尺寸(逻辑像素)
BAR_HEIGHT = 34
CHIP_W, CHIP_H = 60, 28
CHIP_SPACING = 4
PADDING = 3

#: 深色任务栏配色(账号名统一近白,激活与否靠加粗区分,不做灰字——灰字在白字旁边像"被禁用")
_DARK = {"name": "#EDEDED", "text": "#FFFFFF", "hover": QColor(255, 255, 255, 26)}
#: 浅色任务栏配色
_LIGHT = {"name": "#2A2A2A", "text": "#1E1E1E", "hover": QColor(0, 0, 0, 24)}

_STATUS_QCOLOR = {
    render.COLOR_GRAY: QColor("#8E8E93"),
    render.COLOR_RED: QColor("#FF3B30"),
    render.COLOR_YELLOW: QColor("#FFB300"),
    render.COLOR_GREEN: QColor("#30D158"),
}

#: 任务栏右侧托盘区预留宽度(面板默认位置避让"^"与托盘图标)
_TRAY_RESERVE = 300


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


class StatusOverlay(QWidget):
    """任务栏常驻数字面板;数据/配置一律读 TrayApp,自身不取数。"""

    def __init__(self, tray):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._tray = tray
        self._chips: list[tuple[object, object]] = []   # [(QRect, PlatformInstance)]
        self._hover_index = -1
        self._press_pos: Optional[QPoint] = None        # 按下时光标全局位置
        self._press_win_pos: Optional[QPoint] = None    # 按下时窗口位置
        self._dragging = False
        self._placed = False                            # 是否已定过位(默认或记忆)
        self.refresh()

    # ------------------------------------------------------------ 数据与布局

    def refresh(self) -> None:
        """数据/配置变化后重建;保持用户拖过的位置"""
        self._hover_index = -1
        instances = [
            it for it in self._tray.config.instances() if self._tray.config.is_enabled(it.id)
        ]
        width = PADDING * 2 + len(instances) * CHIP_W + max(0, len(instances) - 1) * CHIP_SPACING
        self.setFixedSize(max(width, CHIP_W + PADDING * 2), BAR_HEIGHT)
        self.update()
        if not self._placed:
            self._place_default()
            self._placed = True
        else:
            pos = self._tray.config.overlay_pos()
            if pos is not None:
                self.move(*pos)

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
            # +2px 下沉:对齐托盘图标的光学中心(纯居中会偏上 3px)
            y = tb.top + max(0, (tb.bottom - tb.top - self.height()) // 2 + 2)
            x = tb.right - _TRAY_RESERVE - self.width() - 12
        else:
            y = sgeo.bottom() - self.height() - 4
            x = sgeo.right() - _TRAY_RESERVE - self.width() - 12
        self.move(max(sgeo.left(), x), max(sgeo.top(), y))

    def overlay_instances(self) -> list[PlatformInstance]:
        return [it for it in self._tray.config.instances() if self._tray.config.is_enabled(it.id)]

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = _LIGHT if _taskbar_is_light() else _DARK
        self._chips = []

        x = PADDING
        for index, inst in enumerate(self.overlay_instances()):
            rect = QRect(x, (self.height() - CHIP_H) // 2, CHIP_W, CHIP_H)
            self._chips.append((rect, inst))

            if index == self._hover_index:  # 悬停高亮(圆角块)
                painter.setPen(Qt.NoPen)
                painter.setBrush(palette["hover"])
                painter.drawRoundedRect(rect, 6, 6)

            data = self._tray.data(inst.id)
            metrics = self._tray._visible_metrics(inst.id)
            active = self._tray.config.active_instance_id() == inst.id

            # 行 1:账号名(小字,贴行区底部)。行区高 11px,
            # 与下方数字行留 2px 净空,避免抗锯齿光晕粘连成糊边;激活账号加粗
            name_font = QFont("Segoe UI", 8)
            name_font.setBold(active)
            painter.setFont(name_font)
            painter.setPen(QColor(palette["name"]))
            name_rect = QRect(rect.left() + 8, rect.top(), CHIP_W - 12, 11)
            painter.drawText(name_rect, int(Qt.AlignBottom | Qt.AlignLeft), inst.display_name)

            # 行 2:状态点 + 数字(大号粗体,占据行区 y+13 起)
            color = _STATUS_QCOLOR.get(render.status_color(data), _STATUS_QCOLOR[render.COLOR_GRAY])
            text = render.tray_text(metrics[0], self._tray.config.display_mode()) if metrics else "--"
            if self._tray.is_loading(inst.id) and not metrics:
                text = "…"
            num_font = QFont("Segoe UI", 11)
            num_font.setBold(True)
            painter.setFont(num_font)
            painter.setPen(QPen(color, 1))
            painter.setBrush(color)
            # 点心对齐数字行光学中心(y+21 ≈ 数字区中心 20.5),不再像上标
            painter.drawEllipse(rect.left() + 9, rect.top() + 17, 8, 8)
            painter.setPen(QColor(palette["text"]))
            num_rect = QRect(rect.left() + 20, rect.top() + 13, CHIP_W - 24, 15)
            painter.drawText(
                num_rect,
                int(Qt.AlignVCenter | Qt.AlignLeft),
                text,
            )
            x += CHIP_W + CHIP_SPACING
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
        # 悬停高亮
        index = self._chip_at(event.pos())
        if index != self._hover_index:
            self._hover_index = index
            self.update()
        # 拖拽整体移动
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
                    inst = self._chips[index][1]
                    self._tray.config.set_active_instance_id(inst.id)
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
