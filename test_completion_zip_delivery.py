import base64,json,os,sqlite3,tempfile,unittest
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from unittest.mock import patch
import billing_transport as transport
import mail_automation as mail


class ZipDelivery(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=os.path.join(self.folder.name,'test.db')
        @contextmanager
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
            try:
                with c:yield c
            finally:c.close()
        self.db=db;mail.initialize(db)
        self.raw=os.urandom(3*1024*1024)
        self.event={'eventId':'00000000-0000-0000-0000-000000000001','at':mail.now(),'action':'status','status':'Field complete','committed':True,'jobKey':'florence:123','sheetId':'1','rowId':'123','job':{'type':'Standard'}}
        self.draft={'eventId':self.event['eventId'],'to':['test@example.invalid'],'cc':[],'subject':'Re: Test','parent':'<original@example.invalid>','references':['<original@example.invalid>'],'text':'Complete','html':'<p>Complete</p>','attachments':[{'name':'Field-photos.zip','mime':'application/zip','content':base64.b64encode(self.raw).decode()}]}
        with db() as c:
            c.execute("INSERT INTO mail_event_outbox(event_id,seq,job_key,payload,state,created_at,draft,approved_at) VALUES(?,1,?,?,'READY',?,?,?)",(self.event['eventId'],self.event['jobKey'],json.dumps(self.event),mail.now(),json.dumps(self.draft),mail.now()))
            c.execute("INSERT INTO mail_bridge_meta VALUES('live_after','2000-01-01T00:00:00+00:00')")
        self.store=type('Store',(),{'get_sheet':lambda *a,**k:{'columns':[{'id':1,'title':'Status'}],'rows':[{'id':123,'cells':[{'columnId':1,'value':'Complete'}]}]}})()
        self.armed=patch.object(mail,'SEND_ARMED',True);self.armed.start();self.addCleanup(self.armed.stop)
    def state(self):
        with self.db() as c:return c.execute('SELECT state FROM mail_event_outbox').fetchone()[0]
    def test_large_zip_uses_verified_upload_and_preserves_thread(self):
        with patch.object(transport,'send_large') as send,patch.object(mail.graph,'_request',side_effect=AssertionError('Direct email must not run')):
            mail.dispatch(self.db,self.event['eventId'],self.store)
            args=send.call_args.args;message=BytesParser(policy=policy.default).parsebytes(args[2])
            self.assertEqual(args[3][0]['data'],self.raw)
            self.assertEqual(message['In-Reply-To'],'<original@example.invalid>')
            self.assertFalse(list(message.iter_attachments()))
            self.assertEqual(self.state(),'ACCEPTED')
            with self.assertRaises(RuntimeError):mail.dispatch(self.db,self.event['eventId'],self.store)
            send.assert_called_once()
    def test_failed_upload_is_not_an_uncertain_send(self):
        with patch.object(transport,'send_large',side_effect=transport.BeforeSendFailure('Upload failed')):
            with self.assertRaises(transport.BeforeSendFailure):mail.dispatch(self.db,self.event['eventId'],self.store)
        self.assertEqual(self.state(),'NEEDS_ATTENTION')
    def test_send_timeout_is_held_against_duplicates(self):
        with patch.object(transport,'send_large',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):mail.dispatch(self.db,self.event['eventId'],self.store)
        self.assertEqual(self.state(),'UNCERTAIN')
        mail.retry_photo_size_holds(self.db);self.assertEqual(self.state(),'UNCERTAIN')
    def test_old_size_failure_rebuilds_only_if_definitely_unsent(self):
        with self.db() as c:c.execute("UPDATE mail_event_outbox SET state='NEEDS_THREAD_REVIEW',detail=?",('Field photos need a smaller email package. Office review required; no photos were omitted.',))
        mail.retry_photo_size_holds(self.db);self.assertEqual(self.state(),'NEW')
        with self.db() as c:c.execute("UPDATE mail_event_outbox SET state='NEEDS_THREAD_REVIEW',sent_at=?",(mail.now(),))
        mail.retry_photo_size_holds(self.db);self.assertEqual(self.state(),'NEEDS_THREAD_REVIEW')

if __name__=='__main__':unittest.main()
