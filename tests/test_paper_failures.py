"""Regression checks for the IEEE link failure and error propagation."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from paper_summary import PaperError, _fetch_paper, read_paper
from runtime_control import CancellationToken
from subagents import run_business_subagent
from web_research import PublicFetchError

URL = "https://ieeexplore.ieee.org/document/11029963"


class PaperFailureTests(unittest.TestCase):
    def test_http_202_preserves_reason_and_alternative_inputs(self):
        with patch("paper_summary.fetch_public", side_effect=PublicFetchError(202)):
            with self.assertRaises(PaperError) as raised:
                _fetch_paper(URL, CancellationToken())
        self.assertIn("HTTP 202", str(raised.exception))
        self.assertIn("PDF", str(raised.exception))
        self.assertFalse(raised.exception.retryable)

    def test_challenge_page_is_not_paper_material(self):
        page = b"<html><title>JavaScript is disabled</title><body>In order to continue, we need to verify that you're not a robot. Enable JavaScript and then reload the page.</body></html>"
        with patch("paper_summary._fetch_paper", return_value=(URL, page, "text/html")):
            with self.assertRaisesRegex(PaperError, "访问验证页面"):
                read_paper({"url": URL}, None, CancellationToken(), lambda _: None)

    def test_researchr_reads_only_the_actual_abstract(self):
        url = 'https://conf.researchr.org/details/icse-2025/research-track/10/Test-Article'
        abstract = 'A software engineering paper examines whether a program change makes a comment stale. ' * 4
        page = ('<head><title>Test Article</title></head>' + '<div>Conference session registration</div>' * 540
                + '<div class="row"><label class="col-sm-2 control-label">Abstract</label>'
                + '<div class="col-sm-10"><p>' + abstract + '</p></div></div>'
                + '<div>Another talk in the program</div>')
        with patch('paper_summary._fetch_paper', return_value=(url, page.encode(), 'text/html')):
            result = read_paper({'url': url}, None, CancellationToken(), lambda _: None)

        self.assertEqual(result['text'], abstract.strip())
        self.assertEqual(result['kind'], 'abstract')
        self.assertIn('仅基于会议公开摘要', result['scope'])
        self.assertEqual(result['url'], url)

    def test_researchr_program_without_abstract_is_not_paper_material(self):
        url = 'https://conf.researchr.org/details/icse-2025/research-track/10/Test-Article'
        page = ('<html><title>Program</title><div>Conference session registration</div>' * 40).encode()
        with patch('paper_summary._fetch_paper', return_value=(url, page, 'text/html')):
            with self.assertRaisesRegex(PaperError, '摘要'):
                read_paper({'url': url}, None, CancellationToken(), lambda _: None)

    def test_page_without_main_does_not_count_parser_newlines_as_text(self):
        url = 'https://example.org/abstract'
        body = 'The experimental setup assesses feedback under an explicit time budget. ' * 4
        page = ('<html><body>' + '<div></div>' * 540 + '<p>' + body + '</p></body></html>').encode()
        with patch('paper_summary._fetch_paper', return_value=(url, page, 'text/html')):
            result = read_paper({'url': url}, None, CancellationToken(), lambda _: None)

        self.assertEqual(result['text'], body.strip())
        self.assertEqual(result['kind'], 'page')

    def harness(self, errors, rounds):
        shared = SimpleNamespace(settings=SimpleNamespace(api_key=""), cancellation=CancellationToken(),
                                 model=Mock(), trace=Mock(), execute_tool=Mock(side_effect=errors))
        calls = ["produce_result", "inspect_result"][:rounds]
        shared.model.chat.side_effect = [{"role": "assistant", "tool_calls": [
            {"id": "call_" + str(i), "function": {"name": name, "arguments": "{}"}}]}
            for i, name in enumerate(calls)]
        harness = SimpleNamespace(shared=shared, title="论文总结", max_rounds=rounds, time_budget=30,
            tools=SimpleNamespace(metadata=lambda: []), instructions=lambda: "处理论文", task_description=lambda: {}, finished=False)
        return harness

    def test_terminal_failure_stops_after_one_model_round(self):
        harness = self.harness([PaperError("HTTP 202，请上传 PDF")], 2)
        with patch("subagents.registry.owner", return_value=SimpleNamespace(id="paper_reading", operations={"paper_summary": lambda *_: harness})):
            with self.assertRaisesRegex(PaperError, "HTTP 202"):
                run_business_subagent("paper_summary", {}, SimpleNamespace(settings=SimpleNamespace()))
        self.assertEqual(harness.shared.model.chat.call_count, 1)
        self.assertEqual(harness.shared.execute_tool.call_count, 1)

    def test_followup_inspection_error_does_not_hide_production_error(self):
        harness = self.harness([PublicFetchError(502), ValueError("还没有成果")], 2)
        with patch("subagents.registry.owner", return_value=SimpleNamespace(id="paper_reading", operations={"paper_summary": lambda *_: harness})):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                run_business_subagent("paper_summary", {}, SimpleNamespace(settings=SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
