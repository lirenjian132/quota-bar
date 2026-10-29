# -*- coding: utf-8 -*-
"""任务栏数字面板(StatusOverlay):Windows 上 Mac 菜单栏的对等物。

**嵌入任务栏**——不是浮在任务栏上方的悬浮层,而是任务栏自己的元素,
像日期时间、网络图标那样是任务栏的一部分(Windows 的做法是 TaskbarBand:
把窗口 SetParent 到 Shell_TrayWnd)。

形态:每个账号一个小圆角方块,块内是百分比数字(状态色),横向并排。
- 账号标签用平台首字母(M/G/S/J),同平台多账号加序号(M1、M2),不显示中文名
- 数字随额度变色(绿/黄/红/灰)
- 嵌入后不能靠 alpha 透明,方块底色取任务栏实际背景色采样后提亮,视觉上与任务栏融为一体
- 悬停 tooltip 给出各账号明细;左键开详情窗;右键出完整菜单(含退出)
"""

from __future__ import annotations

import ctypes
from collections import Counter
from typing import Optional

from PyQt5.QtCore import QPoint, QRect, QTimer, Qt
from PyQt5.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter
from PyQt5.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from .. import render
from ..i18n import get_i18n
from ..models import PlatformInstance, PlatformType
from .winutil import (
    detach_from_taskbar,
    show_window,
    embed_in_taskbar,
    ensure_topmost,
    find_notify_area,
    find_taskbar_host,
    get_taskbar_bg_color,
    is_covered_by_taskbar,
    move_in_parent,
    taskbar_rect,
)

#: 行高与间距(纯文字式排版:「标签 数值」一段段并排,不用方块——任务栏里更干净)
BAR_TEXT_H = 30
#: 段与段之间的间距
BLOCK_GAP = 16
PADDING = 6

#: 距任务栏右端(托盘区)的预留宽度
_TRAY_RESERVE = 300

#: 平台首字母(J = 基元律动 TokenRhythm)
_PLATFORM_INITIAL = {
    PlatformType.MINIMAX_CN: "M",
    PlatformType.GLM_CN: "G",
    PlatformType.STEPFUN: "S",
    PlatformType.TOKENRHYTHM: "J",
}

#: 状态色(数字用)
#: 状态色(浅底浮窗用深一档,保证对比度;面板在任务栏上用亮色版)
_STATUS_QCOLOR = {
    render.COLOR_GRAY: QColor("#6E7784"),
    render.COLOR_RED: QColor("#D32F2F"),
    render.COLOR_YELLOW: QColor("#B87700"),
    render.COLOR_GREEN: QColor("#1E9E3E"),
}

#: 任务栏面板上的亮色版状态色
_STATUS_QCOLOR_BRIGHT = {
    render.COLOR_GRAY: QColor("#8E8E93"),
    render.COLOR_RED: QColor("#FF453A"),
    render.COLOR_YELLOW: QColor("#FFD60A"),
    render.COLOR_GREEN: QColor("#32D74B"),
}

#: 悬停浮窗固定宽度(列宽 300 + 间距 36 + 边距 28)
_TIP_WIDTH = 364

#: 字号与间距
#: 标签→数值间距
_GAP_LABEL_NUM = 5
_NUM_FONT_PT = 15
_LABEL_FONT_PT = 11


def _baseline(height: int, fm: QFontMetrics) -> int:
    # 行内基线 y:让文字在给定行高内垂直居中
    return (height - fm.height()) // 2 + fm.ascent()


def _taskbar_is_light() -> bool:
    """系统任务栏是否浅色主题"""
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


