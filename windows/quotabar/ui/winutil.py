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


def get_taskbar_bg_color(default=(74, 79, 92)) -> tuple[int, int, int]:
    """取任务栏实际显示色。

    注意:Win11 任务栏是半透明亚克力材质,取任务栏自己的 DC 只会拿到黑色
    (未合成的透明像素),必须用**屏幕 DC** 取合成后的真实颜色——那才是用户看到的。
    在任务栏空白区(避开左右两端图标)多点采样取众数。
    """
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = taskbar_rect()
        if rect is None:
            return default
        hdc = user32.GetDC(None)  # 屏幕 DC:含亚克力合成后的最终颜色
        if not hdc:
            return default
        y = (rect.top + rect.bottom) // 2
        width = rect.right - rect.left
        samples: dict[tuple[int, int, int], int] = {}
        for i in range(16):
            # 任务栏中段偏右的空白区(用户的固定图标集中在左段)
            x = rect.left + int(width * (0.52 + 0.012 * i))
            color = gdi32.GetPixel(hdc, x, y)
            if color == 0xFFFFFFFF:
                continue
            rgb = (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF)
            samples[rgb] = samples.get(rgb, 0) + 1
        user32.ReleaseDC(None, hdc)
        if not samples:
            return default
        return max(samples.items(), key=lambda kv: kv[1])[0]
    except Exception:
        return default


def set_window_region(hwnd, rects: list[tuple[int, int, int, int]], radius: int = 12) -> bool:
    """用圆角矩形区域裁剪窗口。

    嵌入任务栏的窗口不能靠 alpha 透明(child window 不支持 layered),
    但可以把窗口区域挖成几个圆角小块——镂空处直接露出任务栏背景,
    视觉上就是几个独立的小图标块,而不是一块异色矩形。
    """
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        gdi32.CreateRectRgn.restype = ctypes.c_void_p
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi32.CombineRgn.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]

        RGN_OR = 2
        region = gdi32.CreateRectRgn(0, 0, 0, 0)
        for x, y, w, h in rects:
            piece = gdi32.CreateRoundRectRgn(x, y, x + w, y + h, radius, radius)
            merged = gdi32.CombineRgn(region, piece, RGN_OR)
            gdi32.DeleteObject(region)
            gdi32.DeleteObject(piece)
            region = merged
        ok = user32.SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(region), True)
        # SetWindowRgn 成功后区域归系统所有,不能再删
        return bool(ok)
    except Exception:
        return False


def embed_in_taskbar(hwnd) -> Optional[int]:
    """把窗口嵌入任务栏,成为任务栏自己的元素。

    配方照抄 TrafficMonitor(TaskBarDlg.cpp:1017)与 MSDN 的硬性要求:
    ① 先隐藏 —— 改样式前必须隐藏;
    ② 清掉 WS_POPUP/WS_CAPTION/WS_THICKFRAME 等,置 WS_CHILD;
       **只改 WS_POPUP/WS_CHILD 是不够的**——PyQt 顶层窗口实际是
       WS_OVERLAPPEDWINDOW,不彻底改样式就"画得出来但不参与命中测试",
       鼠标消息全被任务栏自己的子窗口吃掉(症状:视觉嵌入成功,鼠标全失效);
    ③ 再 SetParent(顺序不能反);
    ④ SetWindowPos 带 SWP_FRAMECHANGED 让新样式生效。
    """
    try:
        user32 = ctypes.windll.user32
        user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.SetParent.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool

        taskbar = user32.FindWindowW("Shell_TrayWnd", None)
        if not taskbar:
            return None
        GWL_STYLE, GWL_EXSTYLE = -16, -20
        WS_POPUP = 0x80000000
        WS_CHILD = 0x40000000
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        WS_SYSMENU = 0x00080000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_TRANSPARENT = 0x00000020
        SW_HIDE = 0
        SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0020, 0x0040

        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)

        style = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE) & 0xFFFFFFFF
        style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        style |= WS_CHILD
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE, style)

        ex = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE) & 0xFFFFFFFF
        ex |= WS_EX_TOOLWINDOW      # 不进 Alt+Tab / 任务栏按钮
        ex &= ~WS_EX_TRANSPARENT    # 有这个就全穿透,鼠标永远到不了
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)

        user32.SetParent(ctypes.c_void_p(hwnd), ctypes.c_void_p(taskbar))
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
        )
        return taskbar
    except Exception:
        return None


