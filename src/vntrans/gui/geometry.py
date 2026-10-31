"""Window geometry kept between launches: size and maximized state on every platform, position on Windows.

Slint's Python API cannot move a window, so on Windows the Win32 placement API restores the position and
monitor; sizes stay in Slint's logical pixels, where the window's minimum size applies. Every Win32 call has
explicit argument types, since ctypes would otherwise pass 64-bit handles as 32-bit ints."""

import os
import sys

GRIP = 32
LIMIT = 16384


def normalize(value) -> dict:
    """Saved state with every field checked; malformed fields are dropped, since the config is user-editable."""
    value = value if isinstance(value, dict) else {}
    state = dict(maximized=value.get("maximized") is True)
    for key in ("width", "height"):
        number = value.get(key)
        if type(number) in (int, float) and 0 < number <= LIMIT:
            state[key] = float(number)
    position = value.get("position")
    if isinstance(position, list) and len(position) == 2 \
            and all(type(number) is int and abs(number) <= 8 * LIMIT for number in position):
        state["position"] = position
    return state


def overlap(a, b) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def place(rect, areas) -> tuple[tuple, bool]:
    """(rect, fits) for a restored window on monitors with these work areas (primary first).

    The rect stays put while 100 px of its title bar lie on a work area and it fits there; otherwise it moves
    into the work area it overlaps most, or the primary one. fits is False when it is larger than that area."""
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    target = max(areas, key=lambda area: overlap(rect, area))
    fits = width <= target[2] - target[0] and height <= target[3] - target[1]
    grip = (left, top, right, top + GRIP)
    if fits and any(overlap(grip, area) >= min(width, 100) * GRIP for area in areas):
        return rect, True
    x = max(target[0], min(left, target[2] - width))
    y = max(target[1], min(top, target[3] - height))
    return (x, y, x + width, y + height), fits


Native = None

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    NORMAL, MINIMIZED, MAXIMIZED = 1, 2, 3
    RESTORE_TO_MAXIMIZED = 2
    PRIMARY = 1
    LOCATION_CHANGE = 0x800B

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                    ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                    ("rcNormalPosition", wintypes.RECT)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    ENUM_WINDOWS = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    ENUM_MONITORS = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    WIN_EVENT = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                   wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    user32.EnumWindows.argtypes = [ENUM_WINDOWS, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
    user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), ENUM_MONITORS,
                                           wintypes.LPARAM]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WIN_EVENT,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

    def monitors() -> list[MONITORINFO]:
        """Monitor and work-area rectangles in screen coordinates, primary first."""
        found = []

        def visit(handle, dc, rect, data):
            info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                found.append(info)
            return True

        user32.EnumDisplayMonitors(None, None, ENUM_MONITORS(visit), 0)
        return sorted(found, key=lambda info: not info.dwFlags & PRIMARY)

    def box(rect) -> tuple:
        return rect.left, rect.top, rect.right, rect.bottom

    class Native:
        """The app's top-level window. Placements use workspace coordinates, which start at the primary
        monitor's work area; everything here converts them to screen coordinates."""

        def __init__(self, hwnd):
            self.hwnd = hwnd
            self.hook = self.proc = None

        @classmethod
        def find(cls) -> "Native | None":
            """This process's visible titled window; winit creates it shortly after the event loop starts."""
            found = []

            def visit(hwnd, data):
                owner, title = wintypes.DWORD(), ctypes.create_unicode_buffer(2)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == os.getpid() and user32.IsWindowVisible(hwnd) and user32.GetWindowTextW(hwnd, title, 2):
                    found.append(hwnd)
                return not found

            user32.EnumWindows(ENUM_WINDOWS(visit), 0)
            return cls(found[0]) if found else None

        def placement(self) -> WINDOWPLACEMENT | None:
            placement = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
            return placement if user32.GetWindowPlacement(self.hwnd, ctypes.byref(placement)) else None

        @staticmethod
        def offset() -> tuple[int, int]:
            screens = monitors()
            if not screens:
                return 0, 0
            primary = screens[0]
            return primary.rcWork.left - primary.rcMonitor.left, primary.rcWork.top - primary.rcMonitor.top

        def read(self) -> tuple[tuple, int, bool] | None:
            """(restored bounds in screen coordinates, show state, restores maximized), or None once closed."""
            placement = self.placement()
            if placement is None:
                return None
            dx, dy = self.offset()
            left, top, right, bottom = box(placement.rcNormalPosition)
            maximized = placement.showCmd == MAXIMIZED or (
                placement.showCmd == MINIMIZED and bool(placement.flags & RESTORE_TO_MAXIMIZED))
            return (left + dx, top + dy, right + dx, bottom + dy), placement.showCmd, maximized

        def restore(self, position, maximized: bool):
            """Move the window's restored bounds to `position`, keeping its size and keeping it reachable.

            A window too large for its monitor is maximized there instead."""
            placement, areas = self.placement(), [box(info.rcWork) for info in monitors()]
            if placement is None or not areas:
                return
            left, top, right, bottom = box(placement.rcNormalPosition)
            x, y = position
            (left, top, right, bottom), fits = place((x, y, x + right - left, y + bottom - top), areas)
            dx, dy = self.offset()
            placement.rcNormalPosition = wintypes.RECT(left - dx, top - dy, right - dx, bottom - dy)
            placement.showCmd = MAXIMIZED if maximized or not fits else NORMAL
            user32.SetWindowPlacement(self.hwnd, ctypes.byref(placement))

        def watch(self, callback):
            """Call `callback` after every move, resize, minimize or maximize of the window."""
            def changed(hook, event, hwnd, kind, child, thread, time):
                if hwnd == self.hwnd and kind == 0:
                    callback()

            self.proc = WIN_EVENT(changed)
            self.hook = user32.SetWinEventHook(LOCATION_CHANGE, LOCATION_CHANGE, None, self.proc, os.getpid(), 0, 0)

        def close(self):
            if self.hook:
                user32.UnhookWinEvent(self.hook)
            self.hook = self.proc = None
