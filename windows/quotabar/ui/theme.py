# -*- coding: utf-8 -*-
"""Windows 主题色板:只读一次系统亮/暗偏好,不采样壁纸/任务栏像素。

任务栏面板本身是纯透明文字,不需要底色;悬停卡片与详情窗需要一个稳定的
卡片底色,按 Windows 的「应用模式」选深/浅两套,换壁纸不会影响它。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Palette:
    is_light: bool
    bg: str
    card: str
    card_border: str
    text: str
    muted: str
    value: str
    track: str
    accent: str


_DARK = Palette(
    is_light=False,
    bg="#17191D",
    card="#202329",
    card_border="#2C313A",
    text="#F2F4F7",
    muted="#98A0AC",
    # 数值用纯白,和任务栏面板的白色数字同一套语言
    value="#FFFFFF",
    track="#2E343D",
    accent="#4C8DFF",
)

_LIGHT = Palette(
    is_light=True,
    bg="#F5F6F8",
    card="#FFFFFF",
    card_border="#E1E5EA",
    text="#1B1D22",
    muted="#6B7280",
    value="#111827",
    track="#E8EBEF",
    accent="#0A64D6",
)

#: 状态色:深色卡片用亮色版,浅色卡片用深一档(保证对比度)
_STATUS_DARK = {
    "gray": "#8E8E93",
    "red": "#FF453A",
    "yellow": "#FFD60A",
    "green": "#32D74B",
}
_STATUS_LIGHT = {
    "gray": "#6E7784",
    "red": "#D32F2F",
    "yellow": "#B87700",
    "green": "#1E9E3E",
}


_cached_light: Optional[bool] = None


def is_light_theme() -> bool:
    """读 Windows 系统(任务栏)模式;读不到按深色。

    用 SystemUsesLightTheme 而不是 AppsUseLightTheme:弹出卡片紧贴任务栏,
    跟任务栏同一套深浅更协调(应用模式可能是浅色而任务栏仍是深色)。

    结果缓存:同一次运行里所有窗口必须用同一套色板,否则会出现面板深色、
    详情窗浅色这种自相矛盾。切换系统主题后重启应用即可。
    """
    global _cached_light
    if _cached_light is not None:
        return _cached_light
    if sys.platform != "win32":
        _cached_light = False
        return _cached_light
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            except OSError:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            _cached_light = bool(value)
    except OSError:
        _cached_light = False
    return _cached_light


def palette(light: bool | None = None) -> Palette:
    if light is None:
        light = is_light_theme()
    return _LIGHT if light else _DARK


def status_color(name: str, light: bool | None = None) -> str:
    if light is None:
        light = is_light_theme()
    table = _STATUS_LIGHT if light else _STATUS_DARK
    return table.get(name, table["gray"])


def apply_application_palette(app) -> None:
    """把 Qt 全局调色板切成与卡片一致的深/浅色。

    Windows 原生样式下,子控件(QLabel/QLineEdit/QMenu)会优先吃系统浅色
    palette,自定义 stylesheet 覆盖不到的地方就会露出白底。启动时统一设置,
    配合 Fusion 风格,整个应用的底色才一致。
    """
    from PyQt5.QtGui import QColor, QPalette

    p = palette()
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(p.bg))
    pal.setColor(QPalette.WindowText, QColor(p.text))
    pal.setColor(QPalette.Base, QColor(p.card))
    pal.setColor(QPalette.AlternateBase, QColor(p.bg))
    pal.setColor(QPalette.Text, QColor(p.text))
    pal.setColor(QPalette.Button, QColor(p.card))
    pal.setColor(QPalette.ButtonText, QColor(p.text))
    pal.setColor(QPalette.ToolTipBase, QColor(p.card))
    pal.setColor(QPalette.ToolTipText, QColor(p.text))
    pal.setColor(QPalette.Highlight, QColor(p.accent))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(p.muted))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(p.muted))
    app.setPalette(pal)
