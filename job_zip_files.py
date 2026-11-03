"""Expand archives already attached to operational jobs and their history."""
import hashlib
import json
import logging
import os
import re
import threading
import time

from operations_intake import AREA, zip_member_files, signature_graphic

LOCK = threading.Lock()
LOG = logging.getLogger(__name__)


def initialize(db):
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS job_zip_files (
            sheet_id INTEGER NOT NULL, row_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL, expected TEXT NOT NULL,
            checked_at REAL NOT NULL, detail TEXT NOT NULL,
            PRIMARY KEY(sheet_id,row_id,attachment_id))''')


def inventory(store):
    for area, sid in AREA.items():
        sheet = store.get_sheet(sid, force=True)
        for row in sheet.get('rows', []):
            for attachment in row.get('attachments', []):
                if attachment.get('attachmentType', 'FILE') == 'FILE' and str(attachment.get('name', '')).lower().endswith('.zip'):
                    yield sid, row['id'], {**attachment, '_row_names': [a['name'] for a in row.get('attachments', [])]}


def attach_members(store, sid, rid, members):
    """Use the same content-hashed names as intake; also recognize older plain names."""
    existing = store.list_row_attachments(sid, rid)
    names = {a['name'] for a in existing}
    hashes = {m.group(1): n for n in names if not n.lower().endswith('.zip')
              and (m := re.search(r'-([a-f0-9]{10})(?:\.[^.]+)*$', n))}
    expected, added = set(), 0
    for name, mime, data in members:
        if signature_graphic(name, mime, data):
            continue
        digest = hashlib.sha256(data).hexdigest()[:10]
        stem, ext = os.path.splitext(name)
        target = stem[:110] + '-' + digest + ext
        if digest in hashes:
            expected.add(hashes[digest])
            continue
        # An earlier office upload may predate content-hashed attachment names.
        duplicate = None
        for old in existing:
            if old['name'] == name and old.get('attachmentType', 'FILE') == 'FILE':
                old_data, _, _ = store.download_attachment(sid, old['id'])
                if hashlib.sha256(old_data).hexdigest()[:10] == digest:
                    duplicate = old['name']
                    break
        if duplicate:
            expected.add(duplicate)
            hashes[digest] = duplicate
            continue
        if target not in names:
            # An ambiguous upload is never blindly retried. The next scan lists
            # the actual row first and recognizes the content-hashed filename.
            try:
                store.attach_file_to_row(sid, rid, filename=target, mime_type=mime, data=data)
            except Exception as exc:
                # Smartsheet refuses executable HTML and some email extensions.
                # Keep those exact bytes as a readable text attachment; never
                # execute the file or rewrap it into another unexpanded ZIP.
                if not re.search(r'"errorCode"\s*:\s*5021\b', str(exc)) or os.path.splitext(name)[1].lower() not in {'.html', '.htm', '.eml'}:
                    raise
                target += '.txt'
                if target not in names:
                    store.attach_file_to_row(sid, rid, filename=target, mime_type='text/plain', data=data)
            names.add(target)
            added += 1
        hashes[digest] = target
        expected.add(target)
    return sorted(expected), added


def run(store, db, limit=10, retry_errors=False):
    if os.getenv('RENDER_SERVICE_ID', '') != 'srv-d9p0jflbedkc73ds5tn0':
        return {'enabled': False, 'message': 'Only the original Graycliff service expands job ZIPs.'}
    if not LOCK.acquire(blocking=False):
        return {'running': True}
    try:
        initialize(db)
        result = {'archives': 0, 'processed': 0, 'added': 0, 'pending': 0, 'errors': []}
        for sid, rid, attachment in inventory(store):
            result['archives'] += 1
            aid = attachment['id']
            candidate_started = time.time()
            fingerprint = json.dumps({k: attachment.get(k) for k in ('id', 'name', 'sizeInKb', 'createdAt', 'modifiedAt', 'version')}, sort_keys=True)
            with db() as c:
                prior = c.execute('SELECT * FROM job_zip_files WHERE sheet_id=? AND row_id=? AND attachment_id=?', (sid, rid, aid)).fetchone()
            if prior and prior['fingerprint'] == fingerprint:
                if prior['state'] == 'Complete':
                    # Confirm extracted files remain attached; restore a removed
                    # member automatically while its original ZIP is retained.
                    current = set(attachment['_row_names'])
                    if set(json.loads(prior['expected'])).issubset(current):
                        continue
                if prior['state'] == 'Needs review' and not retry_errors and time.time() - prior['checked_at'] < 1800:
                    result['errors'].append({'sheet': sid, 'row': rid, 'archive': attachment['name'], 'detail': prior['detail']})
                    continue
            if result['processed'] >= limit:
                result['pending'] += 1
                continue
            # Durable claims prevent a manual backfill and the scheduled worker
            # from uploading the same members concurrently.
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                claim = c.execute('SELECT state,checked_at FROM job_zip_files WHERE sheet_id=? AND row_id=? AND attachment_id=?', (sid, rid, aid)).fetchone()
                if claim and claim['state'] == 'Complete' and claim['checked_at'] >= candidate_started:
                    continue
                if claim and claim['state'] == 'Processing' and time.time() - claim['checked_at'] < 900:
                    result['pending'] += 1
                    continue
                c.execute('INSERT OR REPLACE INTO job_zip_files VALUES (?,?,?,?,?,?,?,?)',
                          (sid, rid, aid, fingerprint, 'Processing', '[]', time.time(), ''))
            expected, state, detail = [], 'Complete', ''
            try:
                if int(attachment.get('sizeInKb') or 0) > 100 * 1024:
                    raise ValueError('ZIP exceeds the automatic processing size limit.')
                data, name, mime = store.download_attachment(sid, aid)
                members, errors = zip_member_files([(name, mime, data)])
                expected, added = attach_members(store, sid, rid, members)
                result['added'] += added
                if errors:
                    raise ValueError('; '.join(errors)[:700])
            except Exception as exc:
                state = 'Needs review'
                detail = str(exc)[:700] if isinstance(exc, ValueError) else 'ZIP files could not all be confirmed; original archive retained. Check this job before retrying.'
                result['errors'].append({'sheet': sid, 'row': rid, 'archive': attachment['name'], 'detail': detail})
                LOG.warning('Job ZIP extraction needs review: sheet=%s row=%s attachment=%s', sid, rid, aid)
            with db() as c:
                c.execute('UPDATE job_zip_files SET state=?,expected=?,checked_at=?,detail=? WHERE sheet_id=? AND row_id=? AND attachment_id=?',
                          (state, json.dumps(expected), time.time(), detail, sid, rid, aid))
            result['processed'] += 1
        return result
    finally:
        LOCK.release()
