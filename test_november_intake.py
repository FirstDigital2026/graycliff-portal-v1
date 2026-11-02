import io
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
import graph_import as graph
import operations_intake as intake


class NovemberIntakeTests(unittest.TestCase):
    def test_hamer_work_order_routes_without_signature(self):
        document = ('WO_3671333_1.pdf', 'Work Order #: 3671333\nPRISM ID: 6948528\nEst. Completion: 09/30/2026\n659 RED RD\nHAMER, SC 29547')
        message = {'subject': 'FW: PO Number for NTP 3671333 PRISM 6948528 Created/Revised.', 'body': {'content': 'Ready for splicing.'}}
        with patch.object(graph, '_candidate_document_texts', return_value=[document]):
            result = intake.parse(message, [])
        self.assertEqual((result['market'], result['city'], result['pid'], result['wo']), ('Florence', 'Hamer', '6948528', '3671333'))

    def response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"value": []}'
        response.headers = {'Content-Type': 'application/json'}
        return response

    def error(self, status, headers=None):
        return urllib.error.HTTPError('https://graph.microsoft.com/v1.0/test', status, 'test', headers or {}, io.BytesIO(b'Unavailable'))

    @patch.object(graph.time, 'sleep')
    def test_transient_attachment_read_is_retried(self, sleep):
        with patch.object(graph.urllib.request, 'urlopen', side_effect=[self.error(503), self.response()]) as request:
            self.assertEqual(graph._request('GET', graph.GRAPH_ROOT+'/test'), {'value': []})
        self.assertEqual(request.call_count, 2)

    @patch.object(graph.time, 'sleep')
    def test_send_is_never_replayed(self, sleep):
        with patch.object(graph.urllib.request, 'urlopen', side_effect=self.error(503)) as request:
            with self.assertRaises(graph.GraphImportError):
                graph._request('POST', graph.GRAPH_ROOT+'/test')
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    @patch.object(graph.time, 'sleep')
    def test_auth_error_is_not_retried(self, sleep):
        with patch.object(graph.urllib.request, 'urlopen', side_effect=self.error(403)) as request:
            with self.assertRaises(graph.GraphImportError):
                graph._request('GET', graph.GRAPH_ROOT+'/test')
        self.assertEqual(request.call_count, 1)

    @patch.object(graph.time, 'sleep')
    def test_retries_are_bounded(self, sleep):
        with patch.object(graph.urllib.request, 'urlopen', side_effect=lambda *a, **k: (_ for _ in ()).throw(self.error(503))) as request:
            with self.assertRaises(graph.GraphImportError):
                graph._request('GET', graph.GRAPH_ROOT+'/test')
        self.assertEqual(request.call_count, 3)

    @patch.object(graph.time, 'sleep')
    def test_long_throttle_is_preserved(self, sleep):
        with patch.object(graph.urllib.request, 'urlopen', side_effect=self.error(429, {'Retry-After': '120'})):
            with self.assertRaises(graph.GraphImportError):
                graph._request('GET', graph.GRAPH_ROOT+'/test')
        sleep.assert_not_called()
