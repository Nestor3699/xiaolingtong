"""睡醒检测。macOS / Windows 有系统事件就听；听不到就靠时钟跳变和 IP 变化。"""

from __future__ import annotations

import os
import sys
import threading


def detection_note() -> str:
    system = sys.platform
    if system == "darwin":
        return (
            "睡醒：优先听 macOS 的 NSWorkspace 睡醒通知（需要 pyobjc）。"
            "没装 pyobjc 时，用时钟跳变（每 15 秒看一次，间隔超过 45 秒当作刚醒）和 IP 变化，然后重新广播上线。"
        )
    if system == "win32":
        return (
            "睡醒：听 Windows 电源广播 PBT_APMRESUMEAUTOMATIC。"
            "如果监听没建成，用时钟跳变（每 15 秒看一次，间隔超过 45 秒当作刚醒）和 IP 变化，然后重新广播上线。"
        )
    return (
        "这台是 Linux，没有 macOS 的 NSWorkspace，也没有 Windows 的 PBT_APMRESUMEAUTOMATIC。"
        "睡醒和断网恢复靠两件事：时钟跳变（每 15 秒看一次，间隔超过 45 秒当作刚醒）以及 IP 变化。发现了就重新广播上线。"
    )


def start(on_wake, on_sleep) -> str:
    """能装上系统监听就装。返回给面板看的一句说明。"""
    note = detection_note()
    if sys.platform == "darwin":
        ok = _start_mac(on_wake, on_sleep)
        if ok:
            return note + " 当前：NSWorkspace 监听已挂上。"
        return note + " 当前：没挂上 NSWorkspace（多半是没装 pyobjc），只用时钟跳变和 IP 变化。"
    if sys.platform == "win32":
        ok = _start_windows(on_wake, on_sleep)
        if ok:
            return note + " 当前：电源广播监听已挂上。"
        return note + " 当前：电源广播没挂上，只用时钟跳变和 IP 变化。"
    return note


def _start_mac(on_wake, on_sleep) -> bool:
    try:
        import objc  # type: ignore
        from AppKit import NSWorkspace  # type: ignore
        from Foundation import NSDate, NSObject, NSRunLoop  # type: ignore
    except Exception:
        return False

    class Observer(NSObject):
        def wake_(self, _note):
            on_wake()

        def sleep_(self, _note):
            on_sleep()

    def loop():
        obs = Observer.alloc().init()
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(obs, "wake:", "NSWorkspaceDidWakeNotification", None)
        center.addObserver_selector_name_object_(obs, "sleep:", "NSWorkspaceWillSleepNotification", None)
        while True:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))

    threading.Thread(target=loop, name="xlt-wake-mac", daemon=True).start()
    return True


def _start_windows(on_wake, on_sleep) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    WM_POWERBROADCAST = 0x0218
    PBT_APMSUSPEND = 0x0004
    PBT_APMRESUMEAUTOMATIC = 0x0012
    PBT_APMRESUMESUSPEND = 0x0007

    def loop():
        try:
            user32 = ctypes.windll.user32

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            def wnd(hwnd, msg, wparam, lparam):
                if msg == WM_POWERBROADCAST and wparam == PBT_APMSUSPEND:
                    on_sleep()
                elif msg == WM_POWERBROADCAST and wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    on_wake()
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            callback = WNDPROC(wnd)
            klass = wintypes.WNDCLASSW()
            klass.lpfnWndProc = callback
            klass.lpszClassName = "XLTWakeWindow"
            klass.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            user32.RegisterClassW(ctypes.byref(klass))
            hwnd = user32.CreateWindowExW(0, "XLTWakeWindow", "xlt", 0, 0, 0, 0, 0, 0, 0, klass.hInstance, None)
            if not hwnd:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            return

    threading.Thread(target=loop, name="xlt-wake-win", daemon=True).start()
    return True
