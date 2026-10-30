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
import time
from collections import Counter
from typing import Optional

from PyQt5.QtCore import QPoint, QRect, QTimer, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import render
from ..i18n import get_i18n
from ..models import PlatformInstance, PlatformType
from .winutil import (
    detach_from_taskbar,
    embed_in_taskbar,
    ensure_topmost,
    find_notify_area,
    find_taskbar_host,
    get_taskbar_bg_color,
    is_taskbar_visible,
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
_TIP_WIDTH = 372

#: 字号与间距
#: 标签→数值间距
_GAP_LABEL_NUM = 5
_NUM_FONT_PT = 15
_LABEL_FONT_PT = 11


def _short_countdown(reset_time) -> str:
    """重置倒计时的短格式:3时 / 2天22时 / 11天。

    完整文案("还剩3小时0分")在浮窗里太长,会把卡片撑得很宽;
    倒计时只需要一眼看出量级,短格式够用。
    """
    if reset_time is None:
        return ""
    i18n = get_i18n()
    kind, payload = render.reset_countdown(reset_time)
    if kind == "soon":
        return i18n.t("daily.reset.soon")
    if kind == "hm":
        hours, minutes = payload
        if hours > 0:
            return f"{hours}{i18n.t('detail.hourShort')}"
        return f"{minutes}{i18n.t('detail.minuteShort')}"
    days, hours = payload
    day = i18n.t("detail.dayShort")
    hour = i18n.t("detail.hourShort")
    return f"{days}{day}{hours}{hour}" if hours else f"{days}{day}"


def _baseline(height: int, fm: QFontMetrics) -> int:
    # 行内基线 y:让文字在给定行高内垂直居中
    return (height - fm.height()) // 2 + fm.ascent()


def account_initials(
    instances: list[PlatformInstance],
    numbering_order: Optional[list[PlatformInstance]] = None,
) -> dict[str, str]:
    """账号短标签:平台首字母;同平台多账号加序号(M1/M2),单账号不带号(G/S/J)。

    序号按**注册顺序**给(numbering_order),不随显示位置变——
    调整顺序时主力仍是 M1、备用仍是 M2(与 macOS 版一致)。
    """
    order = numbering_order if numbering_order is not None else instances
    totals = Counter(_PLATFORM_INITIAL[i.platform_type] for i in order)
    seen: Counter = Counter()
    labels: dict[str, str] = {}
    for inst in order:
        base = _PLATFORM_INITIAL[inst.platform_type]
        seen[base] += 1
        labels[inst.id] = base if totals[base] == 1 else f"{base}{seen[base]}"
    return labels


class _TooltipWindow(QWidget):
    """悬停浮窗:每个账号一张卡,卡内指标横排。

    排版要点:
    - 数值紧跟指标名,横向不扫就能对上;
    - 每个「指标块」(名 + 值 + 倒计时)宽度固定,**所有卡共用同一套块宽**,
      所以不同账号的同序号指标纵向对齐;
    - 每行放 2 个指标块,多余的换到下一行(同一列起点),宽度不至于太夸张。
    """

    BLOCKS_PER_ROW = 2

    def __init__(self) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)
        self._cards_box = outer
        self._sig = None

    def show_rows(self, rows: list[tuple[str, str, str, str, str, str]], anchor_rect) -> None:
        """rows: [(标签色, 账号标签, 账号显示名, 指标名, 数值, 倒计时)]"""
        sig = tuple(rows)
        if sig == self._sig and self.isVisible():
            return
        self._sig = sig

        while self._cards_box.count():
            item = self._cards_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # 按账号分组
        groups: list[tuple[str, str, str, list[tuple[str, str, str]]]] = []
        for acc_color, acc, acc_name, metric, value, extra in rows:
            if groups and groups[-1][1] == acc:
                groups[-1][3].append((metric, value, extra))
            else:
                groups.append((acc_color, acc, acc_name, [(metric, value, extra)]))

        f_tag = QFont("Microsoft YaHei UI", 10, QFont.Bold)
        f_name = QFont("Microsoft YaHei UI", 9)
        f_metric = QFont("Microsoft YaHei UI", 9)
        f_value = QFont("Microsoft YaHei UI", 12, QFont.Bold)
        f_extra = QFont("Microsoft YaHei UI", 8)

        # 全局尺寸:所有卡共用,保证纵向对齐
        tag_w = max([QFontMetrics(f_tag).horizontalAdvance(a) for _, a, _, _ in groups] or [20])
        name_w = max([QFontMetrics(f_name).horizontalAdvance(n) for _, _, n, _ in groups] or [40])
        metric_w = max([QFontMetrics(f_metric).horizontalAdvance(m) for _, _, _, ms in groups for m, _, _ in ms] or [40])
        value_w = max([QFontMetrics(f_value).horizontalAdvance(v) for _, _, _, ms in groups for _, v, _ in ms] or [20])
        extra_w = max([QFontMetrics(f_extra).horizontalAdvance(e) for _, _, _, ms in groups for _, _, e in ms] or [20])

        # 每个指标块:指标名 + 数值 一行,倒计时 小字一行(省宽度)
        block_w = metric_w + 10 + value_w
        card_w = (
            PADDING * 2 + tag_w + 10 + name_w + 14
            + self.BLOCKS_PER_ROW * (block_w + 10)
        )

        for acc_color, acc, acc_name, metrics in groups:
            card = QFrame()
            card.setFixedWidth(card_w)
            card.setStyleSheet(
                "QFrame { background-color: #FFFFFF;"
                " border: 1px solid rgba(0, 0, 0, 26); border-radius: 7px; }"
            )
            grid = QGridLayout(card)
            grid.setContentsMargins(PADDING, 5, PADDING, 5)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(2)
            # 多余空间给最后一列:否则 QGridLayout 会拉伸前面的列,内容散开不靠左
            grid.setColumnStretch(2 + self.BLOCKS_PER_ROW, 1)

            tag = QLabel(acc)
            tag.setFixedWidth(tag_w)
            tag.setFont(f_tag)
            tag.setStyleSheet(f"color:{acc_color}; border: none; background: transparent;")
            grid.addWidget(tag, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)
            name = QLabel(acc_name)
            name.setFixedWidth(name_w)
            name.setFont(f_name)
            name.setStyleSheet("color:#98A1AE; border: none; background: transparent;")
            grid.addWidget(name, 0, 1, Qt.AlignLeft | Qt.AlignVCenter)

            for k, (metric, value, extra) in enumerate(metrics):
                r, c = divmod(k, self.BLOCKS_PER_ROW)
                cell = QWidget()
                cell.setFixedWidth(block_w)
                cl = QVBoxLayout(cell)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(0)
                line = QHBoxLayout()
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(6)
                m = QLabel(metric)
                m.setFixedWidth(metric_w)
                m.setFont(f_metric)
                m.setStyleSheet("color:#4A5261; border: none; background: transparent;")
                line.addWidget(m, 0, Qt.AlignLeft | Qt.AlignVCenter)
                v = QLabel(value)
                v.setFixedWidth(value_w)
                v.setFont(f_value)
                v.setStyleSheet("color:#161B22; border: none; background: transparent;")
                line.addWidget(v, 0, Qt.AlignLeft | Qt.AlignVCenter)
                cl.addLayout(line)
                if extra:
                    e = QLabel(extra)
                    e.setFixedWidth(extra_w)
                    e.setFont(f_extra)
                    e.setStyleSheet("color:#98A1AE; border: none; background: transparent;")
                    cl.addWidget(e, 0, Qt.AlignRight | Qt.AlignTop)
                grid.addWidget(cell, r, 2 + c, Qt.AlignLeft | Qt.AlignVCenter)

            self._cards_box.addWidget(card)

        self.adjustSize()
        x = anchor_rect.left() + (anchor_rect.width() - self.width()) // 2
        y = anchor_rect.top() - self.height() - 8
        screen = QGuiApplication.screenAt(QPoint(anchor_rect.left(), anchor_rect.top()))
        geo = screen.availableGeometry() if screen else None
        if geo is not None:
            x = max(geo.left() + 4, min(x, geo.right() - self.width() - 4))
            if y < geo.top() + 4:
                # 面板贴近屏幕顶:上方放不下,改放面板下方
                # (硬钳到屏幕顶会盖住面板和鼠标,想看的数字反而被浮窗挡住)
                y = min(anchor_rect.bottom() + 8, geo.bottom() - self.height() - 4)
            y = max(geo.top() + 4, y)
        self.move(x, y)
        if not self.isVisible():
            self.show()
        self.raise_()
        ensure_topmost(int(self.winId()))


