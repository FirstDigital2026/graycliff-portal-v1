import json
import sqlite3
import unittest
from unittest.mock import patch
import mail_automation as mail


class SupersededUpdateTests(unittest.TestCase):
    def setUp(self):
        self.connection=sqlite3.connect(':memory:')
        self.connection.row_factory=sqlite3.Row
        self.db=lambda:self.connection
        mail.initialize(self.db)

    def tearDown(self):
        self.connection.close()

    def event(self,seq,status,state,incident='incident-1',key='columbia:123',sent=False):
        payload={'status':status,'job':{'incidentId':incident}}
        self.connection.execute('INSERT INTO mail_event_outbox(event_id,seq,job_key,payload,state,created_at,sent_at) VALUES(?,?,?,?,?,?,?)',
            (str(seq),seq,key,json.dumps(payload),state,'2026-10-23T00:00:00Z','2026-10-23T01:18:00Z' if sent else None))
        self.connection.commit()

    def states(self):
        return {r['seq']:r['state'] for r in self.connection.execute('SELECT seq,state FROM mail_event_outbox')}

    def test_delivered_completion_clears_only_earlier_unsent_progress_errors(self):
        self.event(1,'Working','NEEDS_THREAD_REVIEW')
        self.event(2,'On site','NEEDS_ATTENTION')
        self.event(3,'Working','UNCERTAIN')
        self.event(4,'Working','NEEDS_THREAD_REVIEW',incident='different')
        self.event(5,'Working','NEEDS_THREAD_REVIEW',key='florence:456')
        self.event(6,'Field complete','NEEDS_ATTENTION')
        self.event(7,'Field complete','ACCEPTED',sent=True)
        self.event(8,'Working','NEEDS_THREAD_REVIEW')
        with patch.object(mail.graph,'_request',side_effect=AssertionError('Must not send')):
            self.assertEqual(mail.supersede_delivered_completion(self.db),2)
            self.assertEqual(mail.supersede_delivered_completion(self.db),0)
        self.assertEqual(self.states(),{1:'SUPERSEDED',2:'SUPERSEDED',3:'UNCERTAIN',4:'NEEDS_THREAD_REVIEW',5:'NEEDS_THREAD_REVIEW',6:'NEEDS_ATTENTION',7:'ACCEPTED',8:'NEEDS_THREAD_REVIEW'})
        detail=self.connection.execute('SELECT detail FROM mail_event_outbox WHERE seq=1').fetchone()[0]
        self.assertIn('completion event 7',detail)

    def test_unsent_completion_does_not_hide_a_progress_failure(self):
        self.event(1,'Working','NEEDS_THREAD_REVIEW')
        self.event(2,'Field complete','PAUSED')
        self.assertEqual(mail.supersede_delivered_completion(self.db),0)
        self.assertEqual(self.states()[1],'NEEDS_THREAD_REVIEW')


if __name__=='__main__':unittest.main()
