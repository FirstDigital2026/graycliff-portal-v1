import unittest
from unittest.mock import patch
import graph_import as graph
import operations_intake as intake
import intake_formats as formats

class WillowTests(unittest.TestCase):
    def test_forwarded_daytime_cutover_invitation(self):
        body = '''-----Original Appointment-----
From: Dispatch <dispatch@example.invalid>
Subject: [EXTERNAL] Cutover Willow Trace
When: Tuesday, November 3, 2026 9:00 AM-11:00 AM (UTC-05:00) Eastern Time (US & Canada).
Where: 1408 Willow Trace Dr (1408 Willow Trace Dr, Florence, South Carolina 29501)
Graycliff please make sure to open and close CRQ ticket with ROC!!!
'''
        msg = self.message(body)
        msg['subject'] = 'FW: [EXTERNAL] Cutover Willow Trace'
        with patch.object(graph, '_candidate_document_texts', return_value=[]):
            item = intake.parse(msg, [])
        self.assertEqual((item['type'], item['planned_date'], item['work_shift'], item['city']), ('CRQ', '2026-11-03', 'Day', 'Florence'))
        for prefix in ('Canceled: ', 'Tentative: '):
            msg['subject'] = prefix + 'Cutover Willow Trace'
            self.assertEqual(formats.current_crq(msg, body), {})
        msg['subject'] = 'FW: Cutover Willow Trace'
        self.assertEqual(formats.current_crq(msg, 'Please review this old invitation.\n' + body), {})

    def message(self, body=None):
        return {'subject':'FW: [EXTERNAL] 1408 Willow Trace Florence SC 29501','receivedDateTime':'2026-11-02T18:02:12Z','body':{'content':body or 'Jason,\nA daytime CRQ has been set up and approved on this one for tomorrow 11/3.\nThanks,\nSignature\nFrom: older@example.invalid\nOld instruction'}}
    def test_approved_daytime_address_only_dispatch(self):
        with patch.object(graph,'_candidate_document_texts',return_value=[]):
            item=intake.parse(self.message(),[])
        self.assertEqual((item['address'],item['city'],item['market'],item['type'],item['planned_date'],item['work_shift']),('1408 Willow Trace','Florence','Florence','CRQ','2026-11-03','Day'))
    def test_quoted_approval_cannot_create_dispatch(self):
        msg=self.message('Please review.\nFrom: older@example.invalid\nA daytime CRQ approved tomorrow 11/3.')
        self.assertEqual(formats.current_crq(msg,msg['body']['content']),{})
    def test_pending_is_not_approved(self):
        msg=self.message('CRQ set up but not approved tomorrow 11/3.')
        self.assertEqual(formats.current_crq(msg,msg['body']['content']),{})
    def test_conflicting_relative_date_not_scheduled(self):
        msg=self.message('Daytime CRQ approved tomorrow 11/4.')
        self.assertNotIn('planned_date',formats.current_crq(msg,msg['body']['content']))
    def test_year_rollover(self):
        msg=self.message('Daytime CRQ approved tomorrow 1/1.');msg['receivedDateTime']='2026-12-31T19:00:00Z'
        self.assertEqual(formats.current_crq(msg,msg['body']['content'])['planned_date'],'2027-01-01')

if __name__=='__main__':unittest.main()
