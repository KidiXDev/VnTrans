"""Desktop GUI (optional `gui` extra)."""

import sys
import gc


def main(argv=None):
    import slint

    from .app import Window

    argv = sys.argv[1:] if argv is None else argv
    slint.set_xdg_app_id("vntrans")
    automatic_gc = gc.isenabled()
    # Slint models must be collected on the UI thread, never by a Python worker's automatic GC.
    gc.disable()
    try:
        window = Window()
        window.restore_geometry()
        window.show()

        async def start():
            await window.track_geometry()
            if argv:
                await window.open(argv[0])

        slint.run_event_loop(start())
        window.gc_timer.stop()
        window.save_geometry()
    finally:
        gc.collect()
        if automatic_gc:
            gc.enable()
