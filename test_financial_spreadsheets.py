import io,zipfile,unittest,json
from financial_spreadsheets import extract_total,match_job,save_candidates
from test_financial_report import FinancialReportTests
from financial_report import report

def workbook(total='382.90',formula='SUM(C4:R4)'):
    codes=['MC09','ER01','ER03','MC10','FS01','FS02','FS03','FS04','FS07A','FS07','FS08','FS10','FS14','FS15','FS16','US01','AS24']
    cells=[]
    for col,code in zip('BCDEFGHIJKLMNOPQR',codes):
        amount='60.90' if col=='F' else '322' if col=='R' else '97.50' if col=='B' else '0'
        cells.extend([f'<c r="{col}1" t="inlineStr"><is><t>{code}</t></is></c>',f'<c r="{col}3"><v>1</v></c>',f'<c r="{col}4"><v>{amount}</v></c>'])
    cells.extend(['<c r="S3" t="inlineStr"><is><t>Totals</t></is></c>',f'<c r="S4"><f>{formula}</f><v>{total}</v></c>'])
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row>'+''.join(cells)+'</row></sheetData></worksheet>')
    return out.getvalue()

class ExtractionTests(unittest.TestCase):
    def test_actual_total_range_not_repriced(self):
        r=extract_total(workbook());self.assertEqual(r['amountCents'],38290);self.assertNotIn('MC09',[x['code'] for x in r['lines']]);self.assertTrue(r['sourceCell'].endswith('S4'))
    def test_bad_cache_and_unknown_formula_rejected(self):
        for raw in (workbook('400'),workbook(formula='SUM(S4:S4)'),workbook(formula='HYPERLINK("evil")')):
            with self.assertRaises(ValueError):extract_total(raw)
    def test_match_is_unique_and_address_normalized(self):
        jobs=[{'title':'5033 Erwin Court','sourceKey':'1:1'}]
        self.assertEqual(match_job('Work Completed Spreadsheet (5033 Erwin Ct).xlsx',jobs)['sourceKey'],'1:1')
        self.assertIsNone(match_job('Work Completed Spreadsheet (5033 Erwin Ct).xlsx',jobs+jobs))
        self.assertIsNone(match_job('Work Completed Spreadsheet (5034 Erwin Ct).xlsx',jobs))

class ImportedReportTests(FinancialReportTests):
    def test_import_fills_gap_but_not_accepted_send_and_does_not_leak_mail_ids(self):
        record={'sourceKey':'123:456','amountCents':38290,'sentAt':'2026-10-20T12:00:00Z','subject':'Billing','messageId':'private','attachmentId':'private','source':'Work completed spreadsheet','sourceFile':'Example.xlsx','sourceCell':'sheet1!S4'}
        save_candidates(self.db,{'123:456':[record]});r=report(self.db);self.assertEqual(r['jobs'][0]['amountCents'],38290);self.assertNotIn('messageId',r['jobs'][0]);self.add();self.assertEqual(report(self.db)['jobs'][0]['amountCents'],10001)
    def test_conflicting_historical_amounts_are_not_guessed(self):
        base={'sourceKey':'123:456','amountCents':100,'sentAt':'2026-10-20','subject':'Billing'}
        r=save_candidates(self.db,{'123:456':[base,{**base,'amountCents':200,'sentAt':'2026-10-21'}]});self.assertEqual(r['conflicts'],1);self.assertEqual(report(self.db)['jobs'],[])

if __name__=='__main__':unittest.main()
