"""Bounded transport for the fixed DeepSeek endpoint.

Only opening a TCP/TLS connection may be attempted again. Once an API request
starts, its outcome can be uncertain, so neither route switching nor request
replay is allowed. A system HTTP proxy is a fallback for connection setup only.
"""

from contextlib import contextmanager
import http.client
import logging
import math
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request


API_HOST = "api.deepseek.com"
CONNECT_TIMEOUT = 10.0
MAX_CONNECTION_ATTEMPTS = 2
_LOGGER = logging.getLogger("minimal_agent")


class ModelTransportError(RuntimeError):
    """A terminal transport failure; outer Agent loops must not replay it."""

    retryable = False

    def __init__(self, message, phase, request_started=False):
        super().__init__(message)
        self.phase = phase
        self.request_started = request_started


def _check(cancellation):
    if cancellation is not None:
        cancellation.check()


def _remaining(deadline, cancellation=None):
    _check(cancellation)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("DeepSeek API 请求超过总超时预算。")
    return remaining


def _request_parts(request):
    try:
        parsed = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == API_HOST
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (parsed.path, method)
            in {("/chat/completions", "POST"), ("/models", "GET")}
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ModelTransportError(
            "模型请求必须使用 DeepSeek 官方默认 HTTPS 地址及受支持接口。", "configuration"
        )
    headers = {
        name: value
        for name, value in request.header_items()
        if name.lower() not in {"host", "connection", "proxy-authorization", "proxy-connection"}
    }
    headers["Connection"] = "close"
    return method, parsed.path, request.data, headers


