"""Regression coverage for Bennettsville dispatch routing; no network or writes."""
import unittest
from unittest.mock import patch
import graph_import as graph
import operations_intake as intake


class BennettsvilleIntakeTests(unittest.TestCase):
    def message(self, prefix='Fw:'):
        return {'subject': prefix+' [EXTERNAL] FW: PID 7043977 - 1644 PEELE RD BENNETTSVILLE, SC 29512',
                'body': {'content': 'Spectrum job: activate a second port of light.\nSignature: Florence, SC'}}

    def test_assignment_with_ntp_routes_to_florence(self):
        document = ('NTP.pdf', 'Work Order #: 3738002\nPRISM ID: 7043977\nEst. Completion: 11/02/2026\n1644 PEELE RD\nBENNETTSVILLE, SC 29512')
        with patch.object(graph, '_candidate_document_texts', return_value=[document]):
            item = intake.parse(self.message(), [])
        self.assertEqual((item['pid'], item['wo'], item['city'], item['market'], item['ntp_due']),
                         ('7043977', '3738002', 'Bennettsville', 'Florence', '2026-11-02'))

    def test_reply_without_ntp_uses_subject_city(self):
        with patch.object(graph, '_candidate_document_texts', return_value=[]):
            item = intake.parse(self.message('Re:'), [])
        self.assertEqual((item['pid'], item['address'], item['city'], item['market']),
                         ('7043977', '1644 PEELE RD', 'Bennettsville', 'Florence'))
        self.assertEqual(item['ntp_due'], '')

    def test_unmapped_city_does_not_inherit_signature_area(self):
        message = self.message()
        message['subject'] = message['subject'].replace('BENNETTSVILLE', 'UNKNOWNVILLE')
        with patch.object(graph, '_candidate_document_texts', return_value=[]):
            item = intake.parse(message, [])
        self.assertEqual(item['market'], '')

    def test_existing_city_routes_preserved(self):
        self.assertEqual(graph._market_for('Bennettsville', ''), 'Florence')
        self.assertEqual(graph._market_for('Columbia', ''), 'Columbia')
        self.assertEqual(graph._market_for('Florence', ''), 'Florence')


if __name__ == '__main__':
    unittest.main()