class _TooltipWindow(QWidget):
    """自绘悬停浮窗:一次列出**所有账号的全部指标**。

    形态:不透明的深色圆角卡片。背景必须由 QFrame 承载并显式设
    WA_StyledBackground —— translucent 窗口上直接给 QWidget 设 stylesheet 背景
    是画不出来的(会全透明)。
    位置固定在任务栏面板的正上方居中,不跟鼠标跑。
    """

    def __init__(self) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # 卡片容器:浅色底 + 深色字(对标 TrafficMonitor 的白底信息框)。
        # 选择器用 #tipCard 精确限定:写成 QFrame 会让 border 被所有子控件继承,
        # 结果每个单元格都出现一圈框线。
        self._frame = QFrame(self)
        self._frame.setObjectName("tipCard")
        self._frame.setAttribute(Qt.WA_StyledBackground, True)
        self._frame.setStyleSheet(
            "QFrame#tipCard { background-color: rgba(247, 248, 250, 246);"
            " border: 1px solid rgba(120, 130, 145, 90);"
            " border-radius: 10px; }"
            "QLabel { border: none; background: transparent; }"
        )
        outer.addWidget(self._frame)
        self._grid = QGridLayout(self._frame)
        self._grid.setContentsMargins(14, 11, 14, 11)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(5)
        # 列最小宽度;浮窗整体宽度另行固定(见 TIP_WIDTH)
        for col, width in ((0, 28), (1, 92), (2, 56), (3, 124)):
            self._grid.setColumnMinimumWidth(col, width)
        self._sig = None  # 上次内容签名,内容没变就不重建(重建会导致闪烁)

    def show_rows(self, rows: list[tuple[str, str, str, str, str]], anchor_rect) -> None:
        """rows: [(标签色, 账号标签, 指标名, 数值, 倒计时)]
        anchor_rect: 任务栏面板的屏幕矩形,浮窗固定显示在它正上方居中。
        """
        # 内容没变就不重建网格:重建会让浮窗闪一下(尤其鼠标移动时)
        sig = tuple(rows)
        if sig == self._sig and self.isVisible():
            return
        self._sig = sig

        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        line = 0
        last_acc = None
        for acc_color, acc, metric, value, extra in rows:
            if acc != last_acc:
                if last_acc is not None:
                    line += 1  # 账号之间留一行空隙
                acc_label = QLabel(acc)
                acc_label.setStyleSheet(
                    f"color:{acc_color}; font-size:12px; font-weight:bold;"
                    " border: none; background: transparent;"
                )
                self._grid.addWidget(acc_label, line, 0, Qt.AlignLeft | Qt.AlignVCenter)
                last_acc = acc

            m = QLabel(metric)
            m.setStyleSheet("color:#5A6270; font-size:11px; border: none; background: transparent;")
            self._grid.addWidget(m, line, 1, Qt.AlignLeft | Qt.AlignVCenter)

            v = QLabel(value)
            v.setStyleSheet("color:#1E2430; font-size:13px; font-weight:bold; border: none; background: transparent;")
            self._grid.addWidget(v, line, 2, Qt.AlignRight | Qt.AlignVCenter)

            e = QLabel(extra)
            e.setStyleSheet("color:#8A93A0; font-size:10px; border: none; background: transparent;")
            self._grid.addWidget(e, line, 3, Qt.AlignLeft | Qt.AlignVCenter)
            line += 1

        # 先 show 再 adjustSize:未显示的窗口 adjustSize 拿不到真实高度,
        # 会导致浮窗按默认高度定位、压住任务栏
        if not self.isVisible():
            self.show()
        self._grid.activate()
        # 宽度固定、高度随行数:宽度固定才不会因文字长短变化而上下跳动(看着像闪)
        self._frame.setFixedWidth(_TIP_WIDTH)
        self._frame.adjustSize()
        self.setFixedSize(_TIP_WIDTH, self._frame.height())
        show_window(int(self.winId()))  # Qt 的 show() 在 translucent 窗口上偶尔不生效

        x = anchor_rect.left() + (anchor_rect.width() - self.width()) // 2
        y = anchor_rect.top() - self.height() - 8
        screen = QGuiApplication.screenAt(QPoint(anchor_rect.left(), anchor_rect.top()))
        geo = screen.availableGeometry() if screen else None
        if geo is not None:
            x = max(geo.left() + 4, min(x, geo.right() - self.width() - 4))
            y = max(geo.top() + 4, y)
        self.move(x, y)
        self.raise_()
        # 置顶:置顶应用(如 ZCode)会盖住浮窗,提到 topmost 最前才看得见
        ensure_topmost(int(self.winId()))


