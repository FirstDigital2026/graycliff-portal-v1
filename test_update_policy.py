import json,sqlite3,tempfile,unittest,uuid
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import mail_automation as m

STATUSES=['Today','En route','On site','Working','On hold','Canceled','Field complete']
class Store:
    status='Complete';kind='Outage';incident='TEST-INCIDENT';billing='';exists=True
    def get_sheet(self,*args,**kwargs):
        names=['Status','Portal Job Type','Portal Incident ID','Portal Billing Status']
        return {'columns':[{'id':i+1,'title':name} for i,name in enumerate(names)],'rows':[{'id':123,'cells':[{'columnId':i+1,'value':value} for i,value in enumerate([self.status,self.kind,self.incident,self.billing])]}] if self.exists else []}

class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(__file__).parent/('qa-policy-'+str(uuid.uuid4()));self.tmp.mkdir();self.path=self.tmp/'events.sqlite3';self.store=Store()
        @contextmanager
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
            try:
                with c:yield c
            finally:c.close()
        self.db=db;m.initialize(db)
        with db() as c:c.execute("INSERT INTO mail_bridge_meta VALUES('live_after','2000-01-01T00:00:00+00:00')")
        self.seq=0
        self.armed=patch.object(m,'SEND_ARMED',True);self.armed.start()
        self.token=patch.object(m.graph,'access_token',return_value='fixture-token');self.token.start()
        self.transport=patch.object(m.graph,'_request');self.sent=self.transport.start()
    def tearDown(self):
        self.transport.stop();self.token.stop();self.armed.stop();self.path.unlink();self.tmp.rmdir()
    def event(self,kind,status,**extra):
        self.seq+=1
        return dict(eventId=str(uuid.uuid4()),seq=self.seq,jobKey='florence:123',sheetId='1',rowId='123',at=m.now(),action='status',committed=True,status=status,note='Fixture update only.',nocTechnician='Fixture NOC',job={'type':kind,'title':'TEST ONLY JOB','incidentId':'TEST-INCIDENT'},**extra)
    def queue(self,e):
        route={'to':['recipient@example.com'],'cc':[],'subject':'Fixture only','messageId':'<fixture@example.com>'}
        draft=m.draft_event(e,route)
        with self.db() as c:c.execute('INSERT INTO mail_event_outbox(event_id,seq,job_key,payload,state,created_at,draft,approved_at) VALUES(?,?,?,?,?,?,?,?)',(e['eventId'],e['seq'],e['jobKey'],json.dumps(e),'READY',e['at'],json.dumps(draft),m.now()))
    def state(self,e):
        with self.db() as c:return c.execute('SELECT state FROM mail_event_outbox WHERE event_id=?',(e['eventId'],)).fetchone()[0]
    def test_every_outage_update_sends_even_after_later_status(self):
        for status in STATUSES+['Working','Working']:
            e=self.event('Outage',status);self.assertTrue(m.eligible(e));self.queue(e);m.dispatch(self.db,e['eventId'],self.store);self.assertEqual(self.state(e),'ACCEPTED')
            with self.assertRaises(RuntimeError):m.dispatch(self.db,e['eventId'],self.store)
        self.assertEqual(self.sent.call_count,len(STATUSES)+2)
    def test_all_other_categories_are_completion_only_at_ingest_and_dispatch(self):
        for kind in ['Standard','CRQ','Project']:
            self.store.kind=kind
            for status in STATUSES:
                e=self.event(kind,status);self.assertEqual(m.eligible(e),status=='Field complete');self.queue(e);m.dispatch(self.db,e['eventId'],self.store);self.assertEqual(self.state(e),'ACCEPTED' if status=='Field complete' else 'NOT_REQUIRED')
        self.assertEqual(self.sent.call_count,3)
    def test_uncommitted_or_nonstatus_events_never_send(self):
        for action,committed in [('status',False),('reassignment',True),('planning',True),('claim',True)]:
            e=self.event('Outage','Working');e.update(action=action,committed=committed);self.assertFalse(m.eligible(e));self.queue(e);m.dispatch(self.db,e['eventId'],self.store);self.assertEqual(self.state(e),'NOT_REQUIRED')
        self.sent.assert_not_called()
    def test_reopened_nonoutage_completion_is_held(self):
        e=self.event('Standard','Field complete');self.store.kind='Standard';self.store.status='In Progress';self.queue(e);m.dispatch(self.db,e['eventId'],self.store);self.assertEqual(self.state(e),'SUPERSEDED');self.sent.assert_not_called()
    def test_job_identity_billing_and_missing_row_guards_still_hold(self):
        for attr,value in [('kind','Standard'),('incident','OTHER-INCIDENT'),('billing','Submitted'),('exists',False)]:
            self.store=Store();setattr(self.store,attr,value);e=self.event('Outage','On site');self.queue(e);m.dispatch(self.db,e['eventId'],self.store);self.assertEqual(self.state(e),'SUPERSEDED')
        self.sent.assert_not_called()

if __name__=='__main__':unittest.main()
