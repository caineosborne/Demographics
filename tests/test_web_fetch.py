import unittest
from unittest.mock import MagicMock, patch
from typing import TypedDict

import requests
from langgraph.graph import StateGraph, START, END

from data.tools import PageAccessError, get_page_text, get_pdf_text, fetch_with_playwright


class FetchTests(unittest.TestCase):
    def response(self, html):
        response = MagicMock()
        response.content = html
        response.headers = {}
        response.__enter__.return_value = response
        return response

    def test_requests_success_skips_browser(self):
        with patch('data.tools.requests.get', return_value=self.response(
            b'<body><nav>Menu</nav><main>Population 100<script>ignore</script></main></body>'
        )), patch('data.tools.fetch_with_playwright') as browser:
            self.assertEqual(get_page_text.invoke({'url': 'https://example.test'}), 'Population 100')
            browser.assert_not_called()

    def test_requests_keeps_article_text_outside_main(self):
        html = b'<body><main>Navigation shell</main><section><article>National births were 100.</article></section></body>'
        with patch('data.tools.requests.get', return_value=self.response(html)), patch('data.tools.fetch_with_playwright') as browser:
            self.assertIn('National births were 100.', get_page_text.invoke({'url': 'https://example.test'}))
            browser.assert_not_called()

    def test_request_errors_fall_back(self):
        for error in (requests.Timeout('timeout'), requests.ConnectionError('offline'), requests.HTTPError('403')):
            with self.subTest(error=error), patch('data.tools.requests.get', side_effect=error), patch(
                'data.tools.fetch_with_playwright', return_value='Rendered population 100'
            ) as browser:
                self.assertEqual(get_page_text.invoke({'url': 'https://example.test'}), 'Rendered population 100')
                browser.assert_called_once_with('https://example.test')

    def test_unusable_html_falls_back(self):
        for html in (b'', b'<body><script>render()</script></body>', b'<body>Enable JavaScript</body>', b'<body>Access denied</body>'):
            with self.subTest(html=html), patch('data.tools.requests.get', return_value=self.response(html)), patch(
                'data.tools.fetch_with_playwright', return_value='Rendered text'
            ):
                self.assertEqual(get_page_text.invoke({'url': 'https://example.test'}), 'Rendered text')

    def test_both_fail(self):
        with patch('data.tools.requests.get', side_effect=requests.Timeout('request timeout')), patch(
            'data.tools.fetch_with_playwright', side_effect=RuntimeError('browser unavailable')
        ), self.assertRaisesRegex(PageAccessError, 'request timeout.*browser unavailable'):
            get_page_text.invoke({'url': 'https://example.test'})

    def test_live_graph_status_stream(self):
        class State(TypedDict):
            text: str
        builder = StateGraph(State)
        builder.add_node('fetch', lambda state: {'text': get_page_text.invoke({'url': 'https://example.test'})})
        builder.add_edge(START, 'fetch')
        builder.add_edge('fetch', END)
        with patch('data.tools.requests.get', side_effect=requests.Timeout('timeout')), patch(
            'data.tools.fetch_with_playwright', return_value='Rendered text'
        ):
            stream = builder.compile().stream({'text': ''}, stream_mode=['custom', 'values'])
            events = list(stream)
        statuses = [event['fetch_status'] for mode, event in events if mode == 'custom']
        self.assertEqual(statuses, ['Trying Requests', 'Trying Playwright', 'Page loaded via Playwright — summarising'])
        self.assertEqual(events[-1][1]['text'], 'Rendered text')

    def test_browser_cleanup_on_failure(self):
        with patch('playwright.sync_api.sync_playwright') as start:
            browser = start.return_value.__enter__.return_value.chromium.launch.return_value
            browser.new_page.return_value.goto.side_effect = RuntimeError('navigation failed')
            with self.assertRaisesRegex(RuntimeError, 'navigation failed'):
                fetch_with_playwright('https://example.test')
            browser.close.assert_called_once()

    def test_pdf_url_is_extracted_with_requests_without_browser(self):
        response = self.response(b'%PDF-1.7 example')
        page = MagicMock()
        page.extract_text.return_value = 'Japan population report'
        reader = MagicMock()
        reader.is_encrypted = False
        reader.pages = [page]
        with patch('data.tools.requests.get', return_value=response), patch('data.tools.PdfReader', return_value=reader), patch('data.tools.fetch_with_playwright') as browser:
            self.assertEqual(get_page_text.invoke({'url': 'https://example.test/report.pdf'}), 'Japan population report')
            browser.assert_not_called()

    def test_dedicated_pdf_tool_extracts_document_text(self):
        response = self.response(b'%PDF-1.7 example')
        page = MagicMock()
        page.extract_text.return_value = 'Monthly demographic report'
        reader = MagicMock()
        reader.is_encrypted = False
        reader.pages = [page]
        with patch('data.tools.requests.get', return_value=response), patch('data.tools.PdfReader', return_value=reader):
            self.assertEqual(get_pdf_text.invoke({'url': 'https://example.test/report.pdf'}), 'Monthly demographic report')


if __name__ == '__main__':
    unittest.main()