def _system_proxy():
    """Read the existing proxy without ever returning its original URL."""
    proxies = urllib.request.getproxies()
    value = proxies.get("https") or proxies.get("http")
    if not value:
        return None
    try:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value):
            raise ValueError
        proxy = urllib.parse.urlsplit(value)
        port = 80 if proxy.port is None else proxy.port
        if (
            proxy.scheme != "http"
            or not proxy.hostname
            or proxy.username is not None
            or proxy.password is not None
            or proxy.path not in {"", "/"}
            or proxy.query
            or proxy.fragment
            or not 1 <= port <= 65535
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ModelTransportError(
            "DeepSeek 直连失败；系统代理仅支持无凭证的 HTTP 代理，请检查系统代理设置。",
            "configuration",
        ) from None
    return proxy.hostname, port


class _DeadlineResponse(http.client.HTTPResponse):
    """Refresh the remaining socket budget before every public read."""

    def __init__(self, sock, *args, deadline, cancellation, **kwargs):
        self._transport_socket = sock
        self._deadline = deadline
        self._cancellation = cancellation
        super().__init__(sock, *args, **kwargs)

    def _prepare_read(self):
        remaining = _remaining(self._deadline, self._cancellation)
        if self.fp is not None:
            self._transport_socket.settimeout(remaining)

    def read(self, amt=None):
        self._prepare_read()
        return super().read(amt)

    def read1(self, n=-1):
        self._prepare_read()
        return super().read1(n)

    def readline(self, limit=-1):
        self._prepare_read()
        return super().readline(limit)

    def readinto(self, buffer):
        self._prepare_read()
        return super().readinto(buffer)


def _safe_http_detail(data, headers):
    detail = data.decode("utf-8", errors="replace")
    for name, value in headers.items():
        if name.lower() == "authorization" and isinstance(value, str):
            detail = detail.replace(value, "[已隐藏凭证]")
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[1]:
                detail = detail.replace(parts[1], "[已隐藏凭证]")
    return detail


@contextmanager
def open_model_response(request, timeout, cancellation=None):
    """Yield one verified response, with at most two pre-request connections.

    ``request`` is a urllib Request, but urllib's mutable proxy handlers and
    automatic redirect machinery are deliberately not used. All resource and
    cancellation cleanup also covers exceptions raised by the response reader.
    """
    _check(cancellation)
    method, path, body, headers = _request_parts(request)
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ModelTransportError("DeepSeek API 超时预算必须为正数。", "configuration") from None
    deadline = time.monotonic() + timeout
    current = {"connection": None, "response": None}
    timed_out = threading.Event()
    phase = "connect"
    request_started = False

    def interrupt():
        connection = current["connection"]
        response = current["response"]
        sockets = [getattr(connection, "sock", None), getattr(response, "_transport_socket", None)]
        for sock in sockets:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def expire():
        timed_out.set()
        interrupt()

    remove = cancellation.register(interrupt) if cancellation is not None else lambda: None
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    connection = response = None
    try:
        route = None
        context = ssl.create_default_context()
        for attempt in range(MAX_CONNECTION_ATTEMPTS):
            _check(cancellation)
            budget = min(CONNECT_TIMEOUT, _remaining(deadline, cancellation))
            host, port = route if route is not None else (API_HOST, 443)
            connection = http.client.HTTPSConnection(host, port=port, timeout=budget, context=context)
            # Even cancellation between connect() and request() must never cause
            # http.client.send() to reconnect implicitly.
            connection.auto_open = 0
            if route is not None:
                connection.set_tunnel(API_HOST, 443)
            current["connection"] = connection
            connection.response_class = lambda sock, *args, **kwargs: _DeadlineResponse(
                sock, *args, deadline=deadline, cancellation=cancellation, **kwargs
            )
            try:
                connection.connect()
                _remaining(deadline, cancellation)
                break
            except ssl.SSLCertVerificationError as exc:
                _check(cancellation)
                _LOGGER.warning("DeepSeek connection failed route=%s attempt=%s exception=%s",
                    "system_proxy" if route is not None else "direct", attempt + 1, type(exc).__name__)
                raise ModelTransportError(
                    "DeepSeek API 的 TLS 证书校验失败，请检查系统时间与网络证书配置。", "connect"
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                _check(cancellation)
                _LOGGER.warning("DeepSeek connection failed route=%s attempt=%s exception=%s",
                    "system_proxy" if route is not None else "direct", attempt + 1, type(exc).__name__)
                connection.close()
                current["connection"] = None
                if attempt + 1 >= MAX_CONNECTION_ATTEMPTS:
                    raise ModelTransportError(
                        "无法连接 DeepSeek API：两次建连均失败，尚未发送生成请求，请稍后重试。", "connect"
                    ) from exc
                _remaining(deadline, cancellation)
                route = _system_proxy()

        _remaining(deadline, cancellation)
        connection.sock.settimeout(_remaining(deadline, cancellation))
        phase = "request"
        request_started = True
        connection.request(method, path, body=body, headers=headers)
        phase = "response_headers"
        connection.sock.settimeout(_remaining(deadline, cancellation))
        response = connection.getresponse()
        current["response"] = response
        _remaining(deadline, cancellation)
        phase = "response_body"
        if not 200 <= response.status < 300:
            detail = _safe_http_detail(response.read(1200), headers)
            _check(cancellation)
            raise ModelTransportError(
                "DeepSeek API 返回 HTTP {}：{}".format(response.status, detail), phase, True
            )
        yield response
        _remaining(deadline, cancellation)
    except (OSError, http.client.HTTPException) as exc:
        _check(cancellation)
        _LOGGER.warning("DeepSeek transport failed phase=%s route=%s exception=%s",
            phase, "system_proxy" if route is not None else "direct", type(exc).__name__)
        if timed_out.is_set() or isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
            message = "DeepSeek API 请求超过总超时预算。"
        elif request_started:
            number = getattr(exc, "errno", None)
            kind = "（Errno {}）".format(number) if isinstance(number, int) else ""
            message = "DeepSeek API 连接在发送请求或读取回复时中断{}；为避免重复生成，未自动重发，请手动重试。".format(kind)
        else:
            message = "无法连接 DeepSeek API，请检查网络或稍后重试。"
        raise ModelTransportError(message, phase, request_started) from exc
    finally:
        timer.cancel()
        remove()
        for resource in (response, connection):
            if resource is not None:
                try:
                    resource.close()
                except OSError:
                    # Cleanup must not hide the original transport failure or
                    # prevent the remaining socket/cancellation cleanup.
                    pass
