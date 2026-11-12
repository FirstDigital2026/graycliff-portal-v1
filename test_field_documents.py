import unittest,io,json
import subprocess
from unittest.mock import patch
import field_documents as f

SAMPLE='''PRISM ID:         1234567                           Work Order #:       7654321
PO #:             1112223334                        PO Amount:          999.99
Const Type:       Residential                       Const Coordinator:  Example, Coordinator
Work Order Date:  10/20/2026                        Est. Completion:    10/27/2026
Vendor Name:      Example Contractor                Issued By:          Example, Coordinator

Job                      Job Address                     Site Contact Name         Site Phone
EXAMPLE,CONTACT          100 EXAMPLE WAY                 EXAMPLE,CONTACT           (555) 555-0100
                         SAMPLE CITY, SC 00000
Please do not start work without the necessary approved permits for both Underground and Aerial jobs.
Comments: NTP for push light
'''

class FieldDocumentTests(unittest.TestCase):
    def test_large_photo_job_fits_sheet_metadata_without_losing_files(self):
        class Store:
            saved=None
            def get_sheet(self,*args,**kwargs):return {'columns':[{'id':i+1,'title':n} for i,n in enumerate((f.FACTS_COLUMN,f.FILES_COLUMN,f.REVIEW_COLUMN,'Task Name'))],'rows':[{'id':5,'cells':[{'columnId':4,'value':'Synthetic photo job'}]}]}
            def list_row_attachments(self,*args):return [{'id':1000000000000000+i,'name':'field-photo-'+str(i)+'.jpg'} for i in range(100)]
            def update_row(self,sid,rid,values):self.saved=values
        store=Store();result=f._sync_row(store,1,5)
        self.assertEqual(result['files'],100);self.assertLess(len(store.saved[f.FILES_COLUMN]),3900);self.assertEqual(len(json.loads(store.saved[f.FILES_COLUMN]))-1,100)
        self.assertNotIn('Status',store.saved)
    def test_pdf_worker_limits_and_timeout_fail_closed(self):
        with patch.object(f.subprocess,'run') as run:
            self.assertEqual(f.pdf_text(b'x'*(8*1024*1024+1)),'');run.assert_not_called()
        with patch.object(f.subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',12)) as run:
            self.assertEqual(f.pdf_text(b'%PDF-test'),'');self.assertEqual(run.call_args.kwargs['timeout'],12)
            self.assertIn('128*1024*1024',run.call_args.args[0][-1]);self.assertIn('RLIMIT_CPU',run.call_args.args[0][-1])
    def test_nonfinancial_facts(self):
        facts=f.ntp_facts(SAMPLE,'7654321')
        self.assertEqual(facts['prism'],'1234567');self.assertEqual(facts['workOrder'],'7654321')
        self.assertEqual(facts['constructionType'],'Residential');self.assertEqual(facts['coordinator'],'Example, Coordinator')
        self.assertEqual(facts['estimatedCompletion'],'10/27/2026');self.assertEqual(facts['workOrderDate'],'10/20/2026')
        self.assertIn('approved permits',facts['permitNotice']);self.assertEqual(facts['comments'],'NTP for push light')
        self.assertNotIn('999.99',str(facts));self.assertNotIn('_price',facts);self.assertEqual(f.ntp_facts(SAMPLE,'0000000'),{})
    def test_ntp_private_even_without_money_symbol(self):
        with patch.object(f,'pdf_text',return_value=SAMPLE):self.assertEqual(f.classify('document.pdf',b'%PDF-test')[0],'office')
        self.assertEqual(f.classify('NTP.pdf',b'')[0],'office')
        self.assertEqual(f.classify('source.zip',b'')[0],'office')
    def test_scanned_documents_require_review(self):
        with patch.object(f,'scan_text',side_effect=ValueError('unreadable')),patch.object(f,'pdf_text',return_value=''):self.assertEqual(f.classify('scan.pdf',b'%PDF-test')[0],'review')
        with patch.object(f,'image_text',side_effect=ValueError('unreadable')):self.assertEqual(f.classify('image001.jpg',b'photo',True)[0],'review')
        self.assertEqual(f.classify('field-photo.jpg',b'photo',False)[0],'field')
    def test_ntp_package_does_not_hide_nonfinancial_map_or_photo(self):
        with patch.object(f,'pdf_text',return_value='Fiber splice map. Refer to NTP for permitted start date. Ports 1 through 24.'):
            self.assertEqual(f.classify('AFTER_FIBER.pdf',b'map',True)[0],'field')
        with patch.object(f,'image_text',return_value='Fiber ports 1 to 24'):
            self.assertEqual(f.classify('image001.jpg',b'photo',True)[0],'field')
        with patch.object(f,'image_text',return_value='Unit Price: $100'):
            self.assertEqual(f.classify('image001.jpg',b'photo',True)[0],'office')
    def test_nonfinancial_workbook_released_pricing_workbook_blocked(self):
        with patch.object(f,'spreadsheet_text',return_value='Splice ID Quantity Fiber ports'):
            self.assertEqual(f.classify('asbuilt.xlsx',b'workbook',True)[0],'field')
        with patch.object(f,'spreadsheet_text',return_value='Unit Price 100'):
            self.assertEqual(f.classify('asbuilt.xlsx',b'workbook',True)[0],'office')
    def test_phone_shifted_left_of_column_heading(self):
        text=SAMPLE.replace('EXAMPLE,CONTACT           (555) 555-0100','EXAMPLE,CONTACT (555) 555-0100')
        facts=f.ntp_facts(text)
        self.assertEqual(facts['sitePhone'],'(555) 555-0100')
        self.assertEqual(facts['siteContact'],'EXAMPLE,CONTACT')
    def test_technical_pdf_kept_and_email_download_not_released(self):
        with patch.object(f,'pdf_text',return_value='Splice matrix for location FT18. Fiber connections and port assignments.'):self.assertEqual(f.classify('splice-matrix.pdf',b'%PDF-test')[0],'field')
        raw=b'Subject: Outage dispatch\r\nContent-Type: text/plain\r\n\r\nPlease respond to the outage at the job location.'
        self.assertEqual(f.classify('original.eml',raw)[0],'preview')
        self.assertEqual(f.classify('original.eml',raw+b'\nPO Amount: 999.99')[0],'office')
    def test_commercial_text_not_carried_into_field_instructions(self):
        self.assertEqual(f.safe_text('Use port 4\nPO Amount: 999.99\nCall site contact'),'Use port 4\nCall site contact')

if __name__=='__main__':unittest.main()
