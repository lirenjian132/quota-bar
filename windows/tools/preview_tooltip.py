# -*- coding: utf-8 -*-
"""渲染 4 种浮窗排版方案的效果图(离屏绘制,不弹窗口)。

用途:让用户直观挑选浮窗排版,而不是看 ASCII 草图。
输出:tools/preview/tooltip_<方案>.png + tooltip_all.png(2x2 对比图)
"""

from __future__ import annotations

import os
import sys

from PyQt5.QtCore import QRect, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")

# 真实数据:(账号标签, 标签色, [(指标名, 数值, 倒计时)])
DATA = [
    ("J", "#1E9E3E", [("余额", "150 CNY", "11天13小时后重置")]),
    ("M2", "#D32F2F", [
        ("5小时窗口", "13%", "还剩3小时42分"),
        ("周限额(150%加成)", "92%", "2天22小时后重置"),
    ]),
    ("M1", "#1E9E3E", [
        ("5小时窗口", "100%", "还剩3小时42分"),
        ("无限", "∞", ""),
    ]),
    ("G", "#1E9E3E", [
        ("5小时窗口", "100%", "还剩3小时42分"),
        ("周限额", "76%", "2天22小时后重置"),
        ("MCP月度", "99", "11天后0小时后重置"),
    ]),
    ("S", "#1E9E3E", [("套餐余量", "95%", "20天0小时后重置")]),
]

BG = QColor("#F7F8FA")
TEXT = QColor("#161B22")
MUTED = QColor("#4A5261")
FAINT = QColor("#98A1AE")


def font(size: int, bold: bool = False) -> QFont:
    f = QFont("Microsoft YaHei UI", size)
    if bold:
        f.setBold(True)
    return f


def card(w: int, h: int) -> QImage:
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    return img


def draw_card(p: QPainter, w: int, h: int) -> None:
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(120, 130, 145, 90), 1))
    p.setBrush(BG)
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)


# ---------------------------------------------------------------- 方案 A:紧凑行内式

def render_a() -> QImage:
    rows = sum(len(m) for _, _, m in DATA) + len(DATA)
    h = 20 + rows * 19 + 14
    img = card(430, h)
    p = QPainter(img)
    draw_card(p, 430, h)
    y = 18
    f_label = font(12, True)
    f_name = font(11)
    f_num = font(11, True)
    f_extra = font(10)
    for acc, color, metrics in DATA:
        for i, (name, value, extra) in enumerate(metrics):
            x = 16
            if i == 0:
                p.setFont(f_label)
                p.setPen(QColor(color))
                p.drawText(x, y + 12, acc)
                x += 34
            else:
                x += 34
            p.setFont(f_name)
            p.setPen(MUTED)
            p.drawText(x, y + 12, name)
            x += QFontMetrics(f_name).horizontalAdvance(name) + 8
            p.setFont(f_num)
            p.setPen(TEXT)
            p.drawText(x, y + 12, value)
            x += QFontMetrics(f_num).horizontalAdvance(value) + 6
            if extra:
                p.setFont(f_extra)
                p.setPen(FAINT)
                p.drawText(x, y + 12, "· " + extra)
            y += 19
        y += 3
    p.end()
    return img


# ---------------------------------------------------------------- 方案 B:两列紧凑(名右对齐)

def render_b() -> QImage:
    rows = sum(len(m) for _, _, m in DATA) + len(DATA)
    h = 20 + rows * 19 + 14
    img = card(430, h)
    p = QPainter(img)
    draw_card(p, 430, h)
    y = 18
    f_label = font(12, True)
    f_name = font(11)
    f_num = font(11, True)
    f_extra = font(10)
    name_x, name_w = 52, 150
    for acc, color, metrics in DATA:
        for i, (name, value, extra) in enumerate(metrics):
            p.setFont(f_label)
            p.setPen(QColor(color))
            p.drawText(16, y + 12, acc)
            p.setFont(f_name)
            p.setPen(MUTED)
            # 名字右对齐到固定列末,数值紧跟其后
            tw = QFontMetrics(f_name).horizontalAdvance(name)
            p.drawText(name_x + name_w - tw, y + 12, name)
            x = name_x + name_w + 10
            p.setFont(f_num)
            p.setPen(TEXT)
            p.drawText(x, y + 12, value)
            x += QFontMetrics(f_num).horizontalAdvance(value) + 8
            if extra:
                p.setFont(f_extra)
                p.setPen(FAINT)
                p.drawText(x, y + 12, extra)
            y += 19
        y += 3
    p.end()
    return img


# ---------------------------------------------------------------- 方案 C:卡片网格(2 列)

