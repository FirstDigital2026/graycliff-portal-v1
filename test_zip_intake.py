import base64
import io
import unittest
import zipfile
from unittest.mock import patch
import operations_intake as intake


def zipped(items):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in items:
            archive.writestr(name, content)
    return stream.getvalue()


class Store:
    def __init__(self):
        self.files = []
    def list_row_attachments(self, *args):
        return [{'name': name} for name, _, _ in self.files]
    def attach_file_to_row(self, *args, filename, mime_type, data):
        self.files.append((filename, mime_type, data))


class ZipIntakeTests(unittest.TestCase):
    def test_nested_archive_exposes_each_document_without_unpacking_xlsx(self):
        workbook = zipped([('xl/workbook.xml', '<workbook/>')])
        data = zipped([('plans/map.pdf', b'PDF'), ('work.xlsx', workbook),
                       ('nested.zip', zipped([('shot.png', b'PNG')])), ('__MACOSX/._map.pdf', b'metadata')])
        files, errors = intake.zip_member_files([('package.zip', 'application/zip', data)])
        self.assertEqual([n for n, _, _ in files], ['map.pdf', 'work.xlsx', 'nested.zip', 'shot.png'])
        self.assertFalse(errors)

    def test_original_and_individual_files_saved_once_even_when_zip_already_saved(self):
        data = zipped([('plan.pdf', b'PDF'), ('photos/shot.jpg', b'PHOTO')])
        attachment = {'@odata.type': '#microsoft.graph.fileAttachment', 'name': 'work.zip',
                      'contentType': 'application/zip', 'contentBytes': base64.b64encode(data).decode()}
        store = Store()
        with patch.object(intake.graph, 'get_message_mime', return_value=b'Original email'), patch.object(intake, 'source_attachments', return_value=[('work.zip', 'application/zip', data)]):
            intake.capture_email_files(store, '', {'id': 'message'}, 1, 2, [attachment], 'a'*64)
            self.assertEqual(len(store.files), 4)
            self.assertEqual({mime for _, mime, _ in store.files}, {'message/rfc822', 'application/zip', 'application/pdf', 'image/jpeg'})
            intake.capture_email_files(store, '', {'id': 'message'}, 1, 2, [attachment], 'a'*64)
            self.assertEqual(len(store.files), 4)
            # Backfill members when the ZIP was retained before this feature.
            store.files = [f for f in store.files if f[1] in {'message/rfc822', 'application/zip'}]
            intake.capture_email_files(store, '', {'id': 'message'}, 1, 2, [attachment], 'a'*64)
            self.assertEqual(len(store.files), 4)

    def test_invalid_zip_retains_original_before_reporting_review(self):
        attachment = {'@odata.type': '#microsoft.graph.fileAttachment', 'name': 'broken.zip', 'contentType': 'application/zip', 'contentBytes': base64.b64encode(b'bad ZIP').decode()}
        store = Store()
        with patch.object(intake.graph, 'get_message_mime', return_value=b'Original email'):
            with self.assertRaisesRegex(ValueError, 'original email and ZIP retained'):
                intake.capture_email_files(store, '', {'id': 'message'}, 1, 2, [attachment], 'a'*64)
        self.assertEqual(len(store.files), 2)

    def test_path_and_expansion_limits_require_review(self):
        for name in ['../escape.pdf', '/escape.pdf', 'C:/escape.pdf']:
            files, errors = intake.zip_member_files([('unsafe.zip', 'application/zip', zipped([(name, b'data')]))])
            self.assertFalse(files)
            self.assertTrue(errors)
        files, errors = intake.zip_member_files([('large.zip', 'application/zip', zipped([('one.pdf', b'12345')]))], max_bytes=4)
        self.assertFalse(files)
        self.assertTrue(errors)


if __name__ == '__main__':
    unittest.main()