def detach_from_taskbar(hwnd) -> bool:
    """从任务栏脱离,恢复成普通顶层置顶窗口(嵌入失败时的降级路径)。"""
    try:
        user32 = ctypes.windll.user32
        user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        GWL_STYLE, GWL_EXSTYLE = -16, -20
        WS_CHILD = 0x40000000
        WS_POPUP = 0x80000000
        WS_EX_TOOLWINDOW = 0x00000080
        SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0020, 0x0040

        user32.SetParent(ctypes.c_void_p(hwnd), None)
        style = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE) & 0xFFFFFFFF
        style &= ~WS_CHILD
        style |= WS_POPUP
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE, style)
        ex = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE) & 0xFFFFFFFF
        ex &= ~WS_EX_TOOLWINDOW
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
        )
        return True
    except Exception:
        return False


def find_notify_area(host_hwnd) -> Optional[int]:
    """任务栏通知区(TrayNotifyWnd)窗口——Win11 上内容都排到它右边。"""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowExW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowExW(
            ctypes.c_void_p(host_hwnd), None, "TrayNotifyWnd", None
        )
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def find_taskbar_host() -> Optional[int]:
    """当前任务栏窗口句柄(用于检测 explorer 重启后句柄变化)。"""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def move_in_parent(hwnd, x: int, y: int, width: int, height: int) -> bool:
    """在父窗口(任务栏)客户区坐标系里定位窗口。"""
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        HWND_TOP = 0
        SWP_NOACTIVATE = 0x0010
        ok = user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOP),
            x, y, width, height, SWP_NOACTIVATE,
        )
        return bool(ok)
    except Exception:
        return False


def ensure_topmost(hwnd) -> bool:
    """把窗口提到 Z 序最前。

    两件事都要做:
    1. 任务栏(Shell_TrayWnd)本身是 topmost 窗口,普通的 Qt.Tool + WindowStaysOnTopHint
       窗口会被它压在下面,面板直接"消失"——所以必须显式重设 topmost。
    2. 只设 HWND_TOPMOST 还不够:置顶应用(如 ZCode)也占着 topmost 最前,
       单靠 HWND_TOPMOST 盖不住它。要拿到当前 Z 序最顶的窗口当 hWndInsertAfter,
       把本窗口插到它前面(更靠前)。
    """
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user32.GetTopWindow.argtypes = [ctypes.c_void_p]
        user32.GetTopWindow.restype = ctypes.c_void_p
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
        HWND_TOPMOST = -1

        top = user32.GetTopWindow(None)
        insert_after = top if top else ctypes.c_void_p(HWND_TOPMOST)
        ok = user32.SetWindowPos(
            ctypes.c_void_p(hwnd), insert_after,
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        # 再补一次置顶,保证它留在 topmost 层里
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(ok)
    except Exception:
        return False


def is_covered_by_taskbar(hwnd, ignore_hwnds=()) -> bool:
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

        ignore = {int(x) for x in ignore_hwnds if x}
        prev = user32.GetWindow(ctypes.c_void_p(hwnd), GW_HWNDPREV)
        checked = 0
        while prev and checked < 64:
            # 不可见窗口不占预算(输入法/工具提示等能吃掉十几个名额)
            if not user32.IsWindowVisible(ctypes.c_void_p(prev)):
                prev = user32.GetWindow(ctypes.c_void_p(prev), GW_HWNDPREV)
                continue
            checked += 1
            if prev != hwnd and int(prev) not in ignore:
                r = RECT()
                if user32.GetWindowRect(ctypes.c_void_p(prev), ctypes.byref(r)):
                    # 矩形相交 → 这个窗口盖在我们上面
                    if r.left < mine.right and r.right > mine.left and r.top < mine.bottom and r.bottom > mine.top:
                        return True
            prev = user32.GetWindow(ctypes.c_void_p(prev), GW_HWNDPREV)
    except Exception:
        return False
    return False
