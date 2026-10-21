import hashlib, hmac, importlib.util, json, sqlite3, sys, tempfile, time, types, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import contextmanager, ExitStack

with patch.dict(sys.modules, {'graph_import':types.ModuleType('graph_import'),'operations_intake':types.ModuleType('operations_intake')}):
    spec=importlib.util.spec_from_file_location('isolated_intake_review',Path(__file__).with_name('intake_review.py'))
    review=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)

class ReviewTests(unittest.TestCase):
    def test_dismiss_retains_original_and_never_saves_job(self):
        key='d'*64; conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        @contextmanager
        def db():
            with conn:yield conn
        store=Mock();audit=Mock();save=Mock();move=Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review'}])))
            stack.enter_context(patch.object(review,'find_message',return_value={'id':'fictional','subject':'Informational email'}))
            for obj,name,value in [(review.graph,'access_token',lambda:'fake'),(review.graph,'ensure_mail_folder',lambda *a:'archive'),(review.graph,'move_message',move),(review.intake,'audit',audit),(review.intake,'save_message',save)]:
                stack.enter_context(patch.object(obj,name,value,create=True))
            result=review.handle(store,db,{'role':'lead','actor':'lead@example.test','messageKey':key,'action':'dismiss','reason':'Informational only'})
            save.assert_not_called();store.list_row_attachments.assert_not_called()
            self.assertEqual(audit.call_args.kwargs['State'],'Dismissed')
            self.assertIn('lead@example.test',audit.call_args.kwargs['Detail']);self.assertIn('Informational only',audit.call_args.kwargs['Detail'])
            move.assert_called_once_with('fake','fictional','archive')
            self.assertEqual(conn.execute('SELECT state FROM operations_mail').fetchone()[0],'Dismissed')
            self.assertIn('No job was created',result['message'])
        conn.close()

    def test_dismiss_requires_reason_before_mail_access(self):
        key='d'*64
        with patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review'}])),patch.object(review,'find_message') as source:
            with self.assertRaisesRegex(ValueError,'why no job'):
                review.handle(Mock(),Mock(),{'role':'office','actor':'office@example.test','messageKey':key,'action':'dismiss','reason':''})
            source.assert_not_called()

    def test_dismiss_cannot_hide_a_partially_started_job_save(self):
        key='d'*64; conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE portal_intake_review (message_key TEXT PRIMARY KEY, actor TEXT, started REAL, job_key TEXT)')
        conn.execute('INSERT INTO portal_intake_review VALUES (?,?,?,?)',(key,'fictional',time.time(),''))
        @contextmanager
        def db():
            with conn:yield conn
        with patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review'}])),patch.object(review,'find_message') as source:
            with self.assertRaisesRegex(ValueError,'already started'):
                review.handle(Mock(),db,{'role':'office','actor':'office@example.test','messageKey':key,'action':'dismiss','reason':'Informational'})
            source.assert_not_called()
        conn.close()

    def test_dismissed_record_is_idempotent(self):
        key='d'*64
        with patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Dismissed'}])),patch.object(review,'find_message') as source:
            self.assertIn('original is retained',review.handle(Mock(),Mock(),{'role':'office','actor':'office@example.test','messageKey':key,'action':'dismiss'})['message'])
            source.assert_not_called()

    def test_office_detail_reads_names_without_photo_payloads(self):
        key='d'*64
        with ExitStack() as stack:
            stack.enter_context(patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review'}])))
            stack.enter_context(patch.object(review,'find_message',return_value={'id':'fictional','subject':'Dispatch','body':{'content':'Details'}}))
            stack.enter_context(patch.object(review.graph,'access_token',return_value='fake',create=True))
            stack.enter_context(patch.object(review.graph,'_plain_text',side_effect=lambda x:x,create=True))
            names=stack.enter_context(patch.object(review.graph,'list_attachment_metadata',return_value=[{'name':'photo.jpg','size':3000000}],create=True))
            binary=stack.enter_context(patch.object(review.graph,'list_attachments',create=True))
            result=review.handle(Mock(),Mock(),{'role':'office','actor':'office@example.test','messageKey':key,'action':'detail'})
            self.assertEqual(result['files'],['photo.jpg']);names.assert_called_once();binary.assert_not_called()

    def test_signed_route_is_path_bound(self):
        raw=b'{"action":"detail"}';stamp=str(int(time.time()));secret='fictional-secret'
        text='fdgc-mail-bridge-v1\n'+stamp+'\nPOST\n'+review.PATH+'\n'+hashlib.sha256(raw).hexdigest()
        headers={'X-FDGC-Time':stamp,'X-FDGC-Signature':hmac.new(secret.encode(),text.encode(),hashlib.sha256).hexdigest()}
        self.assertTrue(review.authenticated(raw,headers,secret))
        self.assertFalse(review.authenticated(raw+b' ',headers,secret))
        self.assertFalse(review.authenticated(raw,headers,''))

    def test_customer_and_ordinary_tech_cannot_review(self):
        for role in ('customer','tech',''):
            with self.assertRaisesRegex(ValueError,'access required'):
                review.handle(Mock(),Mock(),{'role':role,'actor':'test@example.invalid'})

    def test_reviewed_location_and_ntp_are_validated(self):
        with patch.object(review.intake,'AREA',{'Florence':1},create=True):
            data=dict(address='123 Fictional Rd',market='Florence',city='Test',type='Standard',ntp_due='2026-10-28',body='Push light')
            self.assertEqual(review.reviewed_item(data,{'subject':'Dispatch'})['ntp_due'],'2026-10-28')
            for delta in ({'ntp_due':'bad-date'},{'market':'Unknown'},{'type':'Project'},{'body':''}):
                with self.assertRaises(ValueError):review.reviewed_item(dict(data,**delta),{})

    def test_imported_record_is_idempotent_without_graph(self):
        key='a'*64
        with patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Imported','Job Key':'florence:123'}])):
            result=review.handle(Mock(),Mock(),{'role':'office','actor':'test@example.invalid','messageKey':key})
            self.assertEqual(result['jobKey'],'florence:123')

    def test_lead_detail_never_contains_source_body_or_files(self):
        key='a'*64
        with patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review','Detail':'Missing location'}])),patch.object(review,'find_message',return_value={'subject':'Dispatch','body':{'content':'PO Amount $100'}}),patch.object(review.graph,'access_token',return_value='fake',create=True):
            result=review.handle(Mock(),Mock(),{'role':'lead','actor':'test@example.invalid','messageKey':key,'action':'detail'})
            self.assertNotIn('body',result);self.assertNotIn('files',result)

    def test_existing_match_requires_explicit_link(self):
        key='a'*64;store=Mock()
        with patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review'}])),patch.object(review,'find_message',return_value={'subject':'Dispatch'}),patch.object(review.graph,'access_token',return_value='fake',create=True),patch.object(review,'reviewed_item',return_value={'type':'Outage'}),patch.object(review.intake,'thread_match',return_value=None,create=True),patch.object(review.intake,'locate',return_value=('Florence',1,{'id':123},{}),create=True):
            with self.assertRaisesRegex(ValueError,'existing job matches'):
                review.handle(store,Mock(),{'role':'office','actor':'test@example.invalid','messageKey':key,'action':'resolve'})
            store.list_row_attachments.assert_not_called()

    def test_save_retains_source_before_ledger_and_folder_move(self):
        self.run_save(False)

    def test_uncertain_save_cannot_be_repeated(self):
        self.run_save(True)

    def run_save(self, uncertain):
        key='b'*64; events=[];conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE operations_mail (message_key TEXT PRIMARY KEY, job_key TEXT, state TEXT)')
        @contextmanager
        def db():
            with conn:yield conn
        store=Mock();store.list_row_attachments.return_value=[{'name':'Original-source.eml'},{'name':'site-photo.jpg'}]
        def save(*args, **kwargs):
            events.append('save_with_files')
            self.assertEqual(args[4],[{'name':'site-photo.jpg'}])
            if uncertain:raise RuntimeError('Connection interrupted')
            return 'florence:123'
        with ExitStack() as stack:
            stack.enter_context(patch.object(review,'records',return_value=(1,[{'Message Key':key,'State':'Needs review'}])))
            stack.enter_context(patch.object(review,'find_message',return_value={'id':'fake','subject':'Dispatch'}))
            stack.enter_context(patch.object(review,'reviewed_item',return_value={'type':'Outage'}))
            for obj,name,val in [(review.graph,'access_token',lambda:'fake'),(review.intake,'thread_match',lambda *a:None),(review.intake,'locate',lambda *a:None),(review.graph,'list_attachments',lambda *a,**kw:[{'name':'site-photo.jpg'}]),(review.intake,'save_message',save),(review.intake,'audit',lambda *a,**kw:events.append('ledger')),(review.graph,'ensure_mail_folder',lambda *a:'Imported'),(review.graph,'move_message',lambda *a:events.append('move'))]:
                stack.enter_context(patch.object(obj,name,val,create=True))
            stack.enter_context(patch.object(review.intake,'AREA',{'Florence':1},create=True))
            payload={'role':'office','actor':'test@example.invalid','messageKey':key,'action':'resolve'}
            if uncertain:
                with self.assertRaises(RuntimeError):review.handle(store,db,payload)
                with self.assertRaisesRegex(ValueError,'already recorded'):review.handle(store,db,payload)
                self.assertEqual(events,['save_with_files'])
            else:
                result=review.handle(store,db,payload)
                self.assertEqual(result['jobKey'],'florence:123')
                self.assertEqual(events,['save_with_files','ledger','move'])
                self.assertEqual(conn.execute('SELECT job_key FROM operations_mail').fetchone()[0],'florence:123')
        conn.close()

if __name__=='__main__':unittest.main()


