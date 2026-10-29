import unittest,sqlite3,json,uuid,time
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
from email.message import EmailMessage
from pathlib import Path
import financial_workflow as w
import billing_closeout as b
from test_financial_spreadsheets import workbook

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.path=Path(__file__).parent/('qa-workflow-'+str(uuid.uuid4())+'.sqlite');w.initialize(self.db)
    def tearDown(self):self.path.unlink(missing_ok=True)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def test_failure_and_stale_inbox_are_visible_without_mime(self):
        w.record(self.db,'key','Issued billing','Needs attention','Check unique match','Billing')
        w.monitor(self.db,True);r=w.processing(self.db,now=time.time()+301)
        self.assertEqual(r['attentionCount'],2);self.assertTrue(r['monitorStale']);self.assertNotIn('mime',json.dumps(r))
    def test_short_missing_and_source_gap_are_distinct(self):
        week={'week':'2026-01-03','projectCode':'P','flagged':True,'differenceCents':400,'amountCents':1000,'reportedCents':600,'status':'Short','remittances':[{'invoiceRef':'P-010326'}]}
        r=w.collections({'tracking':{'weeks':[week,{**week,'week':'2026-01-10','remittances':[],'overdueDays':5}], 'unmatchedRemittances':[{'projectCode':'P','amountCents':900}] }},'Spectrum')
        self.assertEqual(r['outstandingCents'],800);self.assertEqual(r['actionCount'],2);self.assertEqual(r['evidenceGapCount'],1)
        self.assertIsNone(r['items'][-1]['outstandingCents'])
    def test_recap_code_difference_even_when_amount_matches(self):
        r=w.collections({},'Spectrum',{'rows':[{'status':'Difference to review','differenceCents':0,'ourAmountCents':100,'recapAmountCents':100,'detail':'FS01: billed 2, recap 1'}]})
        self.assertEqual(r['actionCount'],1);self.assertEqual(r['outstandingCents'],0)
    def source(self):
        m=EmailMessage();m['Date']='Wed, 28 Oct 2026 13:00:00 -0400';m['From']='jason.mosqueda@firstdigitalsc.com';m['To']='customer@graycliffent.com';m.set_content('Billing attached.')
        m.add_attachment(workbook(),maintype='application',subtype='octet-stream',filename='Work Completed Spreadsheet (10 Main St).xlsx');return m.as_bytes()
    def message(self):return {'id':'mail','subject':'Billing 10 Main St','from':{'emailAddress':{'address':'jason.mosqueda@firstdigitalsc.com'}},'toRecipients':[{'emailAddress':{'address':'customer@graycliffent.com'}}],'body':{'content':'Billing attached.'},'receivedDateTime':'2026-10-28T17:00:00Z'}
    def job(self,status='Field complete'):return {'title':'10 Main Street','sourceKey':'123:456','sheetId':'123','rowId':'456','jobKey':'columbia:456','jobStatus':status}
    def test_verified_closeout_retention_before_bridge_and_replay(self):
        def bridge(*args):
            self.assertEqual(args[3]['jobKey'],'columbia:456')
            with self.db() as c:self.assertIsNotNone(c.execute('SELECT mime FROM private_billing_closeout_messages').fetchone())
            return {'ok':True}
        with patch('graph_import.get_message_mime',return_value=self.source()),patch.object(b,'jobs',return_value=[self.job()]),patch('mail_automation.bridge',side_effect=bridge) as call:
            self.assertEqual(b.ingest(None,self.db,self.message(),[],'a'*64,'token')['state'],'Completed')
            b.ingest(None,self.db,self.message(),[],'a'*64,'token');self.assertEqual(call.call_count,1)
        with self.db() as c:r=json.loads(c.execute('SELECT snapshot FROM financial_spreadsheet_imports').fetchone()[0])
        self.assertEqual(r['amountCents'],38290);self.assertEqual(r['sheetId'],'123')
    def test_ambiguous_and_unfinished_jobs_never_close(self):
        for jobs in ([self.job(),self.job()], [self.job('Working')],[]):
            with patch('graph_import.get_message_mime',return_value=self.source()),patch.object(b,'jobs',return_value=jobs),patch('mail_automation.bridge') as call:
                with self.assertRaises(ValueError):b.ingest(None,self.db,self.message(),[],'b'*64,'token')
                call.assert_not_called()
    def test_no_customer_and_test_mail_never_close(self):
        for m in ({**self.message(),'toRecipients':[]},{**self.message(),'subject':'TEST billing'}):
            with self.assertRaises(ValueError):b.validate_message(m,self.source())
    def test_conflicting_amount_never_closes(self):
        b.initialize(self.db)
        with self.db() as c:c.execute('INSERT INTO financial_spreadsheet_imports VALUES (?,?,?)',('123:456',json.dumps({'amountCents':100}),0))
        with patch('graph_import.get_message_mime',return_value=self.source()),patch.object(b,'jobs',return_value=[self.job()]),patch('mail_automation.bridge') as call:
            with self.assertRaises(ValueError):b.ingest(None,self.db,self.message(),[],'c'*64,'token')
            call.assert_not_called()
    def test_portal_keys_and_canonical_financial_keys_are_distinct(self):
        store=MagicMock()
        def sheet(sid,force=False):
            return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Status'}],'rows':[{'id':456,'cells':[{'columnId':1,'value':'10 Main St'},{'columnId':2,'value':'Complete'}]}]} if sid==8067039099834244 else {'columns':[],'rows':[]}
        store.get_sheet.side_effect=sheet
        item=b.jobs(store)[0];self.assertEqual(item['jobKey'],'columbia:456');self.assertEqual(item['sourceKey'],'8067039099834244:456')

if __name__=='__main__':unittest.main()