def render_c() -> QImage:
    cells = []
    for acc, color, metrics in DATA:
        for name, value, extra in metrics:
            cells.append((acc, color, name, value, extra))
    cols, cw, ch = 2, 218, 66
    rows = (len(cells) + cols - 1) // cols
    h = 16 + rows * (ch + 8) + 8
    img = card(cols * (cw + 10) + 12, h)
    p = QPainter(img)
    draw_card(p, cols * (cw + 10) + 12, h)
    f_label = font(11, True)
    f_name = font(10)
    f_num = font(16, True)
    f_extra = font(9)
    for idx, (acc, color, name, value, extra) in enumerate(cells):
        r, c = divmod(idx, cols)
        x = 12 + c * (cw + 10)
        y = 14 + r * (ch + 8)
        p.setPen(QPen(QColor(0, 0, 0, 18), 1))
        p.setBrush(QColor(255, 255, 255, 210))
        p.drawRoundedRect(QRectF(x, y, cw, ch), 7, 7)
        p.setFont(f_label)
        p.setPen(QColor(color))
        p.drawText(x + 9, y + 15, acc)
        p.setFont(f_name)
        p.setPen(MUTED)
        p.drawText(x + 34, y + 15, name)
        p.setFont(f_num)
        p.setPen(TEXT)
        p.drawText(x + 9, y + 42, value)
        if extra:
            p.setFont(f_extra)
            p.setPen(FAINT)
            p.drawText(x + 9, y + 57, extra)
    p.end()
    return img


# ---------------------------------------------------------------- 方案 D:紧凑行内 + 数值上色

def _value_color(pct: float) -> QColor:
    if pct < 20:
        return QColor("#D32F2F")
    if pct < 50:
        return QColor("#B87700")
    return QColor("#1E9E3E")


def render_d() -> QImage:
    rows = sum(len(m) for _, _, m in DATA) + len(DATA)
    h = 20 + rows * 19 + 14
    img = card(430, h)
    p = QPainter(img)
    draw_card(p, 430, h)
    y = 18
    f_label = font(12, True)
    f_name = font(11)
    f_num = font(11, True)
    f_extra = font(10)
    for acc, color, metrics in DATA:
        for i, (name, value, extra) in enumerate(metrics):
            x = 16
            if i == 0:
                p.setFont(f_label)
                p.setPen(QColor(color))
                p.drawText(x, y + 12, acc)
                x += 34
            else:
                x += 34
            p.setFont(f_name)
            p.setPen(MUTED)
            p.drawText(x, y + 12, name)
            x += QFontMetrics(f_name).horizontalAdvance(name) + 8
            p.setFont(f_num)
            num = value.split("%")[0]
            try:
                vc = _value_color(float(num))
            except ValueError:
                vc = TEXT
            p.setPen(vc)
            p.drawText(x, y + 12, value)
            x += QFontMetrics(f_num).horizontalAdvance(value) + 8
            if extra:
                p.setFont(f_extra)
                p.setPen(FAINT)
                p.drawText(x, y + 12, extra)
            y += 19
        y += 3
    p.end()
    return img


# ---------------------------------------------------------------- 拼图

def compose(shots: list[tuple[str, QImage]]) -> QImage:
    labels = [n for n, _ in shots]
    imgs = [i for _, i in shots]
    pad, top = 18, 30
    cell_w = max(i.width() for i in imgs)
    cell_h = max(i.height() for i in imgs)
    w = cell_w * 2 + pad * 3
    h = cell_h * 2 + pad * 3 + top
    out = QImage(w, h, QImage.Format_ARGB32)
    out.fill(QColor("#2B2E33"))
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    fl = font(13, True)
    for idx, (label, img) in enumerate(shots):
        r, c = divmod(idx, 2)
        x = pad + c * (cell_w + pad)
        y = top + pad + r * (cell_h + pad)
        p.setFont(fl)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(x, y - 8, label)
        p.drawImage(x, y, img)
    p.end()
    return out


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    from PyQt5.QtGui import QGuiApplication

    app = QGuiApplication(sys.argv)
    shots = [
        ("方案 A  紧凑行内式(数值紧跟指标名)", render_a()),
        ("方案 B  两列紧凑(指标名右对齐)", render_b()),
        ("方案 C  卡片网格 2 列", render_c()),
        ("方案 D  紧凑行内 + 数值按状态上色", render_d()),
    ]
    for label, img in shots:
        path = os.path.join(OUT_DIR, "tooltip_%s.png" % label[3])
        img.save(path)
        print("saved", path)
    allimg = compose(shots)
    path = os.path.join(OUT_DIR, "tooltip_all.png")
    allimg.save(path)
    print("saved", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