class StatusOverlay(QWidget):
    """任务栏内的账号数字块组;数据/配置一律读 TrayApp,自身不取数。"""

    def __init__(self, tray):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # 面板做成**不透明**窗口 + 底色取任务栏实际颜色(每 2 秒重采样):
        #   · 不透明 → 命中按整个矩形,hover/点击稳定;
        #     (alpha 透明窗口的命中在 Qt 下不可靠,文字旁透明处会穿透给下层窗口)
        #   · 底色跟随任务栏 → 换壁纸/换主题后 2 秒内跟上,观感与半透明一致
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)  # 重绘不擦背景,避免闪烁
        self.setMouseTracking(True)
        self._tray = tray
        self._chips: list[tuple[QRect, str]] = []    # [(QRect, instance_id)]
        self._hover_index = -1
        self._embedded = False
        self._bg_color = QColor(*get_taskbar_bg_color())
        self._is_dark = self._bg_color.lightness() < 128
        self._tip = _TooltipWindow()
        # 置顶守护:任务栏与置顶应用(如 ZCode)都会盖住面板,
        # 用 500ms 轻量检测判断是否真被盖,被盖才重新置顶(避免透明窗口闪烁)
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._ensure_visible)
        self._topmost_timer.start(500)
        # 任务栏颜色重采样:Win10 任务栏是半透明材质,壁纸/主题变了颜色就变,
        # 定期重采以判断明暗(决定文字用白还是深色)
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._refresh_taskbar_theme)
        self._theme_timer.start(2000)
        # 拖拽移动(位置持久化:想放左放右都行)
        self._press_pos: Optional[QPoint] = None
        self._press_win_pos: Optional[QPoint] = None
        self._dragging = False
        # 嵌入任务栏:真正成为任务栏的元素(不是浮在上面的图案)
        self._embed_host: Optional[int] = None
        # 上次尝试嵌入的时刻(失败后退避重试)
        self._last_attach_try: float = 0.0
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

    def _refresh_taskbar_theme(self) -> None:
        """重采样任务栏颜色,判断明暗(换壁纸/换主题后自动跟上)"""
        if not is_taskbar_visible():
            # 任务栏自动隐藏/被全屏应用盖住:屏幕像素采到的不是任务栏,
            # 采了会让底色和文字色乱跳。沿用上一次的取值。
            return
        color = get_taskbar_bg_color()
        if not color:
            return
        self._bg_color = QColor(*color)
        self._is_dark = self._bg_color.lightness() < 128
        self.update()

    def _ensure_visible(self) -> None:
        """守护:嵌入模式下探测 explorer 重启;浮层模式下被盖则提回最前。

        悬停浮窗也要持续抢置顶——置顶应用(如 ZCode)铺满全屏,只在首次显示时
        提一次顶会被它重新压住,浮窗就"不出现"。
        """
        if not self._tray.config.overlay_enabled():
            return  # 用户已关掉面板:守护不能把它带回来
        if self._embed_host is not None:
            # explorer 重启后任务栏句柄会变,需要重新嵌入
            if find_taskbar_host() != self._embed_host:
                self._reattach()
            return
        if not self.isVisible():
            return
        # 嵌入模式却没挂上任务栏:attach 失败过(explorer 重启的窗口期)之后
        # 没有重试路径,面板会永远消失——这里按退避间隔重试
        if self._tray.config.overlay_embedded():
            self._retry_attach()
        # 无条件提到 topmost 最前:置顶的**活动窗口**(如 ZCode,铺满全屏)会抢走
        # 任务栏区域的鼠标消息,面板收不到 hover —— 必须始终排在它前面。
        # SetWindowPos 带 NOMOVE|NOSIZE|NOACTIVATE 不会重绘内容,不会引起闪烁。
        ensure_topmost(int(self.winId()))
        if self._tip.isVisible():
            ensure_topmost(int(self._tip.winId()))

    def _reattach(self) -> None:
        """explorer 重启后重新嵌入任务栏"""
        self._embed_host = None
        self.attach()

    def _retry_attach(self) -> None:
        """嵌入失败后按退避间隔重试(2 秒一次)

        explorer 重启有一个窗口期:旧任务栏已销毁、新的还没起来,此刻
        attach() 必然失败。没有重试的话,_embed_host 会一直是 None,
        守护逻辑再也不会尝试嵌入——面板从此消失,只能重启应用。
        """
        now = time.time()
        if now - self._last_attach_try < 2.0:
            return
        self._last_attach_try = now
        self.attach()

    def ensure_embedded_state(self) -> None:
        """面板重新显示时保证嵌入态有效

        explorer 重启后旧的任务栏句柄已失效,此时不能拿着旧句柄直接跳过嵌入。
        """
        if not self._tray.config.overlay_embedded():
            return
        if self._embed_host is not None and find_taskbar_host() != self._embed_host:
            self._embed_host = None
            detach_from_taskbar(int(self.winId()))
        if self._embed_host is None:
            self.attach()

    @staticmethod
    def _taskbar_screen():
        """任务栏所在的屏幕(多显示器时任务栏可能在副屏上)

        用主屏做位置校验的话,副屏任务栏的用户拖完面板重启就会被拽回主屏——
        面板永远出现在错误的那块屏上。
        """
        primary = QGuiApplication.primaryScreen()
        tb = taskbar_rect()
        if tb is None:
            return primary
        for screen in QGuiApplication.screens():
            geo = screen.geometry()
            if tb.left < geo.right() and tb.right > geo.left() and tb.top < geo.bottom() and tb.bottom > geo.top():
                return screen
        return primary

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
        if not self._tray.config.overlay_enabled():
            return  # 用户已关掉面板:不能借嵌入把它带回来
        host = embed_in_taskbar(int(self.winId()))
        if host is None:
            return
        self._embed_host = host
        # 嵌入后不能再用 alpha 透明(child window 不支持 layered):
        # 底色取任务栏实际颜色,视觉上与任务栏融为一体
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._bg_color = QColor(*get_taskbar_bg_color())
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
        # 脱离后按「面板开关」决定显隐:用户关掉的面板不能被 detach 带回来
        self.setVisible(self._tray.config.overlay_enabled())
        self._place()
        ensure_topmost(int(self.winId()))

    def _place(self) -> None:
        """定位:嵌入模式走 Win32(父客户区坐标),浮层模式走 Qt 屏幕坐标。

        嵌入模式下**不能**调 Qt 的 setFixedSize/move——那会把外部 SetParent 冲掉。
        """
        instances = self.overlay_instances()
        label_fm = QFontMetrics(self._label_font())
        width = PADDING * 2
        # 数值列按**最坏情况**占位:数据随时会变(1% → 100%),
        # 按当前实际值算宽会在数字变长时把末尾截掉
        worst_fm = QFontMetrics(self._num_font("100%"))
        pct_w = worst_fm.horizontalAdvance("100%")
        # 余额型数字用 K 缩写后可能比 "100%" 还长(余额上万 → "10000.0K")
        balance_w = worst_fm.horizontalAdvance("10000.0K")
        for index, inst in enumerate(instances):
            label, text = self._chip_texts(inst)
            width += label_fm.horizontalAdvance(label) + _GAP_LABEL_NUM
            metrics = self._tray._visible_metrics(inst.id)
            is_balance = bool(metrics) and metrics[0].unit == "CNY"
            reserve = max(pct_w, balance_w) if is_balance else pct_w
            # 实际文本更宽时也按实际算(余额已经到十万级)
            reserve = max(reserve, QFontMetrics(self._num_font(text)).horizontalAdvance(text))
            width += reserve
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
                    x = 48  # 靠任务栏左端(避开最左边的开始按钮)
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
                # 钳制到任务栏范围内:账号多/通知区靠左时 x 会算成负数,
                # 面板左侧被推出任务栏(画不全也点不到)
                x = max(0, min(x, max(0, tb_w - width)))
                y = max(0, (tb_h - height) // 2)
                move_in_parent(int(self.winId()), x, y, width, height)
            return

        if self.width() != width or self.height() != height:
            self.setFixedSize(width, height)

        screen = self._taskbar_screen()
        if screen is None:
            return
        sgeo = screen.geometry()
        tb = taskbar_rect()

        remembered = self._tray.config.overlay_pos()
        if remembered is not None:
            # 记住的位置:必须 x 落在(任务栏所在)屏幕内、y 落在任务栏上,否则回默认
            # (只校验 y 的话,拖到屏幕外的面板重启后永久失联;
            #  用主屏校验的话,任务栏在副屏上的用户每次重启都会被拽回主屏)
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
        if self._tray.config.show_percent() and metrics and metrics[0].unit == "%":
            text = f"{text}%"
        if self._tray.is_loading(inst.id) and not metrics:
            text = "…"
        label = account_initials(
            self.overlay_instances(), self._tray.config.registration_order()
        ).get(inst.id, "?")
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
        labels = account_initials(
            instances, self._tray.config.registration_order()
        )
        self._chips = []

        # 不透明铺满任务栏色:与任务栏严丝合缝(颜色每 2 秒重采样,跟随壁纸/主题)
        painter.fillRect(self.rect(), QColor(self._bg_color))
        # 明暗以**任务栏实际采样色**的亮度为准:注册表主题键在 Win10/Win11 名字不同,
        # 缺键时会恒判"深色",白字画在浅色任务栏上完全看不清。
        light = not self._is_dark
        # 数值恒为白(浅色任务栏用深色);标签用状态色随额度变色
        num_color = QColor("#FFFFFF") if not light else QColor("#1E1E1E")  # noqa: F841

        x = PADDING
        for index, inst in enumerate(instances):
            data = self._tray.data(inst.id)
            metrics = self._tray._visible_metrics(inst.id)
            _, text = self._chip_texts(inst)
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
                # 拖完记住位置:想放左放右都行(写盘失败时位置不记,面板停在原处)
                with self._tray._config_write():
                    self._tray.config.set_overlay_pos((self.x(), self.y()))
            elif not self._dragging:
                index = self._chip_at(event.pos())
                if index is not None:
                    instance_id = self._chips[index][1]
                    with self._tray._config_write():
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

    def _tooltip_rows(self) -> list[tuple[str, str, str, str, str, str]]:
        """浮窗内容:**所有账号**的全部指标(与鼠标停在哪个号上无关)。

        每行: (标签色, 账号标签, 指标名, 数值, 倒计时)
        """
        i18n = get_i18n()
        mode = self._tray.config.display_mode()
        rows: list[tuple[str, str, str, str, str, str]] = []
        labels = account_initials(
            self.overlay_instances(), self._tray.config.registration_order()
        )
        for inst in self.overlay_instances():
            data = self._tray.data(inst.id)
            # 全部指标(不是只显示勾选的那一两个)——用户要"放过去就看到全部数据"
            metrics = list(data.metrics) if data is not None else []
            acc = labels.get(inst.id, "?")
            color = _STATUS_QCOLOR.get(
                render.status_color(data), _STATUS_QCOLOR[render.COLOR_GRAY]
            ).name()
            if not metrics:
                rows.append((color, acc, inst.display_name, i18n.t("popover.noData"), "--", ""))
                continue
            for metric in metrics:
                key = f"metric.{metric.label}"
                title = i18n.t(key)
                if title == key:
                    title = metric.label
                # 百分比按显示模式(已用/剩余);次数/余额型走详情口径,
                # 不能当百分比渲染成裸数字(932/1000 会被显示成 "93")
                value = render.metric_value_text(metric, mode, i18n.locale)
                extra = _short_countdown(metric.reset_time)
                rows.append((color, acc, inst.display_name, title, value, extra))
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
