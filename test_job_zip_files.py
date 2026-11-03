from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import job_zip_files as worker


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        for name, data in files:
            z.writestr(name, data)
    return output.getvalue()


class Store:
    def __init__(self):
        self.files = {1: {'id': 1, 'name': 'old-work.zip', 'attachmentType': 'FILE', 'mimeType': 'application/zip'}}
        self.data = {1: archive([('design/map.pdf', b'MAP'), ('quantities.xlsx', b'WORKBOOK')])}
        self.downloads = 0
        self.timeout_after_save = False
    def get_sheet(self, sid, **kw):
        return {'rows': [{'id': 7, 'attachments': list(self.files.values()), 'cells': [{'value': 'On site'}]}]}
    def list_row_attachments(self, *args):
        return list(self.files.values())
    def download_attachment(self, sid, aid):
        self.downloads += 1
        return self.data[aid], self.files[aid]['name'], self.files[aid]['mimeType']
    def attach_file_to_row(self, *args, filename, mime_type, data):
        aid = max(self.files) + 1
        self.files[aid] = {'id': aid, 'name': filename, 'mimeType': mime_type, 'attachmentType': 'FILE'}
        self.data[aid] = data
        if self.timeout_after_save:
            self.timeout_after_save = False
            raise TimeoutError('Simulated timeout after committed upload')


class ExistingZipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.temp.name) / 'receipts.sqlite'
        @contextmanager
        def db():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                with c:
                    yield c
            finally:
                c.close()
        self.db = db
        self.store = Store()
        self.env = patch.dict(os.environ, {'RENDER_SERVICE_ID': 'srv-d9p0jflbedkc73ds5tn0'})
        self.env.start()
        self.sheets = patch.object(worker, 'SEARCH_SHEETS', {'Florence': 99})
        self.sheets.start()
    def tearDown(self):
        self.env.stop()
        self.sheets.stop()
        self.temp.cleanup()
    def test_backfill_and_repeated_scans_preserve_zip_without_duplicate_members(self):
        result = worker.run(self.store, self.db)
        self.assertEqual((result['processed'], result['added'], result['errors']), (1, 2, []))
        self.assertIn(1, self.store.files)
        self.assertEqual(len(self.store.files), 3)
        result = worker.run(self.store, self.db)
        self.assertEqual((result['processed'], result['added']), (0, 0))
        self.assertEqual(self.store.downloads, 1)
    def test_removed_member_is_restored_automatically(self):
        worker.run(self.store, self.db)
        del self.store.files[2]
        result = worker.run(self.store, self.db)
        self.assertEqual(result['added'], 1)
        self.assertEqual(len(self.store.files), 3)
    def test_existing_unhashed_office_upload_is_reused(self):
        self.store.files[2] = {'id': 2, 'name': 'map.pdf', 'mimeType': 'application/pdf'}
        self.store.data[2] = b'MAP'
        result = worker.run(self.store, self.db)
        self.assertEqual(result['added'], 1)
        self.assertEqual(len(self.store.files), 3)
    def test_revised_same_name_file_is_retained_as_separate_version(self):
        self.store.files[2] = {'id': 2, 'name': 'map.pdf', 'mimeType': 'application/pdf'}
        self.store.data[2] = b'OLDER MAP'
        result = worker.run(self.store, self.db)
        self.assertEqual(result['added'], 2)
        self.assertEqual(self.store.data[2], b'OLDER MAP')
    def test_ambiguous_upload_recovers_without_duplicate(self):
        self.store.timeout_after_save = True
        first = worker.run(self.store, self.db)
        self.assertEqual(len(first['errors']), 1)
        second = worker.run(self.store, self.db, retry_errors=True)
        self.assertEqual((second['added'], second['errors']), (1, []))
        self.assertEqual(len(self.store.files), 3)
    def test_invalid_zip_records_issue_and_retains_original(self):
        self.store.data[1] = b'broken ZIP'
        result = worker.run(self.store, self.db)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(len(self.store.files), 1)
        self.assertEqual(worker.run(self.store, self.db)['processed'], 0)
    def test_batch_limit_defers_work_and_other_services_do_not_scan(self):
        self.assertEqual(worker.run(self.store, self.db, limit=0)['pending'], 1)
        with patch.dict(os.environ, {'RENDER_SERVICE_ID': 'another-service'}):
            self.assertFalse(worker.run(self.store, self.db)['enabled'])
    def test_live_durable_claim_is_not_replayed(self):
        worker.initialize(self.db)
        import time
        with self.db() as c:
            c.execute('INSERT INTO job_zip_files VALUES (?,?,?,?,?,?,?,?)', (99, 7, 1, '', 'Processing', '[]', time.time(), ''))
        self.assertEqual(worker.run(self.store, self.db)['pending'], 1)
        self.assertEqual(self.store.downloads, 0)


if __name__ == '__main__':
    unittest.main()
