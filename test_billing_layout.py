"""Synthetic compact-template and outgoing-only pricing checks. No mail is sent."""
import io
import sys
import types
import unittest
import zipfile
import xml.etree.ElementTree as ET

sys.modules.setdefault('mail_automation', types.ModuleType('mail_automation'))
import billing_mail as billing

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
CODES = ['MC09','ER01','ER03','MC10','FS01','FS02','FS03','FS04','FS07A','FS07','FS08','FS10','FS14','FS15','FS16','US01','AS24']
COLS = 'BCDEFGHIJKLMNOPQR'

def fixture(count=2, legacy=False):
    total = 28 if legacy else count + 2
    header = '<row r="1">' + ''.join(f'<c r="{col}1" t="inlineStr"><is><t>{code}</t></is></c>' for col,code in zip(COLS,CODES)) + '</row>'
    rows = ''.join(f'<row r="{n}"><c r="B{n}"><v>1</v></c><c r="F{n}"><v>2</v></c></row>' for n in range(2,count+2))
    rows += f'<row r="{total}"><c r="S{total}" t="inlineStr"><is><t>Totals</t></is></c><c r="B{total}"><f>SUM(B2:B{total-1})</f><v>999</v></c></row>'
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{NS}"><dimension ref="A1:S{total}"/><sheetData>{header}{rows}</sheetData></worksheet>')
        z.writestr('xl/styles.xml',f'<styleSheet xmlns="{NS}"><fonts count="1"><font/></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{NS}"><sheets/></workbook>')
    return out.getvalue(),total

class CompactBillingTests(unittest.TestCase):
    def test_compact_and_legacy_quantity_ranges_ignore_stale_totals(self):
        for count,legacy in [(1,False),(9,False),(26,False),(9,True)]:
            with self.subTest(count=count,legacy=legacy):
                raw,total = fixture(count,legacy)
                quantities,row = billing.quantity_layout(raw)
                self.assertEqual(row,total)
                self.assertEqual(quantities['MC09'],count)
                self.assertEqual(quantities['FS01'],2*count)

    def test_pricing_follows_totals_and_keeps_source_unpriced(self):
        for count in (1,9,26):
            with self.subTest(count=count):
                raw,total=fixture(count)
                priced,summary=billing.price_spreadsheet(raw,{c:2.25 for c in CODES})
                self.assertEqual(summary['amount'],count*6.75)
                with zipfile.ZipFile(io.BytesIO(priced))as z:
                    root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                    amount=root.find(f'.//{{{NS}}}c[@r="S{total+1}"]')
                    self.assertEqual(amount.find(f'{{{NS}}}f').text,f'SUM(B{total+1}:R{total+1})')
                self.assertEqual(billing.quantity_totals(raw)['MC09'],count)
                with self.assertRaises(ValueError):billing.quantity_totals(priced)

    def test_outgoing_filename_keeps_job_name(self):
        raw,_=fixture()
        name='Work Completed Spreadsheet (Fictional job).xlsx'
        files,_=billing.package_files([{'role':'spreadsheet','name':name,'data':raw}],{c:2.25 for c in CODES})
        self.assertEqual(files[0]['name'],name)
        self.assertNotEqual(files[0]['data'],raw)

    def test_missing_or_ambiguous_totals_cannot_reach_pricing(self):
        raw,_=fixture()
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw))as z,zipfile.ZipFile(out,'w')as dest:
            for item in z.infolist():dest.writestr(item,z.read(item.filename).replace(b'Totals',b'Other') if item.filename.endswith('sheet1.xml') else z.read(item.filename))
        with self.assertRaisesRegex(ValueError,'one totals row'):billing.price_spreadsheet(out.getvalue(),{c:2.25 for c in CODES})

if __name__=='__main__':unittest.main()
