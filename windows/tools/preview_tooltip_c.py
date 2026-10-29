# -*- coding: utf-8 -*-
"""渲染「每账号一张卡」的浮窗排版变体(离屏绘制)。

排版原则:所有元素按**实际文字宽度**依次排布,不用固定偏移,避免重叠。
"""

from __future__ import annotations

import os
import sys

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QImage, QPainter, QPen

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")

DATA = [
    ("J", "#1E9E3E", "基元律动", [
        ("余额", "150 CNY", "11天13小时后重置"),
    ]),
    ("M2", "#D32F2F", "MiniMax 备用", [
        ("5小时窗口", "13%", "还剩3小时42分"),
        ("周限额(150%加成)", "92%", "2天22小时后重置"),
    ]),
    ("M1", "#1E9E3E", "MiniMax 主力", [
        ("5小时窗口", "100%", "还剩3小时42分"),
        ("无限", "∞", ""),
    ]),
    ("G", "#1E9E3E", "GLM", [
        ("5小时窗口", "100%", "还剩3小时42分"),
        ("周限额", "76%", "2天22小时后重置"),
        ("MCP月度", "99", "11天后0小时后重置"),
    ]),
    ("S", "#1E9E3E", "Stepfun", [
        ("套餐余量", "95%", "20天0小时后重置"),
    ]),
]

BG = QColor("#F7F8FA")
CARD = QColor("#FFFFFF")
TEXT = QColor("#161B22")
MUTED = QColor("#4A5261")
FAINT = QColor("#98A1AE")

PAD_X = 14          # 卡片内左边距
TAG_W = 26          # 标签列宽
GAP = 8             # 元素间距


def font(size: int, bold: bool = False) -> QFont:
    f = QFont("Microsoft YaHei UI", size)
    if bold:
        f.setBold(True)
    return f


def base(w: int, h: int):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(120, 130, 145, 90), 1))
    p.setBrush(BG)
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
    return img, p


def draw_account_head(p, x: float, y: float, tag: str, color: str, name: str,
                      f_tag, f_name) -> float:
    """画账号头(标签 + 账号名),返回名字绘制后的右边界"""
    p.setFont(f_tag)
    p.setPen(QColor(color))
    p.drawText(int(x), int(y), tag)
    p.setFont(f_name)
    p.setPen(FAINT)
    p.drawText(int(x + TAG_W), int(y), name)
    return x + TAG_W + QFontMetrics(f_name).horizontalAdvance(name)


def draw_row(p, x: float, y: float, metric: str, value: str, extra: str,
             f_metric, f_val, f_extra) -> float:
    """画一行「指标名 数值 倒计时」,按实际宽度依次排,返回右边界"""
    p.setFont(f_metric)
    p.setPen(MUTED)
    p.drawText(int(x), int(y), metric)
    cx = x + QFontMetrics(f_metric).horizontalAdvance(metric) + GAP
    p.setFont(f_val)
    p.setPen(TEXT)
    p.drawText(int(cx), int(y), value)
    cx += QFontMetrics(f_val).horizontalAdvance(value) + GAP
    if extra:
        p.setFont(f_extra)
        p.setPen(FAINT)
        p.drawText(int(cx), int(y), extra)
        cx += QFontMetrics(f_extra).horizontalAdvance(extra)
    return cx


# ---------------------------------------------------------------- C1:每账号一卡,指标竖排

