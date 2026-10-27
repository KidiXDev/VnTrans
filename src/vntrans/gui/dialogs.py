"""Native folder/file picker. Returns None when cancelled or unavailable; path fields still work."""

import secrets
import subprocess
import sys
from urllib.parse import unquote, urlparse

PORTAL = "org.freedesktop.portal.Desktop"


def pick(title: str, folder: bool = True) -> str | None:
    """Blocking; call from a worker thread."""
    try:
        if sys.platform == "win32":
            return tk_pick(title, folder)
        if sys.platform == "darwin":
            return osascript_pick(title, folder)
        return portal_pick(title, folder)
    except Exception:  # any picker failure leaves the typed path as the fallback
        return None


def portal_pick(title, folder):
    # The portal cancels a request when its caller disconnects, so keep one connection open throughout.
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection

    with open_dbus_connection(bus="SESSION") as connection:
        token = "vntrans_" + secrets.token_hex(6)
        sender = connection.unique_name.removeprefix(":").replace(".", "_")
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response",
                         path=f"/org/freedesktop/portal/desktop/request/{sender}/{token}")
        connection.send_and_get_reply(message_bus.AddMatch(rule))
        with connection.filter(rule) as responses:
            chooser = DBusAddress("/org/freedesktop/portal/desktop", bus_name=PORTAL,
                                  interface="org.freedesktop.portal.FileChooser")
            connection.send_and_get_reply(new_method_call(chooser, "OpenFile", "ssa{sv}", (
                "", title, {"handle_token": ("s", token), "directory": ("b", folder)})))
            code, results = connection.recv_until_filtered(responses).body
    uris = results.get("uris", ("as", []))[1]
    return unquote(urlparse(uris[0]).path) if code == 0 and uris else None


def osascript_pick(title, folder):
    script = f'POSIX path of (choose {"folder" if folder else "file"} with prompt "{title}")'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip() or None if result.returncode == 0 else None


def tk_pick(title, folder):
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if folder:
            return filedialog.askdirectory(title=title, mustexist=False) or None
        return filedialog.askopenfilename(title=title) or None
    finally:
        root.destroy()
