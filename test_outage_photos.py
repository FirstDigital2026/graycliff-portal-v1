import unittest,hashlib,base64
from email import policy
from email.parser import BytesParser
import mail_automation as m

RAW=b'\xff\xd8synthetic-jpeg-fixture\xff\xd9'
PHOTO={'id':'900','sheetId':'11','name':'Outage-update-'+'a'*24+'.jpg','mime':'image/jpeg','sha256':hashlib.sha256(RAW).hexdigest()}
EVENT={'eventId':'12345678-1234-1234-1234-123456789012','jobKey':'florence:123','sheetId':'11','rowId':'123','status':'Working','note':'Splicing underway.','job':{'type':'Outage','title':'Synthetic test location'},'photos':[PHOTO]}
ROUTE={'subject':'Outage test','to':['test@example.com'],'cc':['coordinator@example.com'],'messageId':'<source@example.com>'}
class Store:
    def list_row_attachments(self,*args):return [{'id':900,'name':PHOTO['name']}]
    def download_attachment(self,*args):return RAW,PHOTO['name'],'image/jpeg'

class OutagePhotoTests(unittest.TestCase):
    def test_inline_picture_preserves_thread_comments_and_signature(self):
        draft=m.attach_update_photos(m.draft_event(EVENT,ROUTE),EVENT,Store())
        msg=BytesParser(policy=policy.default).parsebytes(m.mime_message(draft))
        pictures=[p for p in msg.walk() if p.get_content_type()=='image/jpeg']
        self.assertEqual(len(pictures),1);self.assertEqual(pictures[0].get_payload(decode=True),RAW)
        self.assertEqual(pictures[0].get_content_disposition(),'inline');self.assertIn('cid:'+str(pictures[0]['Content-ID']).strip('<>'),draft['html'])
        self.assertEqual(msg['In-Reply-To'],'<source@example.com>');self.assertEqual(str(msg['Cc']),'coordinator@example.com')
        self.assertIn('Splicing underway.',draft['html']);self.assertIn('First Digital Operations',draft['html'])
    def test_completion_retains_findings_and_billing_message(self):
        e={**EVENT,'status':'Field complete'};d=m.attach_update_photos(m.draft_event(e,ROUTE),e,Store())
        self.assertTrue(d['text'].startswith('Hands off — job complete'));self.assertIn('billing preparation',d['text']);self.assertIn('Splicing underway.',d['text'])
    def test_no_photo_does_not_change_existing_emails(self):
        e={**EVENT,'photos':[]};d=m.draft_event(e,ROUTE);self.assertEqual(m.attach_update_photos(d,e,Store()),d)
    def test_wrong_job_changed_photo_and_extra_selection_are_blocked(self):
        for e in ({**EVENT,'job':{'type':'CRQ'}},{**EVENT,'photos':[PHOTO,PHOTO]},{**EVENT,'photos':[{**PHOTO,'sheetId':'22'}]},{**EVENT,'photos':[{**PHOTO,'id':'901'}]},{**EVENT,'photos':[{**PHOTO,'sha256':'0'*64}]}):
            with self.assertRaises(m.ThreadReview):m.attach_update_photos(m.draft_event(EVENT,ROUTE),e,Store())
    def test_oversized_photo_is_not_silently_omitted(self):
        class Large(Store):
            def download_attachment(self,*args):return b'\xff\xd8'+b'x'*1048576,'large.jpg','image/jpeg'
        with self.assertRaises(m.ThreadReview):m.attach_update_photos(m.draft_event(EVENT,ROUTE),EVENT,Large())

if __name__=='__main__':unittest.main()
