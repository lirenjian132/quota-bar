# -*- coding: utf-8 -*-
"""任务栏数字面板(StatusOverlay):Windows 上 Mac 菜单栏的对等物。

形态:**纯文字、无底色**——不采样任务栏颜色、不跟随壁纸,直接把
「首字母 + 数值」画在任务栏上,文字带描边保证浅色/深色壁纸都看得清。

- 面板:每个账号一段「M1 39%」,首字母随状态变色,数值白色;账号之间细竖线分隔
- 悬停:深色/浅色卡片(按 Windows 应用模式),每账号一张卡,指标 + 进度条 + 倒计时
- 左键:打开**该账号**的详情窗(不再把所有账号挤在一个窗口里)
- 右键:完整菜单

窗口本身是逐像素透明 + WM_NCHITTEST 强制命中整个矩形:透明处也能收到
hover/点击,不会出现「文字旁点不到」的问题。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from PyQt5.QtCore import QPoint, QRect, QRectF, QTimer, Qt
from PyQt5.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .. import render
from ..i18n import get_i18n
from ..models import PlatformInstance, PlatformType
from .theme import palette, status_color
from .winutil import (
    detach_from_taskbar,
    embed_in_taskbar,
    ensure_topmost,
    find_notify_area,
    find_taskbar_host,
    move_in_parent,
    taskbar_above_window,
    taskbar_rect,
)

#: 面板行高与间距
BAR_TEXT_H = 30
#: 账号之间的间距
BLOCK_GAP = 20
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

#: 面板字号
_GAP_LABEL_NUM = 6
_NUM_FONT_PT = 15
_LABEL_FONT_PT = 11

#: 悬停卡片的指标短名(完整名留给详情窗)
_SHORT_LABELS = {
    "five_hour": ("5小时", "5h"),
    "weekly_limit": ("周限额", "7d"),
    "weekly_limit_boosted": ("周限额+", "7d+"),
    "weekly_limit_unlimited": ("周∞", "7d∞"),
    "mcp_monthly": ("MCP", "MCP"),
    "credits": ("Credits", "Credits"),
    "balance": ("余额", "Balance"),
}


def _short_metric_label(label: str) -> str:
    zh = get_i18n().locale.startswith("zh")
    pair = _SHORT_LABELS.get(label)
    if pair is None:
        return label
    return pair[0] if zh else pair[1]


def _short_countdown(reset_time) -> str:
    """重置倒计时的短格式:3时 / 2天22时 / 11天。"""
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
    return (height - fm.height()) // 2 + fm.ascent()


def account_initials(
    instances: list[PlatformInstance],
    numbering_order: Optional[list[PlatformInstance]] = None,
) -> dict[str, str]:
    """账号短标签:平台首字母;同平台多账号加序号(M1/M2),单账号不带号(G/S/J)。"""
    order = numbering_order if numbering_order is not None else instances
    totals = Counter(_PLATFORM_INITIAL[i.platform_type] for i in order)
    seen: Counter = Counter()
    labels: dict[str, str] = {}
    for inst in order:
        base = _PLATFORM_INITIAL[inst.platform_type]
        seen[base] += 1
        labels[inst.id] = base if totals[base] == 1 else f"{base}{seen[base]}"
    return labels


def _error_text(error, platform: PlatformType, i18n) -> str:
    """错误文案:与详情窗同一套分派,但压短一点给卡片用"""
    from ..models import ErrorKind

    if error.kind == ErrorKind.UNAUTHORIZED:
        return i18n.t(
            "error.unauthorized.session" if platform.cookie_based() else "error.unauthorized.key"
        )
    translated = i18n.t(error.message)
    if translated != error.message:
        return translated
    key = {
        ErrorKind.NOT_CONFIGURED: "error.notConfigured",
        ErrorKind.INVALID_RESPONSE: "error.invalidResponse",
        ErrorKind.NETWORK: "error.networkError",
        ErrorKind.DECODING: "error.decodingError",
        ErrorKind.API: "error.apiError",
    }.get(error.kind)
    if key is None:
        return error.message
    if key in ("error.networkError", "error.decodingError", "error.apiError"):
        return i18n.t(key, str(error.message)[:80])
    return i18n.t(key)


# ---------------------------------------------------------------- 悬停卡片


@dataclass
class TipMetric:
    label: str
    value: str
    countdown: str
    ratio: Optional[float]
    color: str


@dataclass
class TipAccount:
    initial: str
    name: str
    color: str
    metrics: list[TipMetric] = field(default_factory=list)
    error: str = ""
    stale: bool = False


def _draw_outlined_text(painter: QPainter, x: int, baseline: int, text: str,
                        fill: QColor, outline: QColor, font: QFont) -> None:
    """文字描边:先画一圈深色再画填充色,浅色/深色任务栏都能看清"""
    painter.setFont(font)
    painter.setPen(outline)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1)):
        painter.drawText(QPoint(x + dx, baseline + dy), text)
    painter.setPen(fill)
    painter.drawText(QPoint(x, baseline), text)


class _TooltipWindow(QWidget):
    """悬停卡片:每账号一张卡,指标横排 + 进度条 + 倒计时。

    自绘而不是拼 QLabel:列宽能全局对齐,也不会出现每个格子都带边框的观感。
    """

    SLOTS_PER_ROW = 2
    SLOT_GAP = 12
    ROW_H = 28
    CARD_PAD = 8

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # 卡片必须能接鼠标:鼠标从面板移向卡片(读详情的必然动作)会先离开面板,
        # 若卡片透明传鼠标,它永远接不到进入事件、停不下"离开面板 250ms 收起",
        # 结果是鼠标一靠近卡片它就消失
        self._accounts: list[TipAccount] = []
        self._sig = None
        self._geom = None
        # 由 StatusOverlay 注入的悬停回调:进入卡片停收起定时器,离开再计时
        self.on_enter = None
        self.on_leave = None

    def enterEvent(self, event) -> None:  # noqa: N802
        if callable(self.on_enter):
            self.on_enter()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if callable(self.on_leave):
            self.on_leave()
        super().leaveEvent(event)

    # ------------------------------------------------------------ 对外

    def show_accounts(self, accounts: list[TipAccount], anchor_rect: QRect) -> None:
        if not accounts:
            self.hide()
            return
        sig = tuple(
            (a.initial, a.name, a.color, a.error, a.stale,
             tuple((m.label, m.value, m.countdown, m.ratio) for m in a.metrics))
            for a in accounts
        )
        if sig == self._sig and self.isVisible():
            self._move_above(anchor_rect)
            self.raise_()
            ensure_topmost(int(self.winId()))
            return
        self._sig = sig
        self._accounts = accounts
        self._measure()
        self._move_above(anchor_rect)
        if not self.isVisible():
            self.show()
        self.raise_()
        ensure_topmost(int(self.winId()))

    # ------------------------------------------------------------ 尺寸

    def _fonts(self):
        return {
            "badge": QFont("Microsoft YaHei UI", 10, QFont.Bold),
            "name": QFont("Microsoft YaHei UI", 9, QFont.Bold),
            "label": QFont("Microsoft YaHei UI", 8),
            "value": QFont("Microsoft YaHei UI", 11, QFont.Bold),
            "count": QFont("Microsoft YaHei UI", 8),
        }

    def _measure(self) -> None:
        fonts = self._fonts()
        fm = {key: QFontMetrics(font) for key, font in fonts.items()}
        name_w = max(
            [fm["name"].horizontalAdvance(a.name) for a in self._accounts] or [40]
        )
        label_w = max(
            [fm["label"].horizontalAdvance(m.label)
             for a in self._accounts for m in a.metrics] or [36]
        )
        value_w = max(
            [fm["value"].horizontalAdvance(m.value)
             for a in self._accounts for m in a.metrics] or [36]
        )
        count_w = max(
            [fm["count"].horizontalAdvance(m.countdown)
             for a in self._accounts for m in a.metrics] or [24]
        )
        slot_w = label_w + 6 + value_w + 8 + count_w
        metrics_w = self.SLOTS_PER_ROW * slot_w + (self.SLOTS_PER_ROW - 1) * self.SLOT_GAP
        content_w = (
            self.CARD_PAD * 2 + 24 + 8 + name_w + 14 + metrics_w
        )
        content_w = max(content_w, 380)
        # 超长账号名/数值可能把卡片撑得比屏幕还宽:按可用屏宽封顶(超宽部分被裁掉)
        screen = QGuiApplication.primaryScreen()
        max_w = (screen.availableGeometry().width() - 16) if screen else 1600
        content_w = min(content_w, max_w)

        cards: list[tuple[int, int]] = []  # (y, height)
        y = 8
        for acc in self._accounts:
            rows = 1
            if acc.metrics:
                rows = math.ceil(len(acc.metrics) / self.SLOTS_PER_ROW)
            card_h = self.CARD_PAD + max(24, rows * self.ROW_H) + self.CARD_PAD
            cards.append((y, card_h))
            y += card_h + 6
        total_h = y + 2

        self._geom = {
            "fonts": fonts,
            "fm": fm,
            "name_w": name_w,
            "label_w": label_w,
            "value_w": value_w,
            "count_w": count_w,
            "slot_w": slot_w,
            "metrics_w": metrics_w,
            "cards": cards,
            "w": int(content_w),
            "h": int(total_h),
        }
        self.setFixedSize(int(content_w), int(total_h))

    def _move_above(self, anchor_rect: QRect) -> None:
        screen = QGuiApplication.screenAt(
            QPoint(anchor_rect.left(), anchor_rect.top())
        )
        geo = screen.availableGeometry() if screen else None
        x = anchor_rect.left() + (anchor_rect.width() - self.width()) // 2
        y = anchor_rect.top() - self.height() - 8
        if geo is not None:
            x = max(geo.left() + 4, min(x, geo.right() - self.width() - 4))
            if y < geo.top() + 4:
                y = min(anchor_rect.bottom() + 8, geo.bottom() - self.height() - 4)
            y = max(geo.top() + 4, y)
        self.move(x, y)

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._geom:
            return
        pal = palette()
        g = self._geom
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        w, h = self.width(), self.height()

        # 卡片阴影 + 底板
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 70))
        painter.drawRoundedRect(QRectF(1, 2, w - 2, h - 2), 12, 12)
        painter.setBrush(QColor(pal.bg))
        painter.setPen(QPen(QColor(pal.card_border), 1))
        painter.drawRoundedRect(QRectF(0, 0, w - 1, h - 3), 12, 12)

        fonts = g["fonts"]
        fm = g["fm"]
        for acc, (cy, card_h) in zip(self._accounts, g["cards"]):
            card = QRectF(8, cy, w - 16, card_h)
            painter.setPen(QPen(QColor(pal.card_border), 1))
            painter.setBrush(QColor(pal.card))
            painter.drawRoundedRect(card, 8, 8)

            # 账号徽块
            badge = QRectF(card.left() + self.CARD_PAD, card.top() + 7, 24, 24)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(acc.color))
            painter.drawRoundedRect(badge, 6, 6)
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(fonts["badge"])
            painter.drawText(badge, int(Qt.AlignCenter), acc.initial)

            # 账号名
            painter.setPen(QColor(pal.text))
            painter.setFont(fonts["name"])
            painter.drawText(
                QRectF(badge.right() + 8, card.top() + 8, g["name_w"], 20),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                acc.name,
            )

            metrics_x = badge.right() + 8 + g["name_w"] + 14
            if acc.stale:
                # 有旧数据但最近一次刷新失败:名字后点一个小橙点,不抢指标区
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#FF9500"))
                painter.drawEllipse(
                    QRectF(badge.right() + 10 + g["name_w"], card.top() + 16, 6, 6)
                )
            if acc.metrics:
                for index, metric in enumerate(acc.metrics):
                    row, col = divmod(index, self.SLOTS_PER_ROW)
                    x = metrics_x + col * (g["slot_w"] + self.SLOT_GAP)
                    y = card.top() + 7 + row * self.ROW_H

                    painter.setFont(fonts["label"])
                    painter.setPen(QColor(pal.muted))
                    painter.drawText(
                        QRectF(x, y, g["label_w"], 16),
                        int(Qt.AlignLeft | Qt.AlignVCenter),
                        metric.label,
                    )
                    painter.setFont(fonts["value"])
                    painter.setPen(QColor(pal.value))
                    painter.drawText(
                        QRectF(x + g["label_w"] + 6, y - 3, g["value_w"], 20),
                        int(Qt.AlignLeft | Qt.AlignVCenter),
                        metric.value,
                    )
                    painter.setFont(fonts["count"])
                    painter.setPen(QColor(pal.muted))
                    painter.drawText(
                        QRectF(
                            x + g["label_w"] + 6 + g["value_w"] + 8,
                            y + 1,
                            g["count_w"],
                            14,
                        ),
                        int(Qt.AlignLeft | Qt.AlignVCenter),
                        metric.countdown,
                    )
                    if metric.ratio is not None:
                        bar_w = g["label_w"] + 6 + g["value_w"]
                        bar = QRectF(x, y + 19, bar_w, 3)
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(QColor(pal.track))
                        painter.drawRoundedRect(bar, 1.5, 1.5)
                        ratio = max(0.0, min(1.0, metric.ratio))
                        fill_w = max(2.0, bar_w * ratio)
                        painter.setBrush(QColor(metric.color))
                        painter.drawRoundedRect(
                            QRectF(x, y + 19, fill_w, 3), 1.5, 1.5
                        )
            else:
                painter.setFont(fonts["label"])
                painter.setPen(QColor(acc.color if acc.error else pal.muted))
                painter.drawText(
                    QRectF(metrics_x, card.top() + 8, card.right() - metrics_x - 8, 20),
                    int(Qt.AlignLeft | Qt.AlignVCenter),
                    acc.error or "",
                )

        painter.end()


# ---------------------------------------------------------------- 面板


class StatusOverlay(QWidget):
    """任务栏内的账号数字段;数据/配置一律读 TrayApp,自身不取数。"""

    def __init__(self, tray):
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint,
        )
        # 纯文字面板:逐像素透明,不画底色。鼠标命中用 WM_NCHITTEST 兜住透明区域。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._tray = tray
        self._chips: list[tuple[QRect, str]] = []
        self._hover_index = -1
        self._embedded = False
        self._embed_host: Optional[int] = None
        self._last_attach_try: float = 0.0
        self._tip = _TooltipWindow()
        self._tip.on_enter = self._tip_entered
        self._tip.on_leave = self._tip_left

        # 守护定时器:只负责嵌入模式下探测 explorer 重启;浮层模式不在这里
        # 周期性抢 Z 序(那会让面板/任务栏区域闪)。置顶在 showEvent 里做一次。
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._ensure_visible)
        self._topmost_timer.start(500)

        # hover 延迟:进入 120ms 才弹,离开 250ms 才收,避免鼠标掠过时闪
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._show_tip)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._tip.hide)

        # 拖拽移动(位置持久化:想放左放右都行)
        self._press_pos: Optional[QPoint] = None
        self._press_win_pos: Optional[QPoint] = None
        self._dragging = False
        self.refresh()

    # ------------------------------------------------------------ 窗口命中

    def nativeEvent(self, eventType, message):  # noqa: N802
        """透明窗口也要能收到 hover/点击。

        Qt 的 WA_TranslucentBackground 在 Windows 上按像素 alpha 命中;文字
        旁边全透明,鼠标会直接穿透给下层窗口。这里把整个矩形声明成 HTCLIENT,
        并阻止点击抢焦点(MA_NOACTIVATE)。
        """
        if sys.platform == "win32":
            try:
                msg = wt.MSG.from_address(int(message))
                if msg.message == 0x0084:  # WM_NCHITTEST
                    return True, 1  # HTCLIENT
                if msg.message == 0x0021:  # WM_MOUSEACTIVATE
                    return True, 3  # MA_NOACTIVATE
            except (TypeError, ValueError, OSError):
                pass
        return super().nativeEvent(eventType, message)

    # ------------------------------------------------------------ 守护

    def _ensure_visible(self) -> None:
        """守护:嵌入模式下探测 explorer 重启;浮层模式只在被任务栏压住时重新置顶。"""
        if not self._tray.config.overlay_enabled():
            return  # 用户已关掉面板:守护不能把它带回来
        if self._embed_host is not None:
            if find_taskbar_host() != self._embed_host:
                self._reattach()
            return
        if not self.isVisible():
            return
        if self._tray.config.overlay_embedded():
            self._retry_attach()
            return
        # 浮层模式:关闭其它置顶窗口(详情窗/对话框)后 Windows 会重排 topmost 层,
        # 任务栏可能跑到面板上面 → 面板看起来"消失"。这里只做 Z 序比较,
        # 真被压住才 SetWindowPos:正常状态零操作,不会引入周期性闪烁。
        self.raise_above_taskbar()

    def raise_above_taskbar(self) -> None:
        """任务栏压到面板上面时把面板重新置顶(纯条件触发,幂等)"""
        if self._embed_host is not None or not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
        except (TypeError, RuntimeError):
            return
        if taskbar_above_window(hwnd):
            ensure_topmost(hwnd)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._embed_host is None:
            # show 之后再提一次:保证面板在任务栏(topmost)之上
            QTimer.singleShot(0, lambda: ensure_topmost(int(self.winId())))

    def _reattach(self) -> None:
        self._embed_host = None
        self.attach()

    def _retry_attach(self) -> None:
        now = time.time()
        if now - self._last_attach_try < 2.0:
            return
        self._last_attach_try = now
        self.attach()

    def ensure_embedded_state(self) -> None:
        if not self._tray.config.overlay_embedded():
            return
        if self._embed_host is not None and find_taskbar_host() != self._embed_host:
            self._embed_host = None
            detach_from_taskbar(int(self.winId()))
        if self._embed_host is None:
            self.attach()

    # ------------------------------------------------------------ 嵌入(兼容旧配置)

    def attach(self) -> None:
        """尝试嵌入任务栏(旧实验形态;新面板默认浮层透明文字)"""
        if self._embed_host is not None:
            return
        if not self._tray.config.overlay_embedded():
            return
        if not self._tray.config.overlay_enabled():
            return
        host = embed_in_taskbar(int(self.winId()))
        if host is None:
            return
        self._embed_host = host
        self._embedded = True
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._place()

    def detach(self) -> None:
        if self._embed_host is None:
            return
        self._embed_host = None
        self._embedded = False
        detach_from_taskbar(int(self.winId()))
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setVisible(self._tray.config.overlay_enabled())
        self._place()
        ensure_topmost(int(self.winId()))

    # ------------------------------------------------------------ 定位

    @staticmethod
    def _taskbar_screen():
        primary = QGuiApplication.primaryScreen()
        tb = taskbar_rect()
        if tb is None:
            return primary
        for screen in QGuiApplication.screens():
            geo = screen.geometry()
            if (
                tb.left < geo.right()
                and tb.right > geo.left()
                and tb.top < geo.bottom()
                and tb.bottom > geo.top()
            ):
                return screen
        return primary

    def _place(self) -> None:
        instances = self.overlay_instances()
        label_fm = QFontMetrics(self._label_font())
        width = PADDING * 2
        worst_fm = QFontMetrics(self._num_font("100%"))
        pct_w = worst_fm.horizontalAdvance("100%")
        balance_w = worst_fm.horizontalAdvance("10000.0K")
        for index, inst in enumerate(instances):
            label, text = self._chip_texts(inst)
            width += label_fm.horizontalAdvance(label) + _GAP_LABEL_NUM
            metrics = self._tray._visible_metrics(inst.id)
            is_balance = bool(metrics) and metrics[0].unit == "CNY"
            reserve = max(pct_w, balance_w) if is_balance else pct_w
            reserve = max(reserve, QFontMetrics(self._num_font(text)).horizontalAdvance(text))
            width += reserve
            if index < len(instances) - 1:
                width += BLOCK_GAP
        width = max(width, 60)
        height = BAR_TEXT_H

        if self._embed_host is not None:
            notify = find_notify_area(self._embed_host)
            tb = taskbar_rect()
            if tb is not None:
                tb_w = tb.right - tb.left
                tb_h = tb.bottom - tb.top
                anchor = self._tray.config.overlay_anchor()
                x = tb_w - width - _TRAY_RESERVE
                if anchor == "left":
                    x = 48
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
            x, y = remembered
            x_ok = sgeo.left() <= x <= sgeo.right() - 40
            y_ok = tb is not None and tb.top - 6 <= y <= tb.bottom + 6
            if x_ok and y_ok:
                self.move(x, y)
                return

        anchor = self._tray.config.overlay_anchor()
        if tb is not None:
            if tb.bottom - tb.top > tb.right - tb.left:
                # 左/右竖排任务栏(Win10 侧排):贴任务栏内侧竖直居中。
                # 横排公式对竖排会把面板挂到屏幕中部、x 算出负值贴到屏幕左缘
                y = tb.top + max(0, (tb.bottom - tb.top - self.height()) // 2)
                x = (tb.right - self.width() - 8) if anchor == "right" else (tb.left + 8)
            else:
                y = tb.top + max(0, (tb.bottom - tb.top - self.height()) // 2)
                if anchor == "left":
                    x = tb.left + 8
                else:
                    x = tb.right - _TRAY_RESERVE - self.width() - 12
        else:
            y = sgeo.bottom() - self.height() - 4
            x = sgeo.right() - _TRAY_RESERVE - self.width() - 12
        self.move(max(sgeo.left(), x), max(sgeo.top(), y))

    # ------------------------------------------------------------ 数据

    def refresh(self) -> None:
        self._hover_index = -1
        self._place()
        self.update()
        if self._tip.isVisible():
            self._tip.show_accounts(self._tooltip_accounts(), self._panel_screen_rect())

    def overlay_instances(self) -> list[PlatformInstance]:
        return [it for it in self._tray.config.instances() if self._tray.config.is_enabled(it.id)]

    def _chip_texts(self, inst: PlatformInstance) -> tuple[str, str]:
        data = self._tray.data(inst.id)
        metrics = self._tray._visible_metrics(inst.id)
        text = render.tray_text(metrics[0], self._tray.config.display_mode()) if metrics else "--"
        # tray_text 对 inf/NaN 返回 "--",别拼出 "--%"
        if self._tray.config.show_percent() and metrics and metrics[0].unit == "%" and text != "--":
            text = f"{text}%"
        if self._tray.is_loading(inst.id) and not metrics:
            text = "…"
        label = account_initials(
            self.overlay_instances(), self._tray.config.registration_order()
        ).get(inst.id, "?")
        return label, text

    def _chip_color_name(self, inst: PlatformInstance) -> str:
        data = self._tray.data(inst.id)
        if data is None and self._tray.error(inst.id) is not None:
            return render.COLOR_RED
        # 跟随勾选的可见指标判色(与状态栏/菜单同一口径)
        return render.status_color(data, self._tray._visible_metrics(inst.id))

    @staticmethod
    def _num_font(text: str) -> QFont:
        font = QFont("Segoe UI", _NUM_FONT_PT)
        font.setBold(True)
        return font

    @staticmethod
    def _label_font() -> QFont:
        font = QFont("Segoe UI", _LABEL_FONT_PT)
        font.setWeight(QFont.DemiBold)
        return font

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        if self._embedded:
            # 嵌入是旧实验形态(child window 不支持逐像素透明):给个中性深底
            painter.fillRect(self.rect(), QColor("#202020"))

        instances = self.overlay_instances()
        labels = account_initials(instances, self._tray.config.registration_order())
        self._chips = []
        x = PADDING
        for index, inst in enumerate(instances):
            _, text = self._chip_texts(inst)
            label = labels.get(inst.id, "?")
            color = status_color(self._chip_color_name(inst), light=False)
            label_font = self._label_font()
            num_font = self._num_font(text)
            lfm = QFontMetrics(label_font)
            nfm = QFontMetrics(num_font)
            label_w = lfm.horizontalAdvance(label)
            num_w = nfm.horizontalAdvance(text)
            chip_w = label_w + _GAP_LABEL_NUM + num_w
            self._chips.append((QRect(x, 0, chip_w, self.height()), inst.id))

            _draw_outlined_text(
                painter, x, _baseline(self.height(), lfm), label,
                QColor(color), QColor(0, 0, 0, 170), label_font,
            )
            x += label_w + _GAP_LABEL_NUM
            _draw_outlined_text(
                painter, x, _baseline(self.height(), nfm), text,
                QColor("#FFFFFF"), QColor(0, 0, 0, 190), num_font,
            )
            x += num_w

            if index < len(instances) - 1:
                cx = x + BLOCK_GAP // 2
                painter.setPen(QPen(QColor(0, 0, 0, 110), 1))
                painter.drawLine(cx + 1, 8, cx + 1, self.height() - 8)
                painter.setPen(QPen(QColor(255, 255, 255, 110), 1))
                painter.drawLine(cx, 8, cx, self.height() - 8)
                x += BLOCK_GAP
        painter.end()

    # ------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            self._press_pos = None
            self._press_win_pos = None
            self._dragging = False
        if event.button() == Qt.LeftButton:
            # 嵌入模式同样记录起点:不记的话 mouseReleaseEvent 的左键分支整段跳过,
            # 点账号块开不了详情窗、拖拽也不可用
            self._press_pos = event.globalPos()
            self._press_win_pos = self.pos()
            self._dragging = False
        elif event.button() == Qt.RightButton:
            self._tray._show_menu()

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None and not (event.buttons() & Qt.LeftButton):
            self._press_pos = None
            self._press_win_pos = None
            self._dragging = False
        if self._press_pos is not None:
            delta = event.globalPos() - self._press_pos
            if self._dragging or delta.manhattanLength() > 5:
                self._dragging = True
                self.move(self._press_win_pos + delta)
                self._tip.hide()
                self._show_timer.stop()  # 悬停 120ms 内开始拖拽时,卡片不能在拖拽中弹出
                self._hide_timer.stop()
                return

        index = self._chip_at(event.pos())
        if index != self._hover_index:
            self._hover_index = index
            self.setCursor(Qt.PointingHandCursor if index is not None else Qt.ArrowCursor)

        inside = self.rect().contains(event.pos())
        if inside:
            self._hide_timer.stop()
            if not self._tip.isVisible() and not self._show_timer.isActive():
                self._show_timer.start(120)
        else:
            self._show_timer.stop()
            if self._tip.isVisible() and not self._hide_timer.isActive():
                self._hide_timer.start(250)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            moved = (event.globalPos() - self._press_pos).manhattanLength()
            if self._dragging and moved > 5:
                with self._tray._config_write():
                    self._tray.config.set_overlay_pos((self.x(), self.y()))
            elif not self._dragging:
                index = self._chip_at(event.pos())
                if index is not None:
                    instance_id = self._chips[index][1]
                    self._tray.open_detail_for(instance_id)
                    self.update()
        self._press_pos = None
        self._press_win_pos = None
        self._dragging = False

    def _panel_screen_rect(self) -> QRect:
        origin = self.mapToGlobal(QPoint(0, 0))
        return QRect(origin.x(), origin.y(), self.width(), self.height())

    def _show_tip(self) -> None:
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            return
        self._tip.show_accounts(self._tooltip_accounts(), self._panel_screen_rect())

    def _tooltip_accounts(self) -> list[TipAccount]:
        """悬停内容:所有账号的全部指标;无数据/过期时直接写原因。"""
        i18n = get_i18n()
        mode = self._tray.config.display_mode()
        labels = account_initials(
            self.overlay_instances(), self._tray.config.registration_order()
        )
        accounts: list[TipAccount] = []
        for inst in self.overlay_instances():
            data = self._tray.data(inst.id)
            error = self._tray.error(inst.id)
            # 与面板 chip / 菜单同一口径:按用户勾选的可见指标判色
            color_name = render.status_color(data, self._tray._visible_metrics(inst.id))
            if data is None and error is not None:
                color_name = render.COLOR_RED
            color = status_color(color_name)
            metrics: list[TipMetric] = []
            if data is not None:
                for metric in data.metrics:
                    metrics.append(
                        TipMetric(
                            label=_short_metric_label(metric.label),
                            value=render.metric_value_text(metric, mode, i18n.locale),
                            countdown=_short_countdown(metric.reset_time),
                            ratio=render.metric_ratio(metric),
                            color=color,
                        )
                    )
            error_text = ""
            if not metrics:
                if error is not None:
                    error_text = _error_text(error, inst.platform_type, i18n)
                elif self._tray.is_loading(inst.id):
                    error_text = "…"
                else:
                    error_text = i18n.t("popover.noData")
            accounts.append(
                TipAccount(
                    initial=labels.get(inst.id, "?"),
                    name=inst.display_name,
                    color=color,
                    metrics=metrics,
                    error=error_text,
                    stale=bool(error is not None and metrics),
                )
            )
        return accounts

    def hide_tooltip(self) -> None:
        """立即收起悬停卡片(点账号开详情时用,避免盖住详情窗)"""
        self._show_timer.stop()
        self._hide_timer.stop()
        self._tip.hide()

    def _tip_entered(self) -> None:
        """鼠标进入卡片:停止「离开面板即收起」的计时"""
        self._hide_timer.stop()

    def _tip_left(self) -> None:
        """鼠标离开卡片:延迟收起(与离开面板同一节奏)"""
        if self._tip.isVisible():
            self._hide_timer.start(250)

    def leaveEvent(self, event) -> None:
        self._show_timer.stop()
        if self._tip.isVisible():
            self._hide_timer.start(250)
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        self._tip.hide()
        super().hideEvent(event)

    def _chip_at(self, pos) -> Optional[int]:
        for index, (rect, _) in enumerate(self._chips):
            if rect.contains(pos):
                return index
        return None
