"""A Windows system tray icon in pure Win32 ctypes (no extra packages).

Used by the font server's exe (font_server.py): the icon shows the address,
a left click opens the administration page, a right click a menu. The same
technique as the WebREPL's tray (minischeme-webrepl server.py). Importing this
module is safe on any system; only Tray.run() needs Windows.
"""

import os
import threading


def _font_icon_pixels(w=32, h=32):
    """BGRA pixels: a dark rounded square, an amber frame and a white "A"."""
    def near(px, py, x0, y0, x1, y1, width):
        # distance from (px, py) to the segment (x0, y0)-(x1, y1)
        dx, dy = x1 - x0, y1 - y0
        t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / float(dx * dx + dy * dy)))
        ex, ey = x0 + t * dx - px, y0 + t * dy - py
        return ex * ex + ey * ey <= width * width

    pixels = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            if min(x, w - 1 - x) + min(y, h - 1 - y) < 3:
                continue                                     # rounded corners stay transparent
            if x in (2, 3, w - 3, w - 4) or y in (2, 3, h - 3, h - 4):
                bgra = (85, 188, 240, 255)                   # #f0bc55 amber frame
            elif (near(x, y, 16, 7, 9, 25, 1.3) or near(x, y, 16, 7, 23, 25, 1.3)
                  or near(x, y, 12, 19, 20, 19, 1.0)):
                bgra = (255, 255, 255, 255)                  # the "A"
            else:
                bgra = (32, 21, 13, 255)                     # #0d1520 background
            i = (y * w + x) * 4
            pixels[i:i + 4] = bytes(bgra)
    return pixels


class Tray:
    """menu: a list of (label, callback) and None (a separator); the first
    entry is the default one (bold). on_click: a left click. on_exit: called
    (in a thread) after the icon is removed by the menu's last entry."""

    def __init__(self, title, tip, menu, on_click=None, on_exit=None, balloon=None):
        self.title = title
        self.tip = tip
        self.menu = menu
        self.on_click = on_click
        self.on_exit = on_exit
        self.balloon = balloon            # (title, text) shown once at the start, or None
        self._wndproc_ref = None

    def _icon(self):
        import ctypes
        from ctypes import wintypes
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                        ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

        w = h = 32
        pixels = _font_icon_pixels(w, h)
        buf = (ctypes.c_char * len(pixels)).from_buffer(pixels)
        hbm_color = gdi32.CreateBitmap(w, h, 1, 32, ctypes.byref(buf))
        mask = (ctypes.c_char * (w * h // 8))()
        hbm_mask = gdi32.CreateBitmap(w, h, 1, 1, ctypes.byref(mask))
        hicon = user32.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, hbm_mask, hbm_color)))
        gdi32.DeleteObject(hbm_color)
        gdi32.DeleteObject(hbm_mask)
        return hicon or user32.LoadIconW(None, 32512)   # IDI_APPLICATION

    def run(self):
        """Shows the icon and runs the message loop until the exit entry is chosen."""
        import ctypes
        from ctypes import wintypes
        user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32

        WM_DESTROY, WM_TRAY = 0x0002, 0x0400 + 20
        WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
        NIM_ADD, NIM_DELETE = 0, 2
        NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
        TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
        MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
        ID_BASE = 1000

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD)]

        exit_id = ID_BASE + len(self.menu)

        def show_menu(hwnd):
            hmenu = user32.CreatePopupMenu()
            for k, item in enumerate(self.menu):
                if item is None:
                    user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                else:
                    user32.AppendMenuW(hmenu, MF_STRING, ID_BASE + k, item[0])
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, exit_id, f"❌ 退出{self.title}")
            user32.SetMenuDefaultItem(hmenu, ID_BASE, False)
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(hmenu)
            if cmd == exit_id:
                user32.DestroyWindow(hwnd)
            elif ID_BASE <= cmd < exit_id and self.menu[cmd - ID_BASE]:
                try:
                    self.menu[cmd - ID_BASE][1]()
                except Exception as e:   # a menu action must never end the tray
                    print(f"[Warn] tray action failed: {e}")

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK) and self.on_click:
                    self.on_click()
                    return 0
                if lparam == WM_RBUTTONUP:
                    show_menu(hwnd)
                    return 0
            elif msg == WM_DESTROY:
                nid_del = NOTIFYICONDATAW()
                nid_del.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid_del.hWnd = hwnd
                nid_del.uID = 1
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid_del))
                user32.PostQuitMessage(0)
                if self.on_exit:
                    threading.Thread(target=self.on_exit, daemon=True).start()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"MiniSchemeFontTrayWnd_{os.getpid()}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, class_name, self.title, 0, 0, 0, 0, 0, None, None, hinst, None)

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | (NIF_INFO if self.balloon else 0)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon()
        nid.szTip = self.tip[:127]
        if self.balloon:
            nid.szInfoTitle = self.balloon[0][:63]
            nid.szInfo = self.balloon[1][:255]
            nid.dwInfoFlags = 1   # NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
