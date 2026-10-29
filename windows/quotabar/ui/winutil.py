# -*- coding: utf-8 -*-
"""Windows 任务栏几何工具:供详情窗定位与任务栏数字面板共用的 Shell_TrayWnd 探测。"""

from __future__ import annotations

import ctypes
from typing import Optional


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


def taskbar_rect() -> Optional[RECT]:
    """任务栏(Shell_TrayWnd)矩形;拿不到返回 None"""
    try:
        hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return None
        rect = RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect
    except Exception:
        return None


def taskbar_top_edge(screen_geo) -> Optional[int]:
    """底部任务栏的上沿坐标;仅当任务栏横跨该屏幕底部时返回,否则 None。

    screen_geo 传 QRect(目标屏幕 geometry),用于判断任务栏是否属于该屏幕。
    """
    rect = taskbar_rect()
    if rect is None:
        return None
    overlaps_x = rect.left < screen_geo.right() and rect.right > screen_geo.left()
    at_bottom = rect.top > screen_geo.center().y()
    if overlaps_x and at_bottom:
        return rect.top
    return None


def ensure_topmost(hwnd) -> bool:
    """把窗口提到最前(HWND_TOPMOST)。

    必须修:Windows 任务栏(Shell_TrayWnd)本身是 topmost 窗口,普通的
    Qt.Tool + WindowStaysOnTopHint 窗口会被它压在下面,面板直接"消失"。
    这里显式重设 topmost 才能保证面板始终浮在任务栏之上可见。
    """
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
        HWND_TOPMOST = -1
        ok = user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(ok)
    except Exception:
        return False