def render_c1() -> QImage:
    f_tag, f_name = font(11, True), font(9)
    f_metric, f_val, f_extra = font(10), font(12, True), font(9)
    row_h = 18
    # 宽度:取所有行所需最大宽度 + 边距
    max_right = 0
    for _, _, _, ms in DATA:
        for metric, value, extra in ms:
            w = PAD_X + TAG_W + QFontMetrics(f_metric).horizontalAdvance(metric) + GAP
            w += QFontMetrics(f_val).horizontalAdvance(value) + GAP
            w += QFontMetrics(f_extra).horizontalAdvance(extra)
            max_right = max(max_right, w)
    # 账号名也要放得下
    for _, _, name, _ in DATA:
        max_right = max(max_right, PAD_X + TAG_W + QFontMetrics(f_name).horizontalAdvance(name))
    w = max_right + PAD_X
    h = 12
    for _, _, _, ms in DATA:
        h += 24 + len(ms) * row_h + 10
    img, p = base(w, h)
    y = 12
    for tag, color, name, ms in DATA:
        card_h = 20 + len(ms) * row_h
        p.setPen(QPen(QColor(0, 0, 0, 20), 1))
        p.setBrush(CARD)
        p.drawRoundedRect(QRectF(8, y, w - 16, card_h), 7, 7)
        draw_account_head(p, PAD_X, y + 16, tag, color, name, f_tag, f_name)
        yy = y + 22 + row_h - 4
        for metric, value, extra in ms:
            draw_row(p, PAD_X, yy, metric, value, extra, f_metric, f_val, f_extra)
            yy += row_h
        y += card_h + 10
    p.end()
    return img


# ---------------------------------------------------------------- C2:每账号一卡,指标横排

def render_c2() -> QImage:
    f_tag, f_name = font(11, True), font(9)
    f_metric, f_val, f_extra = font(10), font(13, True), font(9)
    head_w = 0
    for _, _, name, _ in DATA:
        head_w = max(head_w, PAD_X + TAG_W + QFontMetrics(f_name).horizontalAdvance(name) + 18)
    w = head_w
    for _, _, _, ms in DATA:
        x = head_w
        for metric, value, extra in ms:
            x += QFontMetrics(f_metric).horizontalAdvance(metric) + GAP
            x += QFontMetrics(f_val).horizontalAdvance(value) + GAP
            x += QFontMetrics(f_extra).horizontalAdvance(extra) + 14
        w = max(w, x + PAD_X)
    h = 12 + len(DATA) * 52
    img, p = base(w, h)
    y = 12
    for tag, color, name, ms in DATA:
        p.setPen(QPen(QColor(0, 0, 0, 20), 1))
        p.setBrush(CARD)
        p.drawRoundedRect(QRectF(8, y, w - 16, 46), 7, 7)
        draw_account_head(p, PAD_X, y + 18, tag, color, name, f_tag, f_name)
        x = head_w
        for i, (metric, value, extra) in enumerate(ms):
            if i:
                p.setPen(QPen(QColor(0, 0, 0, 28), 1))
                p.drawLine(x - 8, y + 12, x - 8, y + 34)
            p.setFont(f_metric)
            p.setPen(MUTED)
            p.drawText(int(x), y + 18, metric)
            p.setFont(f_val)
            p.setPen(TEXT)
            p.drawText(int(x), y + 36, value)
            x += QFontMetrics(f_metric).horizontalAdvance(metric) + GAP
            x += QFontMetrics(f_val).horizontalAdvance(value) + GAP
            if extra:
                p.setFont(f_extra)
                p.setPen(FAINT)
                p.drawText(int(x), y + 36, extra)
                x += QFontMetrics(f_extra).horizontalAdvance(extra) + 14
            else:
                x += 12
        y += 52
    p.end()
    return img


# ---------------------------------------------------------------- C3:窄版,数值紧贴名字

