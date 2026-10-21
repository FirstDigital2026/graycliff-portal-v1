import sqlite3, uuid, unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import Mock, patch
import intake_receipt as receipt
import mail_automation as mail

class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(__file__).parent/('qa-receipt-'+uuid.uuid4().hex);self.tmp.mkdir();self.path=self.tmp/'test.sqlite';self.key='a'*64;self.job='columbia:99'
        @contextmanager
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
            try:
                with c:yield c
            finally:c.close()
        self.db=db;mail.initialize(db)
        with db() as c:c.execute("INSERT INTO mail_bridge_meta VALUES('live_after','2000-01-01T00:00:00+00:00')")
        self.store=Mock();self.store.get_sheet.return_value={'columns':[{'id':1,'title':'Portal Intake Key'},{'id':2,'title':'Task Name'}],'rows':[{'id':99,'cells':[{'columnId':1,'value':self.key},{'columnId':2,'value':'901 Fictional St'}]}]}
        self.route={'subject':'New job','to':['dispatch@example.invalid'],'cc':[],'messageId':'<fictional@example.invalid>','references':[]}
    def tearDown(self):self.path.unlink(missing_ok=True);self.tmp.rmdir()

    def test_existing_job_without_creation_receipt_sends_nothing(self):
        with patch.object(receipt.graph,'_request') as send:
            self.assertIsNone(receipt.finish(self.store,self.db,'fake',self.job,self.key));send.assert_not_called()

    def test_one_receipt_with_original_thread_and_company_signature(self):
        receipt.reserve(self.db,self.job,self.key)
        with patch.object(mail,'resolve_thread',return_value=self.route),patch.object(receipt.graph,'_request') as send:
            self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'ACCEPTED')
            self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'ACCEPTED');self.assertEqual(send.call_count,1)
        with self.db() as c:
            import json
            draft=json.loads(c.execute('SELECT draft FROM intake_receipts').fetchone()[0])
        self.assertEqual(draft['parent'],self.route['messageId']);self.assertIn('received this job',draft['text']);self.assertIn('First Digital Operations',draft['text'])
        self.assertNotIn('completed',draft['text']);self.assertNotIn('technician',draft['text'])

    def test_uncertain_send_never_retries(self):
        receipt.reserve(self.db,self.job,self.key)
        with patch.object(mail,'resolve_thread',return_value=self.route),patch.object(receipt.graph,'_request',side_effect=TimeoutError) as send:
            self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'UNCERTAIN')
            self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'UNCERTAIN');self.assertEqual(send.call_count,1)

    def test_unverified_thread_does_not_block_job_or_guess_recipient(self):
        receipt.reserve(self.db,self.job,self.key)
        with patch.object(mail,'resolve_thread',side_effect=mail.ThreadReview('No verified source')),patch.object(receipt.graph,'_request') as send:
            self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'NEEDS_REVIEW');send.assert_not_called()

    def test_paused_receipt_does_not_release_later(self):
        receipt.reserve(self.db,self.job,self.key)
        with patch.object(mail,'SEND_ARMED',False):self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'PAUSED')
        with patch.object(receipt.graph,'_request') as send:
            self.assertEqual(receipt.finish(self.store,self.db,'fake',self.job,self.key),'PAUSED');send.assert_not_called()

    def test_revision_cannot_retarget_creation_receipt(self):
        receipt.reserve(self.db,self.job,self.key);receipt.reserve(self.db,self.job,'b'*64)
        with patch.object(receipt.graph,'_request') as send:
            self.assertIsNone(receipt.finish(self.store,self.db,'fake',self.job,'b'*64));send.assert_not_called()

if __name__=='__main__':unittest.main()
