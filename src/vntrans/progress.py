"""CLI-only progress: animated terminals, readable lines when redirected."""

import shutil
import sys
import threading


class Progress:
    def __init__(self):
        self.stream = sys.stderr
        self.terminal = self.stream.isatty()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.state = ("", 0, 0, "")
        self.last_logged = None

    def __enter__(self):
        return self.update

    def update(self, phase, completed, total, filename):
        filename = "".join(c if c.isprintable() else "?" for c in filename)
        with self.lock:
            self.state = (phase, completed, total, filename)
            if not self.terminal and (phase, completed) != self.last_logged:
                count = f" {completed}/{total}" if total else "..."
                print(f"{phase}{count}" + (f"  {filename}" if filename else ""),
                      file=self.stream, flush=True)
                self.last_logged = (phase, completed)
        if self.terminal and self.thread is None:
            self.thread = threading.Thread(target=self.animate, daemon=True)
            self.thread.start()

    def animate(self):
        frame = 0
        while not self.stop.is_set():
            with self.lock:
                phase, completed, total, filename = self.state
                if total:
                    filled = 16 * completed // total
                    meter = f"[{'#' * filled}{'-' * (16 - filled)}] {completed}/{total} {100 * completed // total:3d}%"
                else:
                    meter = "..."
                line = f"{'|/-\\'[frame % 4]} {phase} {meter}  {filename}"
                columns = max(1, shutil.get_terminal_size().columns - 1)
                print("\r" + line[:columns].ljust(columns), end="", file=self.stream, flush=True)
            frame += 1
            self.stop.wait(0.1)

    def __exit__(self, *exc):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
            columns = max(1, shutil.get_terminal_size().columns - 1)
            print("\r" + " " * columns + "\r", end="", file=self.stream, flush=True)
