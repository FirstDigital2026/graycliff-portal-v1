import unittest,io
from unittest.mock import patch
import field_documents as f

SAMPLE='''PRISM ID:         1234567                           Work Order #:       7654321
PO #:             1112223334                        PO Amount:          999.99
Const Type:       Residential                       Const Coordinator:  Example, Coordinator
Work Order Date:  10/20/2026                        Est. Completion:    10/27/2026
Vendor Name:      Example Contractor                Issued By:          Example, Coordinator

Job                      Job Address                     Site Contact Name         Site Phone
EXAMPLE,CONTACT          614 5TH STREET EXT              EXAMPLE,CONTACT           (555) 555-0100
                         JACKSON, SC 29831
Please do not start work without the necessary approved permits for both Underground and Aerial jobs.
Comments: NTP for push light
'''

class FieldDocumentTests(unittest.TestCase):
    def test_nonfinancial_facts(self):
        facts=f.ntp_facts(SAMPLE,'7654321')
        self.assertEqual(facts['prism'],'1234567');self.assertEqual(facts['workOrder'],'7654321')
        self.assertEqual(facts['constructionType'],'Residential');self.assertEqual(facts['coordinator'],'Example, Coordinator')
        self.assertEqual(facts['estimatedCompletion'],'10/27/2026');self.assertEqual(facts['workOrderDate'],'10/20/2026')
        self.assertIn('approved permits',facts['permitNotice']);self.assertEqual(facts['comments'],'NTP for push light')
        self.assertNotIn('999.99',str(facts));self.assertNotIn('_price',facts);self.assertEqual(f.ntp_facts(SAMPLE,'0000000'),{})
    def test_ntp_private_even_without_money_symbol(self):
        with patch.object(f.graph,'_pdf_text',return_value=SAMPLE):self.assertEqual(f.classify('document.pdf',b'%PDF-test')[0],'office')
        self.assertEqual(f.classify('NTP.pdf',b'')[0],'office')
        self.assertEqual(f.classify('source.zip',b'')[0],'office')
    def test_scanned_documents_require_review(self):
        with patch.object(f.graph,'_pdf_text',return_value=''):self.assertEqual(f.classify('scan.pdf',b'%PDF-test')[0],'review')
        self.assertEqual(f.classify('image001.jpg',b'photo',True)[0],'review')
        self.assertEqual(f.classify('field-photo.jpg',b'photo',False)[0],'field')
    def test_phone_shifted_left_of_column_heading(self):
        text=SAMPLE.replace('EXAMPLE,CONTACT           (555) 555-0100','EXAMPLE,CONTACT (555) 555-0100')
        facts=f.ntp_facts(text)
        self.assertEqual(facts['sitePhone'],'(555) 555-0100')
        self.assertEqual(facts['siteContact'],'EXAMPLE,CONTACT')
    def test_technical_pdf_kept_and_email_download_not_released(self):
        with patch.object(f.graph,'_pdf_text',return_value='Splice matrix for location FT18. Fiber connections and port assignments.'):self.assertEqual(f.classify('splice-matrix.pdf',b'%PDF-test')[0],'field')
        raw=b'Subject: Outage dispatch\r\nContent-Type: text/plain\r\n\r\nPlease respond to the outage at the job location.'
        self.assertEqual(f.classify('original.eml',raw)[0],'preview')
        self.assertEqual(f.classify('original.eml',raw+b'\nPO Amount: 999.99')[0],'office')
    def test_commercial_text_not_carried_into_field_instructions(self):
        self.assertEqual(f.safe_text('Use port 4\nPO Amount: 999.99\nCall site contact'),'Use port 4\nCall site contact')

if __name__=='__main__':unittest.main()
