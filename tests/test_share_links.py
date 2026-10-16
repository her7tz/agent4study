"""Offline regression coverage for public ChatGPT share text extraction.

Fixtures contain synthetic conversations only. No network requests or model
calls are made by these tests.
"""

import json
import unittest
from unittest.mock import patch

from runtime_control import CancellationToken, RunCancelled
from share_links import (
    ShareLinkError, _ShareHTML, _UnresolvedPromise, _json_payloads, _unflatten,
    normalize_share_url, read_shared_conversation,
)
from web_research import PublicFetchError, fetch_public


URL = "https://chatgpt.com/share/00000000-1111-2222-3333-444444444444"


class Promise:
    def __init__(self, number):
        self.number = number


class Previous:
    def __init__(self, index):
        self.index = index


class Pool:
    """Minimal fixture encoder with one global reference table and string reuse."""
    def __init__(self):
        self.values, self.strings = [], {}

    def add(self, value):
        if value is None:
            return -7
        if isinstance(value, str) and value in self.strings:
            return self.strings[value]
        index = len(self.values)
        self.values.append(None)
        if isinstance(value, str):
            self.strings[value] = index
        if isinstance(value, Promise):
            encoded = ["P", value.number]
        elif isinstance(value, Previous):
            encoded = ["Z", value.index]
        elif isinstance(value, dict):
            encoded = {"_" + str(self.add(key)): self.add(item) for key, item in value.items()}
        elif isinstance(value, list):
            encoded = [self.add(item) for item in value]
        else:
            encoded = value
        self.values[index] = encoded
        return index


def message(role, content, identifier=None, **fields):
    return {"id": identifier or role + "-message", "author": {"role": role},
        "content": {"content_type": "text", "parts": [content]}, **fields}


def conversation():
    return {"title": "递归方法", "linear_conversation": [
        {"message": message("user", "递归是什么？")},
        {"message": message("assistant", "递归包含终止条件与递推步骤。")},
    ]}


