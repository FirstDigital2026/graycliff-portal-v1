"""Fictional dispatch examples; no production emails or job records."""
import hashlib, sqlite3, unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import Mock, patch
import operations_intake as intake
import intake_formats as formats


class Store:
    def __init__(self, market='Columbia', **values):
        self.market=market
        self.values=values
    def get_sheet(self,sid,**kw):
        if sid!=intake.SEARCH_SHEETS[self.market]:return {'columns':[],'rows':[]}
        columns=[{'id':i,'title':name} for i,name in enumerate(self.values,1)]
        return {'columns':columns,'rows':[{'id':99,'cells':[{'columnId':c['id'],'value':self.values[c['title']]} for c in columns]}]}


class ReconciliationTests(unittest.TestCase):
    def message(self,body,subject='FW: Fiber Cut - HUB TWO - INC009999991111'):
        return {'id':'fictional','subject':subject,'body':{'content':body},'receivedDateTime':'2026-10-23T13:00:00Z'}

    def test_explicit_dispatch_beats_quoted_signature_addresses(self):
        body='Spectrum requests assistance.\nMove your crews over to 901 Fictional Dr, Newberry, SC 29108\nFrom: Previous update\nDamage near 902 Fictional Rd, Newberry, SC\nSignature 903 Office Dr, Florence, SC 29501'
        with patch.object(intake.graph,'_candidate_document_texts',return_value=[]):
            item=intake.parse(self.message(body),[])
        self.assertEqual((item['address'],item['city'],item['market']),('901 Fictional Dr','Newberry','Columbia'))
        self.assertEqual(item['incident_id'],'INC009999991111')

    def test_latest_explicit_instruction_wins(self):
        body='Move your crews over to 901 Fictional Dr, Newberry, SC\nFrom: Older\nSend a splicer to 902 Fictional Rd, Florence, SC'
        self.assertEqual(formats.dispatch_site(body),('901 Fictional Dr','Newberry'))

    def test_two_current_destinations_still_require_review(self):
        with self.assertRaisesRegex(ValueError,'Current dispatch'):
            formats.dispatch_site('Move your crews to 901 Fictional Dr, Newberry, SC and 902 Fictional Rd, Newberry, SC')

    def test_exact_incident_finds_completed_history_with_renamed_site(self):
        store=Store('History',**{'Task Name':'Restoration at another named site','Portal Incident ID':'INC009999991111','Status':'Billed'})
        result=intake.locate(store,dict(pid='',wo='',address='901 Fictional Dr',city='Newberry',incident_id='INC009999991111'))
        self.assertEqual((result[0],result[2]['id']),('History',99))

    def test_same_hub_different_incident_is_not_a_duplicate(self):
        store=Store(**{'Task Name':'901 Fictional Dr INC009999992222','Portal Job Type':'Outage','Status':'Field complete'})
        result=intake.locate(store,dict(pid='',wo='',address='901 Fictional Dr',city='',type='Outage',incident_id='INC009999991111'))
        self.assertIsNone(result)

    def test_ambiguous_addresses_can_link_an_exact_existing_incident(self):
        store=Store('History',**{'Task Name':'Hub restoration INC009999991111','Portal Job Type':'Outage','Status':'Billed'})
        message=self.message('Spectrum\n901 Fictional Dr, Newberry, SC\n902 Fictional Rd, Florence, SC')
        with patch.object(intake.graph,'_candidate_document_texts',return_value=[]):
            item=intake.message_item(store,message,[])
        self.assertEqual(item['market'],'History')
        self.assertEqual(item['incident_id'],'INC009999991111')

    def test_missing_due_date_does_not_prevent_clear_job_creation(self):
        store=Store();store.list_sheets=lambda:[]
        item=dict(pid='7999991',wo='',address='901 Fictional Dr',city='Newberry',market='Columbia',type='Standard',ntp_due='',body='Ready for scheduling',subject='PID 7999991 NTP')
        writes=[]
        def write(store,sid,values,rid=None):writes.append(values);return {'id':100}
        with patch.object(intake,'write',side_effect=write),patch.object(intake,'ensure_columns'),patch.object(intake,'capture_email_files'),patch.dict('sys.modules',{'field_documents':Mock(sync_row=Mock())}):
            result=intake.save_message(store,'fake',self.message('Spectrum'),item,[],'x'*64,dict(created=0,updated=0))
        self.assertEqual(result,'columbia:100')
        created=next(v for v in writes if 'Task Name' in v)
        self.assertNotIn('Due Date',created)
        self.assertIn('Confirm scheduling date',created['Notes'])

    def test_link_to_completed_job_preserves_status_and_dates(self):
        store=Store('History',**{'Task Name':'7999991 · 901 Fictional Dr','Portal Project ID':'7999991','Status':'Billed','Due Date':'2026-09-01','Portal Billing Status':'Submitted'})
        item=dict(pid='7999991',wo='',address='901 Fictional Dr',city='',market='Columbia',type='Standard',ntp_due='2026-11-01',body='Source followup',subject='PID 7999991')
        with patch.object(intake,'write',return_value={'id':99}) as write,patch.object(intake,'ensure_columns'),patch.object(intake,'capture_email_files') as capture:
            result=intake.save_message(store,'fake',self.message('Spectrum'),item,[],'x'*64,dict(created=0,updated=0))
        self.assertEqual(result,'history:99')
        self.assertNotIn('Status',write.call_args.args[2]);self.assertNotIn('Due Date',write.call_args.args[2])
        capture.assert_called_once()

    def test_stale_review_receipt_requires_exact_retained_source(self):
        store=Store('History',**{'Task Name':'Fictional finished work'})
        store.list_row_attachments=Mock(return_value=[{'name':'Unrelated.eml'}])
        self.assertFalse(intake.retained_import(store,'a'*64,'history:99'))
        store.list_row_attachments.return_value=[{'name':'Email-'+('a'*20)+'.eml'}]
        self.assertTrue(intake.retained_import(store,'a'*64,'history:99'))
        self.assertFalse(intake.retained_import(store,'a'*64,'history:100'))

    def test_existing_thread_keeps_completed_job_and_records_incident(self):
        store=Store(**{'Task Name':'902 Fictional Hwy','Portal Job Type':'Outage','Portal Incident ID':'EV-FICTIONAL','Portal Email Threads':'["fictional-thread"]','Status':'Field complete'})
        message=dict(self.message('Spectrum'),conversationId='fictional-thread')
        item=dict(pid='',wo='',address='901 Fictional Dr',city='Newberry',market='Columbia',type='Outage',ntp_due='',incident_id='INC009999991111',subject=message['subject'],body='Move crews to the confirmed damage site')
        with patch.object(intake,'write',return_value={'id':99}) as write,patch.object(intake,'ensure_columns'),patch.object(intake,'capture_email_files'),patch.dict('sys.modules',{'field_documents':Mock(sync_row=Mock())}):
            result=intake.save_message(store,'fake',message,item,[],'x'*64,dict(created=0,updated=0))
        self.assertEqual(result,'columbia:99')
        first=write.call_args_list[0].args[2]
        self.assertEqual(first['Portal Incident ID'],'INC009999991111')
        self.assertNotIn('Status',first);self.assertNotIn('Task Name',first)

    def test_fixed_hold_is_retried_once_and_reconciles_without_duplicate_save(self):
        self.run_recovery(False)

    def test_uncertain_portal_claim_is_never_replayed(self):
        self.run_recovery(True)

    def run_recovery(self, claimed):
        message=dict(self.message('Spectrum'),internetMessageId='<fictional-source@example.test>')
        key=hashlib.sha256(message['internetMessageId'].encode()).hexdigest()
        connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
        @contextmanager
        def db():
            with connection:yield connection
        if claimed:
            connection.execute('CREATE TABLE portal_intake_review (message_key TEXT PRIMARY KEY, actor TEXT, started REAL, job_key TEXT)')
            connection.execute('INSERT INTO portal_intake_review VALUES (?,?,?,?)',(key,'office@example.test',1,''))
        values={'Message Key':key,'State':'Needs review','Detail':'NTP completion date is missing. Office review required.'}
        columns=[{'id':i,'title':k} for i,k in enumerate(values,1)]
        ledger={'columns':columns,'rows':[{'id':1,'cells':[{'columnId':c['id'],'value':values[c['title']]} for c in columns]}]}
        store=Mock();store.get_sheet.return_value=ledger
        with ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ',{'RENDER_SERVICE_ID':'srv-d9p0jflbedkc73ds5tn0'}))
            for name,value in [('ledger_id',1),('seed_imported_threads',None),('notify_import_assignment',None),('retained_import',True),('message_item',{}),('audit',None)]:
                stack.enter_context(patch.object(intake,name,return_value=value))
            save=stack.enter_context(patch.object(intake,'save_message',return_value='history:99'))
            for name,value in [('access_token','fake'),('ensure_mail_folder','fictional-folder'),('list_recent_messages',[]),('list_folder_messages',[message]),('get_message_details',message),('list_attachments',[]),('move_message',None)]:
                stack.enter_context(patch.object(intake.graph,name,return_value=value))
            stack.enter_context(patch.object(intake.formats,'enrich_push_parent',side_effect=lambda t,m,a:a))
            stack.enter_context(patch.object(intake.urllib.request,'urlopen',return_value=Mock()))
            intake.run(store,db)
            intake.run(store,db)
            if claimed:save.assert_not_called()
            else:
                save.assert_called_once()
                self.assertIsNone(save.call_args.kwargs['db'])
                self.assertEqual(connection.execute('SELECT job_key,state FROM operations_mail').fetchone()[:],('history:99','Imported'))
        connection.close()


if __name__=='__main__':unittest.main()
