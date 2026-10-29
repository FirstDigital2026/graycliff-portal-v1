import unittest
from datetime import date
from kinetic_tracking import reconcile


class TrackingTests(unittest.TestCase):
    def test_payment_projection_uses_stated_dates_not_notice_dates(self):
        invoices,receipts=self.history()
        for r,d in zip(receipts,['1/30/26','2/10/2026','2026-02-19']):r['paymentDate']=d
        r=reconcile(invoices+[self.invoice('2026-01-24')],receipts,date(2026,2,1))
        row=r['weeks'][0]
        self.assertEqual(row['expectedDate'],'2026-02-23')
        self.assertEqual(row['expectedPaymentDate'],'2026-02-24')
        self.assertEqual(row['expectedPaymentStart'],'2026-02-20')
        self.assertEqual(row['expectedPaymentEnd'],'2026-02-26')
        self.assertEqual(r['weeks'][1]['statedPaymentDates'],['2026-02-19'])

    def test_payment_projection_needs_three_complete_positive_matches(self):
        invoices,receipts=self.history()
        for r in receipts:r['paymentDate']='invalid'
        receipts[0]['paymentDate']='1/30/26';receipts[1]['paymentDate']='2/10/26'
        r=reconcile(invoices+[self.invoice('2026-01-24')],receipts)
        self.assertIsNone(r['weeks'][0]['expectedPaymentDate'])

    def invoice(self, week, amount=10000, project='P1'):
        return {'projectCode':project,'week':week,'amountCents':amount,'lines':[]}

    def receipt(self, ref, amount=10000, notice=None, project='P1'):
        return {'projectCode':project,'invoiceRef':ref,'amountCents':amount,'noticeDate':notice}

    def history(self):
        return ([self.invoice('2026-01-03'),self.invoice('2026-01-10'),self.invoice('2026-01-17')],
                [self.receipt('P-010326',notice='2026-01-29'),self.receipt('P-011026',notice='2026-02-09'),self.receipt('P-011726',notice='2026-02-18')])

    def test_overdue_starts_after_window_not_at_median(self):
        invoices,receipts=self.history();invoices.append(self.invoice('2026-01-24'))
        r=reconcile(invoices,receipts,date(2026,2,25));row=r['weeks'][0]
        self.assertEqual(r['model']['medianDays'],30)
        self.assertEqual(row['expectedEnd'],'2026-02-25');self.assertFalse(row['flagged'])
        row=reconcile(invoices,receipts,date(2026,2,26))['weeks'][0]
        self.assertTrue(row['flagged']);self.assertEqual(row['overdueDays'],1)

    def test_dated_reference_matches_short_amount_and_partial_payments_sum(self):
        inv=[self.invoice('2026-01-03')]
        r=reconcile(inv,[self.receipt('P-010326',6000)],date(2026,1,5))
        self.assertEqual(r['weeks'][0]['differenceCents'],4000);self.assertTrue(r['weeks'][0]['flagged'])
        r=reconcile(inv,[self.receipt('P-010326',6000),self.receipt('P-010326',4000)])
        self.assertFalse(r['weeks'][0]['flagged']);self.assertEqual(r['weeks'][0]['reportedCents'],10000)

    def test_wrong_reference_never_clears_equal_amount(self):
        r=reconcile([self.invoice('2026-01-03')],[self.receipt('P-011026')])
        self.assertEqual(len(r['unmatchedRemittances']),1);self.assertEqual(r['weeks'][0]['reportedCents'],0)

    def test_repeated_amount_and_other_project_cannot_match(self):
        invoices=[self.invoice('2026-01-03'),self.invoice('2026-01-10')]
        r=reconcile(invoices,[self.receipt('NO-DATE'),self.receipt('P-010326',project='P2')])
        self.assertEqual(len(r['unmatchedRemittances']),2)

    def test_excess_amount_and_signed_credit(self):
        inv=[self.invoice('2026-01-03')]
        row=reconcile(inv,[self.receipt('P-010326',11000)])['weeks'][0]
        self.assertTrue(row['flagged']);self.assertEqual(row['differenceCents'],-1000)
        row=reconcile(inv,[self.receipt('P-010326',11000),self.receipt('P-010326',-1000)])['weeks'][0]
        self.assertFalse(row['flagged']);self.assertEqual(row['reportedCents'],10000)

    def test_no_false_deadline_without_sufficient_email_dates(self):
        invoices,receipts=self.history();receipts[0]['noticeDate']=None
        r=reconcile(invoices+[self.invoice('2026-01-24')],receipts,date(2026,5,1))
        self.assertIsNone(r['weeks'][0]['expectedEnd']);self.assertFalse(r['weeks'][0]['flagged'])

    def test_four_digit_reference_uses_billing_year_and_undated_unique_amount(self):
        invoices=[self.invoice('2026-01-03',12000),self.invoice('2026-01-10',14000)]
        r=reconcile(invoices,[self.receipt('P 0103',12000),self.receipt('PROJECT',14000)])
        self.assertEqual(len(r['unmatchedRemittances']),0)
        self.assertTrue(all(w['differenceCents']==0 for w in r['weeks']))

    def test_report_profit_bounds_include_every_billed_line(self):
        import json,sqlite3
        from unittest.mock import patch
        import kinetic_finance as k
        connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
        try:
            k.initialize(lambda:connection)
            invoice=self.invoice('2026-01-03')
            invoice.update(kind='invoice',filename='synthetic.pdf',lines=[
                {'code':'X','codeCandidates':['X1','X2'],'quantity':'10','amountCents':6000,'subCostEstimateCents':None},
                {'code':'Y','codeCandidates':['Y'],'quantity':'1','amountCents':4000,'subCostEstimateCents':500}])
            with connection:connection.execute('INSERT INTO private_commercial_documents VALUES (?,?,?,?)',('test','invoice',json.dumps(invoice),'2026-01-03'))
            with patch.object(k,'COST',{'X1':{'rate':'1.00'},'X2':{'rate':'2.00'}}):
                p=k.report(lambda:connection)['profitEstimate']
            self.assertEqual(p['minimumCents'],7500);self.assertEqual(p['maximumCents'],8500)
            with patch.object(k,'COST',{'X1':{'rate':'1.00'}}):
                p=k.report(lambda:connection)['profitEstimate']
            self.assertIsNone(p['minimumCents']);self.assertEqual(p['unboundedLines'],1)
        finally:connection.close()


if __name__=='__main__':unittest.main()