def json_page(payload, script_id="__NEXT_DATA__"):
    return '<html><head><title>递归方法 - ChatGPT</title></head><body><script id="{}" type="application/json">{}</script></body></html>'.format(
        script_id, json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c"))


def stream_page(lines, split_at=None):
    stream = "\n".join(lines) + "\n"
    chunks = [stream] if split_at is None else [stream[:split_at], stream[split_at:]]
    scripts = ["<script>window.__reactRouterContext.streamController.enqueue(" + json.dumps(chunk) + ");</script>" for chunk in chunks]
    return "<html><body>" + "".join(scripts) + "</body></html>"


class ShareUrlTests(unittest.TestCase):
    def test_supported_hosts_normalize_queries_and_fragments(self):
        for value in (URL, URL + "/?utm_source=chat#answer", URL.replace("chatgpt.com", "chat.openai.com")):
            with self.subTest(value=value):
                self.assertEqual(normalize_share_url(value), URL)

    def test_private_unsupported_or_credential_urls_are_rejected(self):
        for value in (None, "", "http://chatgpt.com/share/abcdefgh", "https://chatgpt.com/c/abcdefgh",
                "https://evil.invalid/share/abcdefgh", "https://chatgpt.com.evil.invalid/share/abcdefgh",
                "https://user:pass@chatgpt.com/share/abcdefgh", "https://chatgpt.com:8443/share/abcdefgh",
                "file:///tmp/private", "https://chatgpt.com/share/a%2fb12345678", "https://chatgpt.com/share/short"):
            with self.subTest(value=value):
                with self.assertRaises(ShareLinkError):
                    normalize_share_url(value)


class ShareParsingTests(unittest.TestCase):
    def read(self, html, **kwargs):
        with patch("share_links.fetch_public", return_value=(URL, html, "text/html; charset=utf-8")) as fetch:
            result = read_shared_conversation(URL, **kwargs)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.kwargs["max_bytes"], 8 * 1024 * 1024)
            return result

    def test_next_data_extracts_order_roles_ids_and_source(self):
        result = self.read(json_page({"props": {"pageProps": {"serverResponse": {"data": conversation()}}}}))
        self.assertEqual(result["title"], "递归方法")
        self.assertEqual([item["role"] for item in result["messages"]], ["user", "assistant"])
        self.assertEqual([item["id"] for item in result["messages"]], [1, 2])
        self.assertEqual(result["messages"][1]["source_message_id"], "assistant-message")
        self.assertEqual(result["source_type"], "chatgpt_share")
        self.assertEqual(result["source_urls"], [URL])

    def test_application_json_is_supported_without_next_specific_path(self):
        result = self.read(json_page({"loaderData": {"renamed-route": conversation()}}, script_id="public-data"))
        self.assertEqual(len(result["messages"]), 2)

    def test_real_code_and_actual_urls_are_preserved_without_fetching_them(self):
        payload = conversation()
        text = "```python\ndef f():\n    return 1\n```\n参考 https://example.org/method"
        payload["linear_conversation"][1]["message"]["content"]["parts"] = [text]
        result = self.read(json_page(payload))
        self.assertEqual(result["messages"][1]["content"], text)
        self.assertIn("https://example.org/method", result["source_urls"])

    def test_hidden_analysis_system_tool_and_private_context_are_excluded(self):
        payload = conversation()
        extras = [message("system", "secret-system"), message("tool", "tool-output"),
            message("assistant", "hidden-answer", metadata={"is_visually_hidden_from_conversation": True}),
            message("assistant", "private-analysis", channel="analysis"),
            message("assistant", "private-analysis-2", metadata={"channel": "analysis"}),
            message("assistant", "tool-request", recipient="python"),
            message("user", "private-custom-context")]
        extras[-1]["content"]["content_type"] = "user_editable_context"
        payload["linear_conversation"] += [{"message": item} for item in extras]
        result = self.read(json_page(payload))
        self.assertEqual(len(result["messages"]), 2)

    def test_non_text_attachments_are_marked_without_reading(self):
        payload = conversation()
        payload["linear_conversation"][1]["message"]["content"]["parts"].append({"content_type": "image_asset_pointer", "asset_pointer": "private-image"})
        result = self.read(json_page(payload))
        self.assertTrue(result["source_details"]["has_attachments"])
        self.assertIn("未读取", result["messages"][1]["content"])
        self.assertNotIn("private-image", result["messages"][1]["content"])

    def test_author_html_excludes_controls_and_scripts(self):
        html = '<title>公开笔记 - ChatGPT</title><nav>登录 注册</nav><main><div data-message-author-role="user" data-message-id="u1"><p>解释递归</p></div><div data-message-author-role="assistant" data-message-id="a1"><p>需要终止条件。</p><pre><code>return f(n - 1)</code></pre><button>复制答案</button><script>steal_secret()</script><img src="https://example.invalid/a.png" /></div></main>'
        result = self.read(html)
        self.assertEqual(result["source_details"]["reader"], "author_html")
        answer = result["messages"][1]["content"]
        self.assertIn("```", answer)
        self.assertIn("return f(n - 1)", answer)
        for omitted in ("登录", "复制答案", "steal_secret"):
            self.assertNotIn(omitted, answer)

    def test_image_only_html_is_not_a_readable_model_reply(self):
        html = '<div data-message-author-role="user">图片里有什么？</div><div data-message-author-role="assistant"><img src="https://example.invalid/a.png" /></div>'
        with self.assertRaises(ShareLinkError):
            self.read(html)

    def test_navigation_challenge_or_unexecuted_script_does_not_become_chat(self):
        for html in ('<title>Just a moment...</title><div>Checking your browser</div>',
                '<h1>ChatGPT</h1><button>Log in</button>',
                '<script>window.config={"messages":[{"role":"assistant","content":"not-data"}]}</script>'):
            with self.subTest(html=html):
                with self.assertRaises(ShareLinkError):
                    self.read(html)

    def test_identical_structured_candidates_are_deduplicated(self):
        html = json_page(conversation()) + json_page(conversation(), script_id="same-snapshot")
        self.assertEqual(len(self.read(html)["messages"]), 2)

    def test_inconsistent_structured_candidates_are_rejected(self):
        alternate = conversation()
        alternate["linear_conversation"][1]["message"]["content"]["parts"] = ["不同分支"]
        with self.assertRaisesRegex(ShareLinkError, "多组不一致"):
            self.read(json_page(conversation()) + json_page(alternate))

    def test_current_node_selects_only_its_branch(self):
        payload = {"title": "分支", "current_node": "a", "mapping": {
            "u": {"message": message("user", "问题"), "parent": None, "children": ["a", "b"]},
            "a": {"message": message("assistant", "选中的答案", "a"), "parent": "u", "children": []},
            "b": {"message": message("assistant", "另一分支", "b"), "parent": "u", "children": []},
        }}
        result = self.read(json_page(payload))
        self.assertEqual(result["messages"][1]["content"], "选中的答案")

    def test_ambiguous_or_broken_mapping_is_rejected(self):
        cases = [
            {"mapping": {"a": {"message": message("assistant", "A"), "children": []}, "b": {"message": message("assistant", "B"), "children": []}}},
            {"current_node": "missing", "mapping": {"a": {"message": message("assistant", "A"), "parent": None, "children": []}}},
            {"current_node": "a", "mapping": {"a": {"message": message("assistant", "A"), "parent": "missing", "children": []}}},
            {"current_node": "a", "mapping": {"a": {"message": message("assistant", "A"), "parent": "a", "children": ["a"]}}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ShareLinkError):
                    self.read(json_page(payload))

    def test_linear_mapping_reference_must_exist(self):
        payload = {"mapping": {"a": {"message": message("assistant", "A")}}, "linear_conversation": ["a", "missing"]}
        with self.assertRaisesRegex(ShareLinkError, "引用不完整"):
            self.read(json_page(payload))

    def test_size_limit_rejects_without_silent_truncation(self):
        with patch("share_links.MAX_TEXT", 4):
            with self.assertRaisesRegex(ShareLinkError, "上限"):
                self.read(json_page(conversation()))

    def test_http_block_is_reported_without_parsing_a_fallback(self):
        with patch("share_links.fetch_public", side_effect=PublicFetchError(403)):
            with self.assertRaisesRegex(ShareLinkError, "HTTP 403"):
                read_shared_conversation(URL)

    def test_timeout_has_actionable_network_error_without_claiming_parse_failure(self):
        with patch("share_links.fetch_public", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(ShareLinkError, "连接 ChatGPT 分享页面超时.*尚未取得聊天正文.*网络或代理") as error:
                read_shared_conversation(URL)
        self.assertIsInstance(error.exception.__cause__, TimeoutError)

    def test_connection_error_is_classified_before_parsing(self):
        with patch("share_links.fetch_public", side_effect=OSError("connection reset")):
            with self.assertRaisesRegex(ShareLinkError, "无法连接 ChatGPT 分享页面.*尚未取得聊天正文") as error:
                read_shared_conversation(URL)
        self.assertIsInstance(error.exception.__cause__, OSError)

    def test_cancellation_stops_before_parsing(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(RunCancelled):
            self.read(json_page(conversation()), cancellation=token)

    def test_redirect_guard_rejects_before_public_network_resolution(self):
        with patch("share_links.fetch_public", return_value=(URL, json_page(conversation()), "text/html")) as fetch:
            read_shared_conversation(URL)
        validator = fetch.call_args.kwargs["redirect_validator"]
        self.assertEqual(validator(URL.replace("chatgpt.com", "chat.openai.com")), URL.replace("chatgpt.com", "chat.openai.com"))
        for target in ("https://example.org/", "https://127.0.0.1/share/abcdefgh", "https://chatgpt.com/auth/login", URL[:-1] + "5"):
            with self.subTest(target=target), patch("web_research._addresses") as addresses:
                with self.assertRaises(ShareLinkError):
                    fetch_public(target, redirect_validator=validator)
                addresses.assert_not_called()


class TurboStreamTests(unittest.TestCase):
    def payloads(self, lines, split_at=None):
        page = _ShareHTML()
        page.feed(stream_page(lines, split_at=split_at))
        return list(_json_payloads(page))

    def test_reference_keys_and_primitive_indexes(self):
        pool = Pool()
        pool.add({"title": "标题", "enabled": True, "count": 12, "nullable": None})
        self.assertEqual(_unflatten(pool.values), {"title": "标题", "enabled": True, "count": 12, "nullable": None})

    def test_global_promise_append_offset_and_reused_prior_strings(self):
        pool = Pool()
        pool.add({"title": "递归方法", "data": Promise(73)})
        initial = list(pool.values)
        start = len(pool.values)
        pool.add(conversation())
        deferred = pool.values[start:]
        root = self.payloads([json.dumps(initial), "P73:" + json.dumps(deferred)])[0]
        self.assertEqual(root["data"], conversation())
        self.assertEqual(root["title"], "递归方法")

    def test_enqueue_chunks_may_split_one_json_line(self):
        pool = Pool()
        pool.add(conversation())
        decoded = self.payloads([json.dumps(pool.values)], split_at=37)
        self.assertEqual(decoded, [conversation()])

    def test_z_references_previously_encoded_global_value(self):
        pool = Pool()
        pool.add({"original": "可复用知识", "resolved": Promise(91)})
        initial = list(pool.values)
        root = self.payloads([json.dumps(initial), "P91:" + json.dumps([["Z", pool.strings["可复用知识"]]])])[0]
        self.assertEqual(root["resolved"], root["original"])

    def test_positive_integer_promise_payload_reuses_existing_index(self):
        pool = Pool()
        pool.add({"original": "资料", "resolved": Promise(92)})
        root = self.payloads([json.dumps(pool.values), "P92:" + str(pool.strings["资料"])])[0]
        self.assertEqual(root["resolved"], "资料")

    def test_negative_integer_promise_payload_adds_no_pool_slots(self):
        pool = Pool()
        pool.add({"empty": Promise(71), "later": Promise(72)})
        initial = list(pool.values)
        start = len(pool.values)
        pool.add({"title": "后续数据"})
        root = self.payloads([json.dumps(initial), "P71:-7", "P72:" + json.dumps(pool.values[start:])])[0]
        self.assertIsNone(root["empty"])
        self.assertEqual(root["later"], {"title": "后续数据"})

    def test_error_array_still_preserves_global_append_offset(self):
        pool = Pool()
        pool.add({"failed": Promise(31), "later": Promise(32)})
        initial = list(pool.values)
        error_start = len(pool.values)
        pool.add({"error": "not public"})
        error_data = pool.values[error_start:]
        success_start = len(pool.values)
        pool.add({"answer": "正确答案"})
        root = self.payloads([json.dumps(initial), "E31:" + json.dumps(error_data), "P32:" + json.dumps(pool.values[success_start:])])[0]
        self.assertIsInstance(root["failed"], _UnresolvedPromise)
        self.assertEqual(root["later"], {"answer": "正确答案"})

    def test_full_stream_conversation_parses_through_reader(self):
        pool = Pool()
        pool.add({"loaderData": Promise(101)})
        initial = list(pool.values)
        start = len(pool.values)
        pool.add({"renamed-route": {"serverResponse": {"data": conversation()}}})
        html = stream_page([json.dumps(initial), "P101:" + json.dumps(pool.values[start:])], split_at=67)
        with patch("share_links.fetch_public", return_value=(URL, html, "text/html")):
            result = read_shared_conversation(URL)
        self.assertEqual(len(result["messages"]), 2)

    def test_missing_message_promise_rejects_partial_conversation(self):
        pool = Pool()
        payload = conversation()
        payload["linear_conversation"].append({"message": Promise(77)})
        pool.add(payload)
        html = stream_page([json.dumps(pool.values)])
        with patch("share_links.fetch_public", return_value=(URL, html, "text/html")):
            with self.assertRaisesRegex(ShareLinkError, "部分消息"):
                read_shared_conversation(URL)

    def test_missing_promise_cannot_fall_back_to_partial_author_html(self):
        pool = Pool()
        payload = conversation()
        payload["linear_conversation"].append({"message": Promise(77)})
        pool.add(payload)
        html = stream_page([json.dumps(pool.values)]) + '<div data-message-author-role="assistant">只有网页上这一段</div>'
        with patch("share_links.fetch_public", return_value=(URL, html, "text/html")):
            with self.assertRaisesRegex(ShareLinkError, "部分消息"):
                read_shared_conversation(URL)

    def test_empty_bad_duplicate_and_unknown_promise_records_are_rejected(self):
        initial = json.dumps([{"_1": 2}, "data", ["P", 9]])
        bad_lines = [
            ["[]"], [initial, "P9:[]"], [initial, "E9:[]"],
            [initial, "P9:["], [initial, "P9:-7", "P9:-7"],
            [initial, "P10:-7"], [initial, "unsupported:[]"],
        ]
        for lines in bad_lines:
            with self.subTest(lines=lines):
                with self.assertRaises(ShareLinkError):
                    self.payloads(lines)


if __name__ == "__main__":
    unittest.main()
