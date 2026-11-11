import hashlib
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

import kinetic_finance as finance
import operations_intake as intake


class FinancialRoutingTests(unittest.TestCase):
    def setUp(self):
        self.connection=sqlite3.connect(':memory:')
        self.connection.row_factory=sqlite3.Row
        self.key='a'*64
        self.connection.executescript('''
          CREATE TABLE private_commercial_messages(message_key TEXT PRIMARY KEY,mime BLOB,mime_hash TEXT);
          CREATE TABLE private_commercial_jobs(message_key TEXT PRIMARY KEY,state TEXT);
          CREATE TABLE portal_intake_review(message_key TEXT PRIMARY KEY,actor TEXT,job_key TEXT);
        ''')
        self.ledger={'columns':[{'id':1,'title':'Message Key'},{'id':2,'title':'State'}],
                     'rows':[{'id':10,'cells':[{'columnId':1,'value':self.key},{'columnId':2,'value':'Needs review'}]}]}

    @contextmanager
    def db(self):
        with self.connection:
            yield self.connection

    def tearDown(self):
        self.connection.close()

    def save_archive(self, corrupt=False):
        raw=b'Complete fictional original and PDF'
        self.connection.execute('INSERT INTO private_commercial_messages VALUES (?,?,?)',
                                (self.key,raw,'invalid' if corrupt else hashlib.sha256(raw).hexdigest()))
        self.connection.execute("INSERT INTO private_commercial_jobs VALUES (?,'Complete')",(self.key,))

    def test_plain_forwarded_recap_routes_before_scanned_pdf_read(self):
        with patch.object(intake.graph,'_candidate_document_texts',side_effect=AssertionError('No scan needed')):
            self.assertTrue(finance.candidate({'subject':'FW: [EXTERNAL] Re: Recap'},[{'name':'Example WE 12-9.pdf'}]))
        with patch.object(intake.graph,'_candidate_document_texts',return_value=[]):
            self.assertFalse(finance.candidate({'subject':'FW: Recap'},[]))
            self.assertFalse(finance.candidate({'subject':'PID 7999991 assignment'},[{'name':'map.pdf'}]))

    def test_completed_exact_private_archive_clears_stale_review(self):
        self.save_archive()
        with patch.object(intake,'audit') as audit:
            self.assertEqual(intake.reconcile_terminal_receipts(Mock(),self.db,1,self.ledger),{self.key})
        self.assertEqual(audit.call_args.kwargs['State'],'Financial complete')

    def test_corrupt_or_unrelated_archive_does_not_clear_review(self):
        self.save_archive(corrupt=True)
        with patch.object(intake,'audit') as audit:
            self.assertEqual(intake.reconcile_terminal_receipts(Mock(),self.db,1,self.ledger),set())
        audit.assert_not_called()
        self.assertIsNone(intake.terminal_receipt(self.db,'b'*64))

    def test_partial_job_save_remains_for_office_review(self):
        self.save_archive()
        self.connection.execute('INSERT INTO portal_intake_review VALUES (?,?,?)',(self.key,'office@example.test',''))
        with patch.object(intake,'audit') as audit:
            self.assertEqual(intake.reconcile_terminal_receipts(Mock(),self.db,1,self.ledger),set())
        audit.assert_not_called()

    def test_durable_dismissal_repairs_overwritten_ledger(self):
        self.connection.execute('INSERT INTO portal_intake_review VALUES (?,?,?)',(self.key,'office@example.test','DISMISSED'))
        with patch.object(intake,'audit') as audit:
            self.assertEqual(intake.reconcile_terminal_receipts(Mock(),self.db,1,self.ledger),{self.key})
        self.assertEqual(audit.call_args.kwargs['State'],'Dismissed')


if __name__=='__main__':
    unittest.main()
