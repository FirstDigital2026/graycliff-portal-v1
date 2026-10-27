import io,json,sqlite3,uuid,unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch,MagicMock
import billing_recaps as r

TEXT='''Payroll Recaps
Week Ending: 10/31/2026
95603000 - COLUMBIA, SC
Emp # 127675
First Digital
Daily No 1234
70-FS01+ SPLICE FIBER $40.602100.00%614 5th Street Ext F7036906
70-AS24+ DROP SPLICE $80.501100.00%614 5th Street Ext F7036906
Sub $121.10 0 0 $0.00 $121.10First Digital
'''

class RecapTests(unittest.TestCase):
    def setUp(self):
        self.path=Path(__file__).parent/('qa-recap-'+str(uuid.uuid4())+'.sqlite');r.initialize(self.db)
        self.job={'sourceKey':'8067039099834244:123','title':'7036906 · 614 5th Street Ext','area':'Columbia','sentAt':'2026-10-27T16:00:00Z','billedDay':'2026-10-27','amountCents':12110,'lines':[{'code':'FS01','quantity':2},{'code':'AS24','quantity':1}]}
    def tearDown(self):self.path.unlink(missing_ok=True)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def doc(self,text=TEXT,digest='one'):
        doc={**r.parse_text(text,'First Digital Payroll Recap.pdf'),'digest':digest,'receivedAt':'2026-10-27T18:00:00Z'}
        with self.db() as c:c.execute('INSERT OR IGNORE INTO billing_recap_documents VALUES (?,?,?,?)',(digest,'m',json.dumps(doc),r.now()))
        return doc
    def test_verified_printed_total_and_unit_counts(self):
        d=r.parse_text(TEXT,'recap.pdf');self.assertEqual(d['totalCents'],12110);self.assertEqual(d['records'][0]['lines'][0]['quantity'],2)
    def test_reject_partial_or_wrong_total(self):
        for text in (TEXT.replace('Sub $121.10','Sub $121.11'),TEXT.replace('100.00%','50.00%',1),TEXT.replace('First Digital','Other company')):
            with self.assertRaises(ValueError):r.parse_text(text,'recap.pdf')
    def test_pdf_extraction_path(self):
        from reportlab.pdfgen import canvas
        b=io.BytesIO();pdf=canvas.Canvas(b,pagesize=(1200,900));y=850
        for line in TEXT.splitlines():pdf.drawString(20,y,line);y-=18
        pdf.save();self.assertEqual(r.parse_pdf(b.getvalue(),'recap.pdf')['totalCents'],12110)
    def test_exact_address_abbreviations_pid_and_ambiguity(self):
        rec={'location':'614 5th St Ext F7036906','area':'Columbia'}
        self.assertEqual(r.match_record(rec,[self.job]),self.job)
        self.assertIsNone(r.match_record(rec,[self.job,{**self.job,'sourceKey':'other'}]))
        self.assertIsNone(r.match_record({'location':'614 other st','area':'Columbia'},[self.job]))
    def test_money_and_code_differences_separate(self):
        self.doc();rows=r.report(self.db,[self.job])['rows'];self.assertEqual(rows[0]['status'],'Matched');self.assertEqual(rows[0]['differenceCents'],0)
        other={**self.job,'amountCents':12000,'lines':[{'code':'FS01','quantity':3}]};row=r.report(self.db,[other])['rows'][0]
        self.assertEqual(row['differenceCents'],110);self.assertIn('FS01: billed 3, recap 2',row['detail']);self.assertEqual(row['status'],'Difference to review')
    def test_cutoff_eastern_midnight_and_old_jobs_never_flagged(self):
        self.assertEqual(r.day('2026-10-27T03:59:59Z'),'2026-10-26');self.assertEqual(r.day('2026-10-27T04:00:00Z'),'2026-10-27')
        old={**self.job,'sourceKey':'old','sentAt':'2026-10-26T23:00:00Z'}
        rows=r.report(self.db,[old,self.job])['rows'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['sourceKey'],self.job['sourceKey']);self.assertEqual(rows[0]['status'],'Awaiting recap')
    def test_old_recap_is_not_a_missing_current_recap(self):
        self.doc(TEXT.replace('10/31/2026','10/24/2026'));rows=r.report(self.db,[self.job])['rows'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'Awaiting recap')
    def test_unmatched_job_not_on_covering_recap_only_current(self):
        self.doc();other={**self.job,'title':'111 other st','sourceKey':'other'};rows=r.report(self.db,[other])['rows']
        self.assertEqual(rows[-1]['status'],'Not on received recap');self.assertEqual(rows[0]['status'],'Not matched to current billing')
    def test_same_digest_duplicate_and_revised_files_not_summed(self):
        self.doc();self.doc();self.assertEqual(len(r.report(self.db,[self.job])['rows']),1)
        self.doc(digest='two');rows=r.report(self.db,[self.job])['rows'];self.assertEqual(len(rows),2);self.assertTrue(all(x['status']=='Multiple recap versions' for x in rows))
    def test_normal_job_emails_do_not_become_recaps(self):
        self.assertFalse(r.candidate({'subject':'614 5th St Work Complete'},[{'name':'Work Completed Spreadsheet.xlsx'}]))
        self.assertTrue(r.candidate({'subject':'FW: Contractor Invoices & Payroll Recaps WE 10.31.26'},[]))
    def test_original_historical_parser_regression_no_import(self):
        path=Path(__file__).parent.parent/'mail-historical-documents.json'
        if not path.exists():return
        docs=json.loads(path.read_text(encoding='utf8'));checked=0
        for d in docs:
            if 'payroll recap' not in d.get('filename','').lower():continue
            try: parsed=r.parse_text(d['text'],d['filename'])
            except ValueError:continue
            self.assertEqual(parsed['totalCents'],sum(x['amountCents'] for x in parsed['records']));checked+=1
        self.assertGreater(checked,5)
    def test_intake_does_not_create_operational_job(self):
        import operations_intake
        source=Path(operations_intake.__file__).read_text(encoding='utf8');self.assertLess(source.index('if is_recap(message,attachments):'),source.index('item = message_item(store,message,attachments)'))
    def test_forward_ingest_duplicate_retains_source_once_and_never_sends(self):
        import base64,graph_import,operations_intake
        message={'subject':'FW: First Digital Billing Recap','receivedDateTime':'2026-10-27T18:00:00Z','from':{'emailAddress':{'address':'thomas@firstdigitalsc.com'}}}
        attachments=[{'@odata.type':'#microsoft.graph.fileAttachment','name':'First Digital Payroll Recap.pdf','contentType':'application/pdf','contentBytes':base64.b64encode(b'fixture').decode()}]
        store=MagicMock();store.get_sheet.return_value={'columns':[{'id':1,'title':'Message Key'}],'rows':[{'id':10,'cells':[{'columnId':1,'value':'m'}]}]}
        parsed=r.parse_text(TEXT,'First Digital Payroll Recap.pdf')
        with patch.object(operations_intake,'capture_email_files') as retain,patch.object(r,'parse_pdf',return_value=parsed) as parse,patch.object(r,'sync_sheet'):
            first=r.ingest(store,self.db,message,attachments,'m','token',5);second=r.ingest(store,self.db,message,attachments,'m','token',5)
            self.assertEqual(first['state'],'Recap reconciled');self.assertEqual(second['state'],'Recap reconciled');self.assertEqual(parse.call_count,1);self.assertEqual(retain.call_count,2)
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM billing_recap_documents').fetchone()[0],1)
    def test_mail_before_activation_is_retained_without_import(self):
        import operations_intake
        store=MagicMock();store.get_sheet.return_value={'columns':[{'id':1,'title':'Message Key'}],'rows':[{'id':10,'cells':[{'columnId':1,'value':'m'}]}]}
        message={'receivedDateTime':'2026-10-27T03:59:00Z','from':{'emailAddress':{'address':'thomas@firstdigitalsc.com'}}}
        with patch.object(operations_intake,'capture_email_files'),patch.object(r,'sync_sheet') as sync:
            self.assertEqual(r.ingest(store,self.db,message,[],'m','token',5)['state'],'Before recap tracking');sync.assert_not_called()

if __name__=='__main__':unittest.main()