class StatusOverlay(QWidget):
    """任务栏内的账号数字块组;数据/配置一律读 TrayApp,自身不取数。"""

    def __init__(self, tray):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # 不用 WA_TranslucentBackground:alpha 窗口的命中测试按像素 alpha 判定,
        # 文字旁的透明处会把鼠标直接穿透给下层窗口(置顶应用),hover/点击收不到。
        # 改成不透明窗口、底色取任务栏色 —— 视觉上与任务栏无缝,命中按整个矩形。
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self._tray = tray
        self._chips: list[tuple[QRect, str]] = []    # [(QRect, instance_id)]
        self._hover_index = -1
        self._embedded = False
        self._bg_color = get_taskbar_bg_color()
        self._tip = _TooltipWindow()
        # 置顶守护:任务栏与置顶应用(如 ZCode)都会盖住面板,
        # 用 500ms 轻量检测判断是否真被盖,被盖才重新置顶(避免透明窗口闪烁)
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._ensure_visible)
        self._topmost_timer.start(500)
        # 拖拽移动(位置持久化:想放左放右都行)
        self._press_pos: Optional[QPoint] = None
        self._press_win_pos: Optional[QPoint] = None
        self._dragging = False
        # 嵌入任务栏:真正成为任务栏的元素(不是浮在上面的图案)
        self._embed_host: Optional[int] = None
        self.refresh()

    def _win_pid(self) -> int:
        """本窗口所属进程 id(遮挡检测时排除同进程窗口)"""
        import ctypes
        import ctypes.wintypes as wt

        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(
            ctypes.c_void_p(int(self.winId())), ctypes.byref(pid)
        )
        return pid.value

    def _ensure_visible(self) -> None:
        """守护:嵌入模式下探测 explorer 重启;浮层模式下被盖则提回最前。

        悬停浮窗也要持续抢置顶——置顶应用(如 ZCode)铺满全屏,只在首次显示时
        提一次顶会被它重新压住,浮窗就"不出现"。
        """
        if self._embed_host is not None:
            # explorer 重启后任务栏句柄会变,需要重新嵌入
            if find_taskbar_host() != self._embed_host:
                self._reattach()
            return
        if not self.isVisible():
            return
        # 只在真被盖时才提顶:无条件 SetWindowPos 会让透明窗口反复重绘(=闪烁)。
        # 遮挡检测要排除本进程窗口,否则自家浮窗会被当成"遮挡者"互相抢。
        pid = self._win_pid()
        ignore = [int(self._tip.winId())]
        if is_covered_by_taskbar(int(self.winId()), ignore, exclude_pid=pid):
            ensure_topmost(int(self.winId()))
        if self._tip.isVisible() and is_covered_by_taskbar(
            int(self._tip.winId()), ignore, exclude_pid=pid
        ):
            ensure_topmost(int(self._tip.winId()))

    def _reattach(self) -> None:
        """explorer 重启后重新嵌入任务栏"""
        self._embed_host = None
        self.attach()

    def attach(self) -> None:
        """尝试嵌入任务栏(真正成为任务栏的元素)。

        顺序有讲究:先 show() 让 Qt 建好原生窗口,再外部改样式 + SetParent,
        之后**绝不能再动 Qt 的窗口几何**(setFixedSize/move 会把嵌入冲掉),
        定位一律走 Win32。
        """
        if self._embed_host is not None:
            return
        if not self._tray.config.overlay_embedded():
            return
        host = embed_in_taskbar(int(self.winId()))
        if host is None:
            return
        self._embed_host = host
        # 嵌入后不再用 alpha 透明(child window 不支持 layered),改用不透明底色
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._bg_color = get_taskbar_bg_color()
        self._place()

    def detach(self) -> None:
        """脱离任务栏,回到顶层置顶浮层"""
        if self._embed_host is None:
            return
        self._embed_host = None
        detach_from_taskbar(int(self.winId()))
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.show()
        self._place()
        ensure_topmost(int(self.winId()))

    def _place(self) -> None:
        """定位:嵌入模式走 Win32(父客户区坐标),浮层模式走 Qt 屏幕坐标。

        嵌入模式下**不能**调 Qt 的 setFixedSize/move——那会把外部 SetParent 冲掉。
        """
        instances = self.overlay_instances()
        label_fm = QFontMetrics(self._label_font())
        num_fm = QFontMetrics(self._num_font("100"))
        width = PADDING * 2
        for index, inst in enumerate(instances):
            label, text = self._chip_texts(inst)
            width += label_fm.horizontalAdvance(label) + _GAP_LABEL_NUM
            width += num_fm.horizontalAdvance(text)
            if index < len(instances) - 1:
                width += BLOCK_GAP
        width = max(width, 60)
        height = BAR_TEXT_H

        if self._embed_host is not None:
            # 位置:靠通知区(TrayNotifyWnd)左侧 或 靠开始按钮右侧,按配置锚点
            notify = find_notify_area(self._embed_host)
            tb = taskbar_rect()
            if tb is not None:
                tb_w = tb.right - tb.left
                tb_h = tb.bottom - tb.top
                anchor = self._tray.config.overlay_anchor()
                x = tb_w - width - _TRAY_RESERVE
                if anchor == "left":
                    x = 8  # 靠任务栏左端(开始按钮右侧)
                elif notify:
                    try:
                        from .winutil import RECT

                        nr = RECT()
                        ctypes.windll.user32.GetWindowRect(
                            ctypes.c_void_p(notify), ctypes.byref(nr)
                        )
                        x = (nr.left - tb.left) - width - 4
                    except Exception:
                        pass
                y = max(0, (tb_h - height) // 2)
                move_in_parent(int(self.winId()), x, y, width, height)
            return

        if self.width() != width or self.height() != height:
            self.setFixedSize(width, height)


        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        sgeo = screen.geometry()
        tb = taskbar_rect()

        remembered = self._tray.config.overlay_pos()
        if remembered is not None:
            # 记住的位置:必须 x 落在屏幕内、y 落在任务栏上,否则回默认
            # (只校验 y 的话,拖到屏幕外的面板重启后永久失联)
            x, y = remembered
            x_ok = sgeo.left() <= x <= sgeo.right() - 40
            y_ok = tb is not None and tb.top - 6 <= y <= tb.bottom + 6
            if x_ok and y_ok:
                self.move(x, y)
                return

        anchor = self._tray.config.overlay_anchor()
        if tb is not None:
            y = tb.top + max(0, (tb.bottom - tb.top - self.height()) // 2)
            if anchor == "left":
                x = tb.left + 8
            else:
                x = tb.right - _TRAY_RESERVE - self.width() - 12
        else:
            y = sgeo.bottom() - self.height() - 4
            x = sgeo.right() - _TRAY_RESERVE - self.width() - 12
        self.move(max(sgeo.left(), x), max(sgeo.top(), y))

    # ------------------------------------------------------------ 数据与布局

    def refresh(self) -> None:
        """数据/配置变化后重建"""
        self._hover_index = -1
        self._place()
        self.update()

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

    @staticmethod
    def _num_font(text: str) -> QFont:
        # 固定字号:三位数(如 100)与二位数(如 28)视觉大小必须一致,
        # 否则同一行里数字大小忽大忽小,看着不齐
        font = QFont("Segoe UI", _NUM_FONT_PT)
        font.setBold(True)
        return font

    @staticmethod
    def _label_font() -> QFont:
        # DemiBold:与数值同粗会抢戏,标签要退后一点
        font = QFont("Segoe UI", _LABEL_FONT_PT)
        font.setWeight(QFont.DemiBold)
        return font

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        instances = self.overlay_instances()
        labels = account_initials(instances)
        self._chips = []

        # 铺满任务栏色:与任务栏融为一体(不透明窗口,鼠标命中按整个矩形)
        base = QColor(*self._bg_color)
        painter.fillRect(self.rect(), base)
        light = _taskbar_is_light()
        # 数值恒为白(浅色任务栏用深色);标签用状态色随额度变色
        num_color = QColor("#FFFFFF") if not light else QColor("#1E1E1E")

        x = PADDING
        for index, inst in enumerate(instances):
            data = self._tray.data(inst.id)
            metrics = self._tray._visible_metrics(inst.id)
            text = render.tray_text(metrics[0], self._tray.config.display_mode()) if metrics else "--"
            if self._tray.is_loading(inst.id) and not metrics:
                text = "…"
            label = labels.get(inst.id, "?")
            color = _STATUS_QCOLOR_BRIGHT.get(
                render.status_color(data), _STATUS_QCOLOR_BRIGHT[render.COLOR_GRAY]
            )

            label_font = self._label_font()
            num_font = self._num_font(text)
            lfm = QFontMetrics(label_font)
            nfm = QFontMetrics(num_font)
            label_w = lfm.horizontalAdvance(label)
            num_w = nfm.horizontalAdvance(text)
            chip_w = label_w + _GAP_LABEL_NUM + num_w

            block = QRect(x, 0, chip_w, self.height())
            self._chips.append((block, inst.id))

            # 悬停:底色轻微提亮,提示可点
            if index == self._hover_index:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255, 22) if not light else QColor(0, 0, 0, 22))
                painter.drawRoundedRect(block.adjusted(-4, 3, 4, -3), 5, 5)

            # 首字母标签:状态色(随额度绿/黄/红)——M 字带颜色会变色
            painter.setFont(label_font)
            painter.setPen(color)
            painter.drawText(QPoint(x, _baseline(self.height(), lfm)), label)
            x += label_w + _GAP_LABEL_NUM

            # 数值:白色粗体
            painter.setFont(num_font)
            painter.setPen(num_color)
            painter.drawText(QPoint(x, _baseline(self.height(), nfm)), text)
            x += num_w + BLOCK_GAP

        painter.end()

    # ------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            self._press_pos = None
            self._press_win_pos = None
            self._dragging = False
        if event.button() == Qt.LeftButton:
            if self._embed_host is None:
                # 浮层模式才支持拖拽(嵌入后是任务栏子窗口,拖不出任务栏)
                self._press_pos = event.globalPos()
                self._press_win_pos = self.pos()
                self._dragging = False
        elif event.button() == Qt.RightButton:
            self._tray._show_menu()

    def mouseMoveEvent(self, event) -> None:
        # 按键已松开但没收到 release(例如右键开菜单时左键 release 被菜单吃掉)
        # → 拖拽状态残留,会让之后任意鼠标移动变成拖拽。按实际按键状态纠正。
        if self._press_pos is not None and not (event.buttons() & Qt.LeftButton):
            self._press_pos = None
            self._press_win_pos = None
            self._dragging = False
        # 拖拽中:跟手移动
        if self._press_pos is not None:
            delta = event.globalPos() - self._press_pos
            if self._dragging or delta.manhattanLength() > 5:
                self._dragging = True
                self.move(self._press_win_pos + delta)
                self._tip.hide()
                return
        index = self._chip_at(event.pos())
        changed = index != self._hover_index
        if changed:
            self._hover_index = index
            self.setCursor(Qt.PointingHandCursor if index is not None else Qt.ArrowCursor)
            self.update()
        # 悬停浮窗:鼠标进入**面板任意位置**(含两个号之间的空隙)就展示
        # 全部账号的全部数据——用户要的是"放过去就看全部",不是必须精确压在某个号上。
        inside = not self._dragging and self.rect().contains(event.pos())
        if inside:
            # 只在"进入/换目标"时刷新;鼠标在面板内移动不重建(否则一动就闪)
            if changed:
                self._tip.show_rows(self._tooltip_rows(), self._panel_screen_rect())
        elif self._tip.isVisible():
            self._tip.hide()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            moved = (event.globalPos() - self._press_pos).manhattanLength()
            if self._dragging and moved > 5:
                # 拖完记住位置:想放左放右都行
                self._tray.config.set_overlay_pos((self.x(), self.y()))
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

    def _panel_screen_rect(self) -> QRect:
        """面板在屏幕上的矩形(浮窗定位用,固定在面板正上方)"""
        origin = self.mapToGlobal(QPoint(0, 0))
        return QRect(origin.x(), origin.y(), self.width(), self.height())

    def _tooltip_rows(self) -> list[tuple[str, str, str, str, str]]:
        """浮窗内容:**所有账号**的全部指标(与鼠标停在哪个号上无关)。

        每行: (标签色, 账号标签, 指标名, 数值, 倒计时)
        """
        i18n = get_i18n()
        mode = self._tray.config.display_mode()
        rows: list[tuple[str, str, str, str, str]] = []
        labels = account_initials(self.overlay_instances())
        for inst in self.overlay_instances():
            data = self._tray.data(inst.id)
            # 全部指标(不是只显示勾选的那一两个)——用户要"放过去就看到全部数据"
            metrics = list(data.metrics) if data is not None else []
            acc = labels.get(inst.id, "?")
            color = _STATUS_QCOLOR.get(
                render.status_color(data), _STATUS_QCOLOR[render.COLOR_GRAY]
            ).name()
            if not metrics:
                rows.append((color, acc, i18n.t("popover.noData"), "--", ""))
                continue
            for metric in metrics:
                key = f"metric.{metric.label}"
                title = i18n.t(key)
                if title == key:
                    title = metric.label
                value = render.tray_text(metric, mode)
                if metric.unit == "%":
                    value = f"{value}%"
                extra = ""
                if metric.reset_time is not None:
                    kind, payload = render.reset_countdown(metric.reset_time)
                    if kind == "soon":
                        extra = i18n.t("daily.reset.soon")
                    elif kind == "hm":
                        hours, minutes = payload
                        if hours > 0:
                            extra = i18n.t("daily.reset.remaining", hours, minutes)
                        else:
                            extra = i18n.t("daily.reset.minutesonly", minutes)
                    elif kind == "days":
                        days, hours = payload
                        extra = i18n.t("weekly.reset.remaining", days, hours)
                rows.append((color, acc, title, value, extra))
        return rows

    def leaveEvent(self, event) -> None:
        self._tip.hide()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        # 面板被隐藏(菜单里关掉)时浮窗也会滞留屏幕上,一并收掉
        self._tip.hide()
        super().hideEvent(event)

    def _chip_at(self, pos) -> Optional[int]:
        for index, (rect, _) in enumerate(self._chips):
            if rect.contains(pos):
                return index
        return None
