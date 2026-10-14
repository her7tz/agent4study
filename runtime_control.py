"""Cooperative cancellation shared by conversations and capability workers."""

import threading


class RunCancelled(Exception):
    pass


class CancellationToken:
    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = []

    def check(self):
        if self.event.is_set():
            raise RunCancelled("已停止执行。")

    def cancel(self):
        self.event.set()
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def register(self, callback):
        with self._lock:
            self._callbacks.append(callback)
        if self.event.is_set():
            callback()
        def remove():
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return remove


def interrupt_socket(response):
    """Wake a blocking network read without waiting for its socket timeout."""
    import socket
    try:
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
