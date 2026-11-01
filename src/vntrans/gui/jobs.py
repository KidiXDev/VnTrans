"""Bounded background requests and coalesced progress delivery."""

import asyncio
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from queue import Empty, Full

from .. import workspace

_threads = None


async def thread(function, *args, **options):
    """Poll completion on the UI thread; Slint's native thread-safe callbacks can drop UI objects off-thread."""
    global _threads
    if _threads is None:
        _threads = ThreadPoolExecutor(thread_name_prefix="vntrans")
    future = _threads.submit(function, *args, **options)
    while not future.done():
        await asyncio.sleep(0.005)
    return future.result()


def shutdown():
    global _threads
    if _threads is not None:
        _threads.shutdown(wait=True, cancel_futures=True)
        _threads = None


def build(root, **options):
    ws = workspace.Workspace(root)
    try:
        return ws.build(**options)
    finally:
        ws.close()


def _worker(function, args, options, cancel, updates, result):
    last = None

    def progress(*value):
        nonlocal last
        if cancel.is_set():
            raise Cancelled
        last = value
        try:
            updates.put_nowait(value)
        except Full:
            with suppress(Empty):
                updates.get_nowait()
            with suppress(Full):
                updates.put_nowait(value)

    try:
        value = function(*args, **options, progress=progress)
        result.send((True, value, last))
    except BaseException as exc:
        result.send((False, exc, last))
    finally:
        result.close()
        updates.cancel_join_thread()


async def run(function, *args, cancel, progress, **options):
    """Run one CPU-heavy GUI operation in a spawned process, with cooperative cancellation."""
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    updates = context.Queue(maxsize=1)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker, args=(function, args, options, stop, updates, send)
    )
    process.start()
    send.close()
    try:
        while True:
            if cancel.is_set():
                stop.set()
            try:
                value = updates.get_nowait()
            except Empty:
                pass
            else:
                if not cancel.is_set():
                    progress(*value)
            if receive.poll():
                try:
                    ok, value, last = await thread(receive.recv)
                except EOFError as exc:
                    raise OSError("background worker exited without a result") from exc
                if last is not None and not cancel.is_set():
                    progress(*last)
                if not ok:
                    raise value
                return value
            if not process.is_alive():
                raise OSError(
                    f"background worker exited with status {process.exitcode}"
                )
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        receive.close()
        await thread(process.join)
        process.close()
        updates.close()
        updates.join_thread()


class Cancelled(Exception):
    """Cooperative cancellation at a pipeline progress checkpoint."""


class Latest:
    """One running worker and one replaceable pending request; only the latest result applies."""

    def __init__(self, on_error):
        self.on_error = on_error
        self.serial = 0
        self.pending = None
        self.task = None

    def invalidate(self):
        self.serial += 1
        self.pending = None

    def submit(self, work, apply):
        self.serial += 1
        self.pending = self.serial, work, apply
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while self.pending is not None:
            serial, work, apply = self.pending
            self.pending = None
            try:
                result = await thread(work)
                if serial == self.serial:
                    apply(result)
            except Exception as exc:
                if serial == self.serial:
                    self.on_error(exc)


class Progress:
    def __init__(self, show, cancel, *, interval=0.1):
        self.loop = asyncio.get_running_loop()
        self.show = show
        self.cancel = cancel
        self.interval = interval
        self.lock = threading.Lock()
        self.value = None
        self.scheduled = False
        self.closed = False
        self.last_shown = 0
        self.timer = None

    def __call__(self, phase, completed, total, filename):
        if self.cancel.is_set():
            raise Cancelled
        with self.lock:
            if self.closed:
                return
            self.value = phase, completed, total, filename
            if not self.scheduled:
                self.scheduled = True
                self.loop.call_soon_threadsafe(self._flush)

    def _flush(self):
        with self.lock:
            if self.closed or self.cancel.is_set():
                self.scheduled = False
                return
            delay = self.interval - (time.monotonic() - self.last_shown)
            if delay > 0:
                self.timer = self.loop.call_later(delay, self._flush)
                return
            value = self.value
            self.scheduled = False
        self.last_shown = time.monotonic()
        self.show(*value)

    def close(self):
        with self.lock:
            self.closed = True
            value = self.value
        if self.timer:
            self.timer.cancel()
        if value is not None and not self.cancel.is_set():
            self.show(*value)