def render_c3() -> QImage:
    f_tag, f_name = font(11, True), font(9)
    f_metric, f_val, f_extra = font(10), font(11, True), font(9)
    row_h = 17
    max_right = 0
    for _, _, name, ms in DATA:
        max_right = max(max_right, PAD_X + TAG_W + QFontMetrics(f_name).horizontalAdvance(name) + 14)
        for metric, value, extra in ms:
            w = PAD_X + TAG_W + 60 + QFontMetrics(f_metric).horizontalAdvance(metric) + GAP
            w += QFontMetrics(f_val).horizontalAdvance(value) + GAP
            w += QFontMetrics(f_extra).horizontalAdvance(extra)
            max_right = max(max_right, w)
    w = max_right + PAD_X
    h = 12
    for _, _, _, ms in DATA:
        h += 20 + len(ms) * row_h + 10
    img, p = base(w, h)
    y = 12
    for tag, color, name, ms in DATA:
        card_h = 18 + len(ms) * row_h
        p.setPen(QPen(QColor(0, 0, 0, 20), 1))
        p.setBrush(CARD)
        p.drawRoundedRect(QRectF(8, y, w - 16, card_h), 7, 7)
        draw_account_head(p, PAD_X, y + 15, tag, color, name, f_tag, f_name)
        yy = y + 20 + row_h - 5
        for metric, value, extra in ms:
            draw_row(p, PAD_X + TAG_W + 34, yy, metric, value, extra, f_metric, f_val, f_extra)
            yy += row_h
        y += card_h + 10
    p.end()
    return img


# ---------------------------------------------------------------- C4:横条 + 主指标大字

def render_c4() -> QImage:
    f_tag, f_name = font(11, True), font(9)
    f_metric, f_val, f_extra = font(9), font(19, True), font(9)
    row_h = 46
    max_right = 0
    for _, _, name, ms in DATA:
        right = PAD_X + TAG_W + QFontMetrics(f_name).horizontalAdvance(name) + 20
        for metric, value, extra in ms:
            right += QFontMetrics(f_metric).horizontalAdvance(metric) + 8
            right += QFontMetrics(f_val).horizontalAdvance(value) + 8
            right += QFontMetrics(f_extra).horizontalAdvance(extra) + 14
        max_right = max(max_right, right)
    w = max_right + PAD_X
    h = 12 + len(DATA) * (row_h + 6)
    img, p = base(w, h)
    y = 12
    for tag, color, name, ms in DATA:
        p.setPen(QPen(QColor(0, 0, 0, 20), 1))
        p.setBrush(CARD)
        p.drawRoundedRect(QRectF(8, y, w - 16, row_h), 7, 7)
        draw_account_head(p, PAD_X, y + 17, tag, color, name, f_tag, f_name)
        x = PAD_X + TAG_W + QFontMetrics(f_name).horizontalAdvance(name) + 16
        for i, (metric, value, extra) in enumerate(ms):
            if i:
                p.setPen(QPen(QColor(0, 0, 0, 25), 1))
                p.drawLine(x - 8, y + 12, x - 8, y + 34)
            p.setFont(f_metric)
            p.setPen(MUTED)
            p.drawText(int(x), y + 17, metric)
            p.setFont(f_val)
            p.setPen(TEXT)
            p.drawText(int(x), y + 38, value)
            x += QFontMetrics(f_metric).horizontalAdvance(metric) + 8
            x += QFontMetrics(f_val).horizontalAdvance(value) + 8
            if extra:
                p.setFont(f_extra)
                p.setPen(FAINT)
                p.drawText(int(x), y + 38, extra)
                x += QFontMetrics(f_extra).horizontalAdvance(extra) + 14
        y += row_h + 6
    p.end()
    return img


def compose(shots):
    imgs = [i for _, i in shots]
    pad, top = 18, 30
    cell_w = max(i.width() for i in imgs)
    cell_h = max(i.height() for i in imgs)
    out = QImage(cell_w * 2 + pad * 3, cell_h * 2 + pad * 3 + top, QImage.Format_ARGB32)
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
    app = QGuiApplication(sys.argv)
    shots = [
        ("C1  每账号一卡 · 指标竖排", render_c1()),
        ("C2  每账号一卡 · 指标横排", render_c2()),
        ("C3  每账号一卡 · 窄版", render_c3()),
        ("C4  每账号一横条 · 主指标大字", render_c4()),
    ]
    for label, img in shots:
        path = os.path.join(OUT_DIR, "card_%s.png" % label[:2].strip())
        img.save(path)
        print("saved", path)
    path = os.path.join(OUT_DIR, "card_all.png")
    compose(shots).save(path)
    print("saved", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
