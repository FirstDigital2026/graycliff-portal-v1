"""Synthetic regression checks for unpriced review and send-only pricing."""
import hashlib,json,sqlite3,uuid,unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import billing_mail as b

class UnpricedReviewTests(unittest.TestCase):
    def setUp(self):
        self.path=Path(__file__).parent/('qa-review-'+str(uuid.uuid4())+'.sqlite')
        self.payload={'op':'prepare','sheetId':'2515846899388292','rowId':'123','rates':{'TEST':2}}
        self.files=[]
        for role,name,data in [('spreadsheet','Test.xlsx',b'unpriced test'),('map','Test.pdf',b'test map')]:
            self.files.append({'role':role,'name':name,'data':data,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        self.route={'to':['test@example.invalid'],'cc':[],'subject':'Test only','messageId':'<test@example.invalid>','references':[]}
        self.patches=[patch.object(b,'fresh_job',return_value={'Task Name':'Test'}),patch.object(b,'collect',side_effect=lambda *a:[dict(f) for f in self.files]),patch.object(b,'quantity_totals',return_value={'TEST':1}),patch.object(b.mail,'resolve_thread',return_value=self.route,create=True),patch.object(b.mail.graph,'access_token',return_value='test'),patch.object(b.mail.graph,'_request'),patch.object(b,'mime_message',return_value=b'test MIME'),patch.object(b,'price_spreadsheet',return_value=(b'priced test',{'amount':2,'lines':[]}))]
        self.mocks=[p.start() for p in self.patches]
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.path.unlink(missing_ok=True)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def test_review_is_unpriced_and_send_prices_once(self):
        d=b.handle(object(),self.db,self.payload)
        self.assertTrue(d['pricingOnSend']);self.assertNotIn('pricing',d);self.assertNotIn('rates',d)
        self.mocks[-1].assert_not_called();self.mocks[5].assert_not_called()
        self.assertEqual(d['files'][0]['sha256'],self.files[0]['sha256'])
        with self.db() as c:snap=json.loads(c.execute('SELECT snapshot FROM billing_package_outbox').fetchone()[0])
        self.assertNotIn('pricing',snap);self.assertEqual(snap['snapshotVersion'],4)
        send={**self.payload,'op':'send','id':d['id']}
        self.assertEqual(b.handle(object(),self.db,send)['state'],'ACCEPTED')
        self.mocks[-1].assert_called_once_with(b'unpriced test',{'TEST':2})
        self.assertEqual(self.files[0]['data'],b'unpriced test')
        b.handle(object(),self.db,send);self.mocks[-1].assert_called_once();self.mocks[5].assert_called_once()
    def test_outgoing_asbuilt_name_preserves_bytes_and_source(self):
        source=[{'role':'map','name':'Field-upload-abc-map.pdf','data':b'test map','size':8,'sha256':'test'}]
        renamed=b.delivery_files(source,'Example Job')
        self.assertEqual(renamed[0]['name'],'Splicing Asbuilt (Example Job).pdf')
        self.assertEqual(renamed[0]['data'],source[0]['data'])
        self.assertEqual(source[0]['name'],'Field-upload-abc-map.pdf')
        self.assertNotIn('\\',b.delivery_files(source,'Bad\\name')[0]['name'])
    def test_email_uses_the_selected_wording_and_keeps_thread(self):
        d=b.draft({'Task Name':'Example Job'},self.route,'test')
        self.assertIn('Please see the attached completed billing package for Example Job.',d['text'])
        self.assertIn('We appreciate your business and look forward to continuing our partnership.',d['html']);self.assertNotIn('Billing package submitted',d['text'])
        self.assertEqual(d['parent'],self.route['messageId'])
    def test_retired_test_requests_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'removed'):
            b.handle(object(),self.db,{**self.payload,'testTo':'tester@firstdigitalsc.com'})
        self.mocks[5].assert_not_called()
    def test_priced_review_is_unavailable(self):
        with self.assertRaisesRegex(ValueError,'unpriced'):
            b.handle(object(),self.db,{**self.payload,'op':'review_pricing'})
        self.mocks[-1].assert_not_called();self.mocks[5].assert_not_called()

if __name__=='__main__':unittest.main()


