"""Offline coverage of the explicit ChatGPT-share system-proxy exception."""

import ssl
import unittest
from unittest.mock import MagicMock, patch

from runtime_control import CancellationToken, RunCancelled
from web_research import PublicFetchError, SHARE_PROXY_HOSTS, fetch_public


URL = "https://chatgpt.com/share/00000000-1111-2222-3333-444444444444"


def connection(body=b"<html>public share</html>", status=200, location=None):
    result = MagicMock()
    response = result.getresponse.return_value
    response.status = status
    headers = {"Content-Type": "text/html; charset=utf-8", "Location": location}
    response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
    response.read.side_effect = [body, b""]
    return result


class ShareTransportTests(unittest.TestCase):
    def test_default_never_discovers_or_uses_proxy(self):
        direct = connection()
        with patch("web_research.urllib.request.getproxies") as proxies, \
                patch("web_research._addresses", return_value=["1.1.1.1"]) as addresses, \
                patch("web_research._PinnedConnection", return_value=direct) as pinned:
            self.assertEqual(fetch_public(URL)[1], "<html>public share</html>")
        proxies.assert_not_called()
        addresses.assert_called_once_with("chatgpt.com", 443)
        pinned.assert_called_once_with("chatgpt.com", 443, "1.1.1.1", True, 18)

    def test_opt_in_uses_connect_and_certificate_verification(self):
        tunneled = connection()
        with patch("web_research.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}), \
                patch("web_research._addresses") as addresses, \
                patch("web_research.http.client.HTTPSConnection", return_value=tunneled) as https:
            fetch_public(URL, proxy_hosts=SHARE_PROXY_HOSTS, timeout=7)
        addresses.assert_not_called()
        self.assertEqual(https.call_args.args, ("127.0.0.1",))
        self.assertEqual(https.call_args.kwargs["port"], 7890)
        self.assertEqual(https.call_args.kwargs["timeout"], 7)
        context = https.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        tunneled.set_tunnel.assert_called_once_with("chatgpt.com", 443)
        self.assertEqual(tunneled.request.call_args.args, ("GET", "/share/00000000-1111-2222-3333-444444444444"))
        self.assertNotIn("Proxy-Authorization", tunneled.request.call_args.kwargs["headers"])
        tunneled.close.assert_called_once()

    def test_legacy_share_host_and_http_proxy_fallback(self):
        tunneled = connection()
        with patch("web_research.urllib.request.getproxies", return_value={"http": "http://127.0.0.1:7890"}), \
                patch("web_research.http.client.HTTPSConnection", return_value=tunneled):
            fetch_public(URL.replace("chatgpt.com", "chat.openai.com"), proxy_hosts=SHARE_PROXY_HOSTS)
        tunneled.set_tunnel.assert_called_once_with("chat.openai.com", 443)

    def test_no_proxy_retains_pinned_transport(self):
        with patch("web_research.urllib.request.getproxies", return_value={}), \
                patch("web_research._addresses", return_value=["1.1.1.1"]) as addresses, \
                patch("web_research._PinnedConnection", return_value=connection()):
            fetch_public(URL, proxy_hosts=SHARE_PROXY_HOSTS)
        addresses.assert_called_once_with("chatgpt.com", 443)

    def test_other_hosts_paths_and_cleartext_cannot_enable_proxy(self):
        for url in (URL.replace("chatgpt.com", "example.com"), URL.replace("/share/", "/c/"), URL.replace("https:", "http:")):
            with self.subTest(url=url), \
                    patch("web_research.urllib.request.getproxies") as proxies, \
                    patch("web_research._addresses", return_value=["1.1.1.1"]), \
                    patch("web_research._PinnedConnection", return_value=connection()):
                fetch_public(url, proxy_hosts=("chatgpt.com", "example.com"))
                proxies.assert_not_called()

    def test_invalid_proxy_config_rejected_without_exposing_credentials(self):
        invalid = ("socks5://127.0.0.1:7890", "https://127.0.0.1:7890", "http://secret-user:secret-key@127.0.0.1:7890",
            "http://127.0.0.1:7890/private", "http://127.0.0.1:7890?token=secret-key", "http://127.0.0.1:7890#secret-key",
            "http://127.0.0.1:0", "http://127.0.0.1:99999", "http://", "http://127.0.0.1:7890\r\nX-Token:secret-key")
        for proxy in invalid:
            with self.subTest(proxy=proxy), \
                    patch("web_research.urllib.request.getproxies", return_value={"https": proxy}), \
                    patch("web_research.http.client.HTTPSConnection") as https:
                with self.assertRaises(ValueError) as error:
                    fetch_public(URL, proxy_hosts=SHARE_PROXY_HOSTS)
                https.assert_not_called()
                self.assertNotIn("secret-key", str(error.exception))
                self.assertNotIn("secret-user", str(error.exception))

    def test_redirect_is_validated_before_any_next_request(self):
        first = connection(status=302, location="http://127.0.0.1/private")
        seen = []
        def validate(url):
            seen.append(url)
            if url != URL:
                raise ValueError("unsupported share redirect")
            return url
        with patch("web_research.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}), \
                patch("web_research.http.client.HTTPSConnection", return_value=first) as https, \
                patch("web_research._addresses") as addresses:
            with self.assertRaisesRegex(ValueError, "unsupported share redirect"):
                fetch_public(URL, proxy_hosts=SHARE_PROXY_HOSTS, redirect_validator=validate)
        self.assertEqual(seen, [URL, "http://127.0.0.1/private"])
        self.assertEqual(https.call_count, 1)
        addresses.assert_not_called()
        first.close.assert_called_once()

    def test_non_200_and_size_limits_still_apply(self):
        for conn, expected, kwargs in ((connection(status=403), PublicFetchError, {}), (connection(body=b"12345"), ValueError, {"max_bytes": 4})):
            with self.subTest(expected=expected), \
                    patch("web_research.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}), \
                    patch("web_research.http.client.HTTPSConnection", return_value=conn):
                with self.assertRaises(expected):
                    fetch_public(URL, proxy_hosts=SHARE_PROXY_HOSTS, **kwargs)
                conn.close.assert_called_once()

    def test_cancellation_interrupts_proxy_socket_and_cleans_up(self):
        token = CancellationToken()
        tunneled = connection()
        def read(_size):
            token.cancel()
            return b"some bytes"
        tunneled.getresponse.return_value.read.side_effect = read
        with patch("web_research.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}), \
                patch("web_research.http.client.HTTPSConnection", return_value=tunneled):
            with self.assertRaises(RunCancelled):
                fetch_public(URL, cancellation=token, proxy_hosts=SHARE_PROXY_HOSTS)
        tunneled.sock.shutdown.assert_called_once()
        tunneled.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
