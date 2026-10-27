"""Desktop GUI (optional `gui` extra)."""

import sys


def main(argv=None):
    import slint

    from .app import Window

    argv = sys.argv[1:] if argv is None else argv
    slint.set_xdg_app_id("vntrans")
    window = Window()
    window.show()

    async def start():
        if argv:
            window.open(argv[0])

    slint.run_event_loop(start())
