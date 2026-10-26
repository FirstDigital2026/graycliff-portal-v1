import json,sqlite3,uuid,unittest
from contextlib import contextmanager
from pathlib import Path
import billing_mail as billing
from financial_report import report,cents


class FinancialReportTests(unittest.TestCase):
    def setUp(self):
        self.path=Path(__file__).parent/('qa-finance-'+str(uuid.uuid4())+'.sqlite');billing.initialize(self.db)
    def tearDown(self):self.path.unlink(missing_ok=True)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def add(self,identity='1',key='123:456',state='ACCEPTED',amount='100.01',created=1,test=False):
        snap={'input':{'sheetId':'123','rowId':'456'},'draft':{'title':'Fictional job'},'pricing':{'amount':amount,'lines':[{'code':'FS01','quantity':2,'total':amount}]}}
        if test:snap['input']['testTo']='test@example.invalid'
        with self.db() as c:c.execute('INSERT INTO billing_package_outbox VALUES(?,?,?,?,?,?,?)',(identity,key,state,json.dumps(snap),created,'2026-10-26T19:00:00+00:00',''))
    def test_production_only_and_deduplicated(self):
        self.add();self.add('2',key='123:456:test:legacy');self.add('3',state='PREPARED');self.add('4',state='UNCERTAIN');self.add('5',created=5,amount='125.50')
        result=report(self.db);self.assertEqual(len(result['jobs']),1);self.assertEqual(result['jobs'][0]['amountCents'],12550);self.assertEqual(result['excludedTestSends'],1)
    def test_internal_test_on_normal_key_excluded(self):
        self.add(test=True);self.assertEqual(report(self.db)['jobs'],[])
    def test_unverified_pricing_is_missing_not_zero(self):
        self.add(amount=None);self.assertIsNone(report(self.db)['jobs'][0]['amountCents'])
    def test_report_is_read_only_and_excludes_delivery_contacts(self):
        self.add();result=report(self.db);self.assertNotIn('to',result['jobs'][0]);self.assertNotIn('cc',result['jobs'][0])
        with self.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM billing_package_outbox').fetchone()[0],1)
    def test_cents_are_exact_and_reject_invalid_values(self):
        self.assertEqual(cents('1719.20'),171920);self.assertEqual(cents('1.005'),101);self.assertEqual(cents(0),0)
        for value in (None,True,'NaN','Infinity',-1,'not money'):self.assertIsNone(cents(value))
    def test_dispatcher_report_needs_no_job_and_never_dispatches(self):
        self.add();result=billing.handle(object(),self.db,{'op':'financials'});self.assertEqual(len(result['jobs']),1)


if __name__=='__main__':unittest.main()
