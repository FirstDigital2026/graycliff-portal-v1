import unittest,sqlite3,hashlib
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch,Mock
import kinetic_finance as k
import commercial_scan as scan

class PrivateScans(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.rates=patch.object(k,'REV',{'TEST1':{'rate':'2.50'},'TEST2':{'rate':'4.00'}});self.rates.start()
        self.costs=patch.object(k,'COST',{});self.costs.start()
    def tearDown(self):self.rates.stop();self.costs.stop();self.c.close()
    def db(self):return self.c
    def page(self,body,finance=''):
        return [{'text':'First Digital Graycliff\nWeek Ending: 1/3/2026\nJob #: 123TEST','invoiceBody':body,'financeBody':finance}]
    def test_signed_detail_total_required(self):
        p=self.page('TEST1 Work $2.50 4 $10.00\nTotal: $10.00')
        self.assertEqual(scan.scanned_invoice(p)['amountCents'],1000)
        with self.assertRaises(ValueError):scan.scanned_invoice(self.page('TEST1 Work $2.50 4 $10.00\nTotal: $11.00'))
    def test_second_read_repairs_ocr_only_with_matching_row_count(self):
        p=self.page('TEST1 Work $2.50 41. $10.00\nTotal: $10.00','$2.50 4 $10.00')
        with self.assertRaises(ValueError):scan.scanned_invoice(p)
        self.assertEqual(scan.scanned_invoice(p,alternative=True)['amountCents'],1000)
        p[0]['financeBody']+='\n$4.00 1 $4.00'
        with self.assertRaises(ValueError):scan.scanned_invoice(p,alternative=True)
    def test_unknown_code_never_becomes_kinetic_by_town(self):
        with self.assertRaises(ValueError):scan.scanned_invoice(self.page('OTHER Lewisville $2.50 4 $10.00\nTotal: $10.00'))
    def mime(self):
        m=EmailMessage();m['Subject']='Forwarded billing';m.set_content('Original forwarding copy')
        m.add_attachment(b'%PDF retained scan',maintype='application',subtype='pdf',filename='scan.pdf')
        return m.as_bytes()
    def test_shared_copy_removed_after_verified_retention_before_ocr(self):
        raw=self.mime();k.initialize(self.db)
        with self.db() as c:c.execute("INSERT INTO private_commercial_settings VALUES ('cleanup_enabled','yes')")
        def check(*a,**kw):
            r=self.c.execute('SELECT mime,mime_hash FROM private_commercial_messages').fetchone()
            self.assertEqual(r['mime'],raw);self.assertEqual(hashlib.sha256(raw).hexdigest(),r['mime_hash'])
            self.assertEqual(self.c.execute('SELECT state FROM private_commercial_jobs').fetchone()['state'],'Queued')
        graph=SimpleNamespace(get_message_mime=lambda *a:raw,mailbox=lambda:'graycliffjobs@firstdigitalsc.com',GRAPH_ROOT='https://graph.microsoft.com/v1.0',_request=Mock(side_effect=check))
        with patch.dict('sys.modules',{'graph_import':graph}),patch.object(k,'kick') as kick,patch.object(k,'PdfReader') as reader:
            k.ingest(self.db,{'id':'forward','from':{'emailAddress':{'address':'thomas.bramlette@firstdigitalsc.com'}}},[{'name':'scan.pdf'}],'source','token')
        graph._request.assert_called_once();reader.assert_not_called();kick.assert_called_once()
        self.assertIn('shared copy removed',self.c.execute('SELECT state FROM private_commercial_messages').fetchone()['state'])
    def test_incomplete_private_attachment_never_allows_removal(self):
        graph=SimpleNamespace(get_message_mime=lambda *a:self.mime(),_request=Mock())
        with patch.dict('sys.modules',{'graph_import':graph}),patch.object(k,'kick'):
            with self.assertRaises(RuntimeError):k.ingest(self.db,{'id':'forward','from':{'emailAddress':{'address':'thomas.bramlette@firstdigitalsc.com'}}},[{'name':'missing.pdf'}],'source','token')
        graph._request.assert_not_called()

if __name__=='__main__':unittest.main()
