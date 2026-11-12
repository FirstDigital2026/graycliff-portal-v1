import unittest,io,tempfile
from pathlib import Path
from unittest.mock import patch
import field_pdf as f

class RegionsTests(unittest.TestCase):
    def test_ntp_copy_keeps_scope_and_removes_po_amount(self):
        try:
            import pymupdf as fitz
            from PIL import Image
        except ImportError:self.skipTest('Production PDF runtime required')
        doc=fitz.open();p=doc.new_page();p.insert_text((20,30),'PRISM ID: 7061750 Work Order #: 3755355');p.insert_text((20,50),'PO Amount: 999.99');p.insert_text((20,70),'OTE upgrade with light push')
        raw=doc.tobytes();doc.close()
        with tempfile.TemporaryDirectory() as folder:out,n=f.sanitize(raw,Path(folder))
        result=fitz.open(stream=out,filetype='pdf');text=result[0].get_text();self.assertNotIn('999.99',text);self.assertIn('OTE upgrade with light push',text);self.assertIn('7061750',text);self.assertGreater(n,0);result.close()
    def test_only_financial_field_selected_from_survey_row(self):
        row=[(0,0,40,10,'Status:'),(45,0,90,10,'Working'),(100,0,125,10,'Total'),(130,0,170,10,'Project'),(175,0,205,10,'Cost:'),(220,0,250,10,'836'),(300,0,335,10,'Cross'),(340,0,375,10,'Street:')]
        self.assertEqual(f.regions(row),[(99,-1,251,11)])
    def test_unresolved_price_table_is_not_released(self):
        with self.assertRaises(ValueError):f.regions([(0,0,40,10,'Unit'),(45,0,85,10,'Price')])
    def test_currency_amount_removed(self):
        self.assertEqual(f.regions([(0,0,40,10,'$123.45')]),[(-1,-1,41,11)])
    def test_raster_price_removed_from_embedded_survey(self):
        try:
            import pymupdf as fitz
            from PIL import Image
        except ImportError:self.skipTest('Production PDF runtime required')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);im=Image.new('RGB',(400,80),'white');b=io.BytesIO();im.save(b,format='PNG');im.close()
            doc=fitz.open();p=doc.new_page();p.insert_text((20,30),'Fiber splice map');p.insert_image(fitz.Rect(20,40,420,120),stream=b.getvalue());raw=doc.tobytes();doc.close()
            def read(data,root,index):
                path=root/'ocr.png';path.write_bytes(data)
                return path,{'text':'Total Project Cost: 836','words':[(0,0,30,10,'Total'),(35,0,75,10,'Project'),(80,0,105,10,'Cost:'),(110,0,140,10,'836')]}
            with patch.object(f,'ocr_image',side_effect=read):out,count=f.sanitize(raw,root)
            self.assertEqual(count,1)
            result=fitz.open(stream=out,filetype='pdf');self.assertEqual(len(result),1);self.assertIn('Fiber splice map',result[0].get_text());self.assertEqual(result.embfile_count(),0)
            result.close()
    def test_real_ocr_can_run_under_pdf_worker_limits(self):
        try:
            import pymupdf as fitz
            from PIL import Image
        except ImportError:self.skipTest('Production PDF runtime required')
        im=Image.new('RGB',(80,40),'white');b=io.BytesIO();im.save(b,format='PNG');im.close()
        doc=fitz.open();p=doc.new_page();p.insert_image(fitz.Rect(10,10,90,50),stream=b.getvalue());raw=doc.tobytes();doc.close()
        out,n=f.prepare(raw)
        self.assertEqual(n,0);result=fitz.open(stream=out,filetype='pdf');self.assertEqual(len(result),1);result.close()

if __name__=='__main__':unittest.main()
