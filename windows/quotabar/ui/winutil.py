# -*- coding: utf-8 -*-
"""Windows 任务栏几何工具:供详情窗定位与任务栏数字面板共用的 Shell_TrayWnd 探测。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from typing import Optional


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


def taskbar_rect() -> Optional[RECT]:
    """主任务栏(Shell_TrayWnd)矩形;拿不到返回 None"""
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


def _all_taskbar_rects() -> list[RECT]:
    """全部任务栏矩形:主屏 Shell_TrayWnd + 副屏 Shell_SecondaryTrayWnd。

    只查主任务栏的话,副屏上详情窗拿不到任务栏上沿,会压进副屏任务栏里。
    """
    rects: list[RECT] = []
    main = taskbar_rect()
    if main is not None:
        rects.append(main)
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowExW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p
        ]
        user32.FindWindowExW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowExW(None, None, "Shell_SecondaryTrayWnd", None)
        while hwnd:
            rect = RECT()
            if ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                rects.append(rect)
            hwnd = user32.FindWindowExW(None, ctypes.c_void_p(hwnd), "Shell_SecondaryTrayWnd", None)
    except Exception:
        pass
    return rects


def set_dark_title_bar(hwnd, dark: bool = True) -> bool:
    """把标题栏切成深色(Windows 10 1809+ / 11 的 DwmSetWindowAttribute)。

    Qt 的深色 stylesheet 只能画客户区,原生标题栏仍跟系统应用模式走;
    这里补一刀,让深色详情窗的标题栏不突兀。失败静默(老系统/远程桌面)。
    """
    try:
        dwm = ctypes.windll.dwmapi
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # 20=Win10 20H1+, 19=更早的预览版
            if dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(value),
                ctypes.sizeof(value),
            ) == 0:
                return True
    except Exception:
        pass
    return False


def taskbar_top_edge(screen_geo) -> Optional[int]:
    """底部任务栏的上沿坐标;仅当任务栏横跨该屏幕底部时返回,否则 None。

    screen_geo 传 QRect(目标屏幕 geometry),用于判断任务栏是否属于该屏幕。
    主屏与副屏任务栏都查:副屏任务栏是独立的 Shell_SecondaryTrayWnd 窗口。
    """
    for rect in _all_taskbar_rects():
        overlaps_x = rect.left < screen_geo.right() and rect.right > screen_geo.left()
        at_bottom = rect.top > screen_geo.center().y()
        if overlaps_x and at_bottom:
            return rect.top
    return None


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
        SW_SHOW = 5
        SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0020, 0x0040

        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)

        orig_style = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE) & 0xFFFFFFFF
        orig_ex = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE) & 0xFFFFFFFF
        style = orig_style & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        style |= WS_CHILD
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE, style)

        ex = orig_ex | WS_EX_TOOLWINDOW      # 不进 Alt+Tab / 任务栏按钮
        ex &= ~WS_EX_TRANSPARENT            # 有这个就全穿透,鼠标永远到不了
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)

        # SetParent 成功时返回的是**旧父窗口**(顶层窗口是 NULL),返回值分不清
        # 成功失败——只能看 GetLastError。
        user32.SetParent.use_last_error = True
        ctypes.set_last_error(0)
        user32.SetParent(ctypes.c_void_p(hwnd), ctypes.c_void_p(taskbar))
        if ctypes.get_last_error() != 0:
            # 嵌入失败(explorer 恰好重启):回滚样式。
            # 不回滚的话窗口会变成「没有父窗口的 WS_CHILD」——既画不出来也收不到
            # 鼠标,等于彻底消失,只能重启进程。
            user32.SetParent(ctypes.c_void_p(hwnd), None)
            user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE, orig_style)
            user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, orig_ex)
            user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOW)
            return None

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


#: 这些窗口不该当作"要压过的对象":输入法候选窗、工具提示都是系统辅助窗口,
#: 而且它们经常排在 Z 序最前,拿它们当 hWndInsertAfter 会导致插错位置
_IGNORED_TOP_CLASSES = (
    "IME",
    "Default IME",
    "MSCTFIME",
    "tooltips_class32",
    "Windows.UI.Core.CoreWindow",
    "EdgeUiInputTopWndClass",
)


def _top_visible_window(exclude_hwnds=(), exclude_pid=None) -> Optional[int]:
    """Z 序最前的"真实"可见窗口(排除输入法/提示框等系统辅助窗口,以及指定窗口)。

    之前的 bug:GetTopWindow(None) 返回的常是 IME 窗口,拿它当插入位置,
    结果浮窗插不到置顶应用(ZCode)之上,表现为"浮窗不出现"。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
        skip = {int(x) for x in exclude_hwnds if x}
        found: list[int] = []

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                return True
            if int(hwnd) in skip:
                return True
            if exclude_pid is not None:
                # 同进程的其他窗口(面板/浮窗)不能当插入目标:
                # 插到"自己人"前面等于没插,压不住置顶应用
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
                if pid.value == exclude_pid:
                    return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
            name = buf.value
            if any(p in name for p in _IGNORED_TOP_CLASSES):
                return True
            found.append(int(hwnd))
            return False  # 停:EnumWindows 的枚举顺序就是 Z 序从顶到底

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        return found[0] if found else None
    except Exception:
        return None


def ensure_topmost(hwnd) -> bool:
    """把窗口提到 Z 序最前。

    三件事都要做:
    1. 任务栏(Shell_TrayWnd)本身是 topmost 窗口,普通的 Qt.Tool + WindowStaysOnTopHint
       窗口会被它压在下面,面板直接"消失"——所以必须显式重设 topmost。
    2. 只设 HWND_TOPMOST 还不够:置顶应用(如 ZCode)也占着 topmost 最前,
       单靠 HWND_TOPMOST 盖不住它。要拿到当前 Z 序最前的"真实"窗口当 hWndInsertAfter,
       把本窗口插到它前面(更靠前)。
    3. 取"最前窗口"不能直接用 GetTopWindow(None)——它经常返回输入法(IME)候选窗,
       插错位置导致浮窗依然被置顶应用盖住(表现为"浮窗不出现")。
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
        SWP_SHOWWINDOW = 0x0040
        HWND_TOPMOST = -1

        # 排除本进程的所有窗口:插到"自己人"前面等于没插,
        # 结果依然被置顶应用(如 ZCode)盖住——而且置顶应用会抢走
        # 该区域的鼠标消息,面板就完全收不到 hover/点击。
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        top = _top_visible_window(exclude_hwnds=(hwnd,), exclude_pid=pid.value)
        insert_after = ctypes.c_void_p(top) if top else ctypes.c_void_p(HWND_TOPMOST)
        # 带 SWP_SHOWWINDOW:目标窗口此刻若处于隐藏状态,没有这个标志就不会被显示
        ok = user32.SetWindowPos(
            ctypes.c_void_p(hwnd), insert_after,
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        # 再补一次置顶,保证它留在 topmost 层里
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(ok)
    except Exception:
        return False

