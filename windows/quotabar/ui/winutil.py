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


def is_covered_by_taskbar(hwnd) -> bool:
    """面板是否被更上层的窗口压在下面。

    判据:沿 Z 序向前找第一个与本窗口矩形相交、且不是自身的可见顶层窗口。
    任务栏(Shell_TrayWnd)与置顶应用(如 ZCode)都会命中。
    只在真正被盖时才需要重新置顶——无条件 SetWindowPos 会让透明窗口闪烁。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetWindow.restype = ctypes.c_void_p
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        GW_HWNDPREV = 3

        mine = RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(mine)):
            return False

        prev = user32.GetWindow(ctypes.c_void_p(hwnd), GW_HWNDPREV)
        checked = 0
        while prev and checked < 12:
            checked += 1
            if prev != hwnd and user32.IsWindowVisible(ctypes.c_void_p(prev)):
                r = RECT()
                if user32.GetWindowRect(ctypes.c_void_p(prev), ctypes.byref(r)):
                    # 矩形相交 → 这个窗口盖在我们上面
                    if r.left < mine.right and r.right > mine.left and r.top < mine.bottom and r.bottom > mine.top:
                        return True
            prev = user32.GetWindow(ctypes.c_void_p(prev), GW_HWNDPREV)
    except Exception:
        return False
    return False
