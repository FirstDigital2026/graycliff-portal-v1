import unittest
from unittest.mock import patch
import kinetic_finance as k
from commercial_cost_codes import enrich
from commercial_spectrum import parse


class FullCodes(unittest.TestCase):
    def test_full_recap_must_match_summary_quantity_and_amount(self):
        doc={'amountCents':2000,'lines':[{'code':'T','codeCandidates':['TEST1','TEST2'],'quantity':'10','amountCents':2000,'rateCents':200,'subCostEstimateCents':None}]}
        pages=[{'page':2,'recapBody':'TEST1 Work 100.00% 4 $8.00\nTEST2 Work 100.00% 6 $12.00'}]
        with patch.object(k,'COST',{'TEST1':{'rate':'1.00'},'TEST2':{'rate':'1.50'}}):
            d=enrich(doc,pages);self.assertEqual(sum(l['subCostEstimateCents'] for l in d['lines']),1300)
            pages[0]['recapBody']=pages[0]['recapBody'].replace('6 $12.00','5 $10.00')
            self.assertIsNone(enrich(doc,pages)['lines'][0]['subCostEstimateCents'])

    def test_spectrum_recaps_accept_on_call_code_and_const_header(self):
        text='Payroll Recaps\nFirst Digital\nWeek Ending: 1/3/2026\n9819304003-COLUMBIA, SC_CONST\nEmp # 127675\nDaily No 1\n257 ON CALL PAY $150.001.5100.00%Pay Only\nSub $150.00'
        d=parse(text,'test.pdf');self.assertEqual(d['kind'],'spectrum_recap');self.assertEqual(d['amountCents'],15000)

    def test_spectrum_invoice_rejects_partial_total_and_kinetic_code(self):
        text='First Digital Graycliff\nWeek Ending: 1/3/2026\nJob # : 981TEST\n70-FS01+ Work 2 $40.00$20.00\nTotal: $40.00'
        self.assertEqual(parse(text,'test.pdf')['amountCents'],4000)
        with self.assertRaises(ValueError):parse(text.replace('Total: $40.00','Total: $41.00'),'test.pdf')
        with self.assertRaises(ValueError):parse(text.replace('70-FS01+','BFO12'),'test.pdf')

if __name__=='__main__':unittest.main()
