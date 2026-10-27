"""Exercise the real attachment collector, preview and MIME. Transport is mocked."""
import base64,sqlite3,uuid,unittest
from pathlib import Path
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from unittest.mock import patch
import billing_mail as b
from test_billing_layout import fixture,CODES
from test_billing_mail import Store

class NoWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.path=Path(__file__).parent/('qa-no-workbook-'+str(uuid.uuid4())+'.sqlite')
        self.store=Store();self.store.raw=fixture()[0]
        self.payload={'op':'prepare','sheetId':'2515846899388292','rowId':'123','files':{'spreadsheet':'1','map':'2','workbook':'3'},'workbookRequired':True,'workbookNotNeeded':True,'rates':{code:2.25 for code in CODES}}
    def tearDown(self):self.path.unlink(missing_ok=True)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def test_explicit_no_overrides_stale_workbook_for_preview_and_send(self):
        route={'to':['customer@example.invalid'],'cc':[],'subject':'Test','messageId':'<source@example.invalid>','references':[]}
        sent=[]
        with patch.object(b,'billing_route',return_value=route),patch.object(b.mail.graph,'access_token',return_value='fixture'),patch.object(b.mail.graph,'_request',side_effect=lambda *a,**kw:sent.append(kw)):
            prepared=b.handle(self.store,self.db,self.payload)
            self.assertEqual([f['role'] for f in prepared['files']],['spreadsheet','map']);self.assertEqual(sent,[])
            result=b.handle(self.store,self.db,{**self.payload,'op':'send','id':prepared['id']})
        self.assertEqual(result['state'],'ACCEPTED');self.assertEqual(len(sent),1)
        message=BytesParser(policy=policy.default).parsebytes(base64.b64decode(sent[0]['body']))
        attachments=list(message.iter_attachments());self.assertEqual(len(attachments),2)
        self.assertNotIn('asbuilt.xlsm',[a.get_filename() for a in attachments])
        self.assertTrue(any(a.get_filename().endswith('.xlsx') for a in attachments))
        self.assertTrue(any(a.get_payload(decode=True)==self.store.pdf for a in attachments))
    def test_required_workbook_still_included_without_opt_out(self):
        files=b.collect(self.store,{**self.payload,'workbookNotNeeded':False})
        self.assertEqual([f['role'] for f in files],['spreadsheet','map','workbook'])

if __name__=='__main__':unittest.main()
