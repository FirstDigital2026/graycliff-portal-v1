import unittest
from datetime import date
from spectrum_tracking import build
from commercial_spectrum import parse


class SpectrumTrackingTests(unittest.TestCase):
    def fixtures(self):
        line={'code':'FS01','quantity':'2','amountCents':4000}
        invoice={'kind':'spectrum_invoice','projectCode':'TEST','week':'2026-01-03','amountCents':4000,'lines':[line],'filename':'invoice.pdf'}
        recap={'kind':'spectrum_recap','area':'Area A','week':'2026-01-03','amountCents':4000,'records':[{'lines':[line]}],'filename':'recap.pdf'}
        remit={'kind':'remittance','paymentNumber':'E1','paymentDate':'1/30/26','filename':'remit.pdf','source':{'sentAt':'2026-01-29T12:00:00Z'},'lines':[{'projectCode':'TEST','invoiceRef':'F-010326','amountCents':4000},{'projectCode':'KINETIC','invoiceRef':'K-010326','amountCents':99999}]}
        return invoice,recap,remit

    def test_invoice_and_recap_count_once_and_other_projects_excluded(self):
        docs=list(self.fixtures());r=build(docs,date(2026,2,1))
        self.assertEqual(r['revenueCents'],4000);self.assertEqual(r['receiptsCents'],4000)
        self.assertEqual(len(r['tracking']['weeks']),1);self.assertEqual(r['tracking']['flagCount'],0)
        self.assertEqual(r['tracking']['weeks'][0]['statedPaymentDates'],['2026-01-30'])

    def test_recap_mapping_requires_full_codes_and_quantities(self):
        invoice,recap,remit=self.fixtures();recap['records']=[{'lines':[{'code':'FS02','quantity':'2','amountCents':4000}]}]
        r=build([invoice,recap,remit]);self.assertEqual(len(r['review']),1)

    def test_conflicting_billing_excluded_not_double_counted(self):
        invoice,recap,remit=self.fixtures();other={**invoice,'amountCents':4100}
        r=build([invoice,recap,other,remit]);self.assertEqual(r['revenueCents'],0)
        self.assertTrue(r['review']);self.assertEqual(r['tracking']['flagCount'],0)

    def test_unmatched_old_remittance_is_missing_source_not_unpaid_bill(self):
        invoice,recap,remit=self.fixtures();remit['lines'][0]['invoiceRef']='F-122025'
        r=build([invoice,recap,remit]);self.assertEqual(len(r['tracking']['unmatchedRemittances']),1)
        self.assertEqual(r['tracking']['flagCount'],0)

    def test_legacy_invoice_without_rate_and_accounting_credit(self):
        text='First Digital Graycliff\nFor Week Ending 1/3/2026\n95603000 - FLORENCE, SC\nFS01+ Work 2 $40.00\nTotal: $40.00'
        self.assertEqual(parse(text,'test.pdf')['amountCents'],4000)
        text='First Digital Graycliff\nWeek Ending: 1/3/2026\nJob # : 981TEST\n70-MC10+ Setup -1 ($56.00)$56.00\nTotal: ($56.00)'
        self.assertEqual(parse(text,'test.pdf')['amountCents'],-5600)
        text='Payroll Recaps\nFirst Digital\nWeek Ending: 1/3/2026\n981 - COLUMBIA\nEmp # 1\nDaily No 1\n70-MC10+ Setup ($56.00)-1100.00%Location\nSub ($56.00)'
        self.assertEqual(parse(text,'test.pdf')['amountCents'],-5600)


if __name__=='__main__':unittest.main()
