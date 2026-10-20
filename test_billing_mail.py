import base64,io,json,sqlite3,unittest,zipfile,uuid
from unittest.mock import patch
from email.parser import BytesParser
from email import policy
import billing_mail as b
from pathlib import Path
from contextlib import contextmanager

def spreadsheet(priced=False):
    codes=['AA'+str(i).zfill(2) for i in range(17)]
    cells=''.join(f'<c r="{col}1" t="inlineStr"><is><t>{code}</t></is></c>' for col,code in zip('BCDEFGHIJKLMNOPQR',codes))
    cells+='<c r="B2"><v>2</v></c><c r="B27"><v>3</v></c>'
    if priced:cells+='<c r="B29"><f>B28*12.34</f><v>0</v></c>'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row>'+cells+'</row></sheetData></worksheet>')
    return out.getvalue()

class Store:
    token='test-only'
    def __init__(self):self.status='Complete';self.raw=spreadsheet();self.pdf=b'%PDF-1.7\nTEST ONLY';self.book=b'PKtest macro workbook bytes';self.downloads=0
    def get_sheet(self,*a,**kw):return {'columns':[{'id':1,'title':'Status'},{'id':2,'title':'Task Name'}],'rows':[{'id':123,'cells':[{'columnId':1,'value':self.status},{'columnId':2,'value':'TEST ONLY job'}]}]}
    def list_row_attachments(self,*a):return [{'id':1,'name':'quantities.xlsx'},{'id':2,'name':'splice-map.pdf'},{'id':3,'name':'asbuilt.xlsm'}]
    def download_attachment(self,sid,aid):return {1:self.raw,2:self.pdf,3:self.book}[aid],'unused','unused'

class BillingTests(unittest.TestCase):
    def setUp(self):
        self.path=str(Path(__file__).parent/('qa-billing-'+str(uuid.uuid4())+'.sqlite'));self.store=Store();self.sent=[]
        self.payload={'op':'prepare','sheetId':'2515846899388292','rowId':'123','files':{'spreadsheet':'1','map':'2','workbook':'3'},'workbookRequired':True}
        self.route={'to':['customer@example.com'],'cc':['office@example.com'],'subject':'TEST ONLY original','messageId':'<original@example.com>','references':[]}
        self.patches=[patch.object(b.mail,'resolve_thread',return_value=self.route),patch.object(b.mail.graph,'access_token',return_value='test'),patch.object(b.mail.graph,'_request',side_effect=lambda *a,**kw:self.sent.append(kw))]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in self.patches:p.stop()
        Path(self.path).unlink(missing_ok=True)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def test_review_then_send_preserves_files_and_thread(self):
        d=b.handle(self.store,self.db,self.payload);self.assertEqual(self.sent,[]);self.assertEqual(d['state'],'PREPARED')
        send={**self.payload,'op':'send','id':d['id']};r=b.handle(self.store,self.db,send);self.assertEqual(r['state'],'ACCEPTED')
        m=BytesParser(policy=policy.default).parsebytes(base64.b64decode(self.sent[0]['body']))
        self.assertEqual(m['In-Reply-To'],'<original@example.com>');self.assertEqual(m['To'],'customer@example.com')
        files={a.get_filename():a.get_payload(decode=True) for a in m.iter_attachments()}
        self.assertEqual(files['asbuilt.xlsm'],self.store.book);self.assertEqual(files['splice-map.pdf'],self.store.pdf)
        b.handle(self.store,self.db,send);self.assertEqual(len(self.sent),1)
        quantities=b.handle(self.store,self.db,{**self.payload,'op':'quantities'})['quantities'];self.assertEqual(quantities['AA00'],5)
    def test_required_map_and_optional_workbook(self):
        p={**self.payload,'files':{'spreadsheet':'1'},'workbookRequired':False,'workbookNotNeeded':True}
        with self.assertRaisesRegex(ValueError,'map'):b.handle(self.store,self.db,p)
        p['files']['map']='2';d=b.handle(self.store,self.db,p);self.assertEqual(len(d['files']),2)
    def test_priced_sheet_and_incomplete_job_blocked(self):
        self.store.raw=spreadsheet(True)
        with self.assertRaisesRegex(ValueError,'dollar'):b.handle(self.store,self.db,self.payload)
        self.store.raw=spreadsheet();self.store.status='En route'
        with self.assertRaisesRegex(ValueError,'Finish field'):b.handle(self.store,self.db,self.payload)
        self.assertEqual(self.sent,[])
    def test_changed_attachment_requires_review(self):
        d=b.handle(self.store,self.db,self.payload);self.store.pdf+=b'changed'
        with self.assertRaisesRegex(ValueError,'attachment changed'):b.handle(self.store,self.db,{**self.payload,'op':'send','id':d['id']})
        self.assertEqual(self.sent,[])
    def test_uncertain_send_cannot_retry(self):
        d=b.handle(self.store,self.db,self.payload)
        with patch.object(b.mail.graph,'_request',side_effect=TimeoutError):
            with self.assertRaisesRegex(ValueError,'did not confirm'):b.handle(self.store,self.db,{**self.payload,'op':'send','id':d['id']})
        with self.assertRaisesRegex(ValueError,'uncertain'):b.handle(self.store,self.db,self.payload)
        self.assertEqual(self.sent,[])
    def test_stale_review_and_unauthenticated_requests(self):
        d=b.handle(self.store,self.db,self.payload)
        with self.db() as c:c.execute('UPDATE billing_package_outbox SET created_at=0')
        with self.assertRaisesRegex(ValueError,'expired'):b.handle(self.store,self.db,{**self.payload,'op':'send','id':d['id']})
        self.assertFalse(b.authenticated(b'{}',{},'secret'));self.assertEqual(self.sent,[])
    def test_two_prepared_tabs_only_send_once(self):
        a=b.handle(self.store,self.db,self.payload);c=b.handle(self.store,self.db,self.payload)
        b.handle(self.store,self.db,{**self.payload,'op':'send','id':a['id']});b.handle(self.store,self.db,{**self.payload,'op':'send','id':c['id']})
        self.assertEqual(len(self.sent),1)
    def test_blank_quantity_sheet_blocked(self):
        with patch.object(b,'quantity_totals',return_value={'AA00':0}):
            with self.assertRaisesRegex(ValueError,'no completed quantities'):b.handle(self.store,self.db,self.payload)
        self.assertEqual(self.sent,[])

if __name__=='__main__':unittest.main()
