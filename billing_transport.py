"""Bounded Graph draft uploads for reviewed billing packages.

No transport retries the send operation. An uncertain send remains held by the
billing outbox. Pre-send upload failures may be retried because no send occurred.
"""
import base64
import json
import re
import hashlib
import urllib.parse
import urllib.request
from collections import Counter
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

MAX_FILE = 15 * 1024 * 1024
MAX_PACKAGE = 20 * 1024 * 1024
DIRECT_BUDGET = 1500000
CHUNK = 8 * 320 * 1024


class BeforeSendFailure(ValueError):
    pass


def content_type(name):
    ext = name.lower().rsplit('.', 1)[-1]
    return {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','pdf': 'application/pdf', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'xlsm': 'application/vnd.ms-excel.sheet.macroEnabled.12', 'csv': 'text/csv',
            'txt': 'text/plain', 'sor': 'application/octet-stream'}.get(ext, 'application/octet-stream')


def validate_size(files):
    if any(f['size'] > MAX_FILE for f in files):
        raise ValueError('Each billing file must be at most 15 MB. Nothing was sent.')
    if sum(f['size'] for f in files) > MAX_PACKAGE:
        raise ValueError('The billing package exceeds 20 MB. Choose fewer workbook photos or supporting files. Nothing was sent.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def put_chunk(url, data, start, total):
    """Graph supplies this short-lived, pre-authorized URL. Never log it."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'outlook.office.com', 'outlook.office365.com'} or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Microsoft returned an unsupported attachment upload address.')
    req = urllib.request.Request(url, data=data, method='PUT', headers={
        'Content-Type': 'application/octet-stream', 'Content-Length': str(len(data)),
        'Content-Range': f'bytes {start}-{start + len(data) - 1}/{total}'})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=35) as response:
        status = response.status
        raw = response.read(65537)
    if len(raw) > 65536 or status not in (200, 201):
        raise ValueError('Microsoft did not confirm the attachment upload.')
    result = json.loads(raw) if raw else {}
    if start + len(data) == total:
        if status != 201:
            raise ValueError('Microsoft did not confirm the complete attachment.')
    else:
        ranges=result.get('nextExpectedRanges',[])
        match=re.fullmatch(r'(\d+)(?:-(\d*))?',str(ranges[0])) if len(ranges)==1 else None
        if not match or int(match[1])!=start+len(data):
            raise ValueError('Microsoft requested an unexpected attachment range: '+repr(ranges))


def attachment_digest(url,token):
    """Outlook's metadata size includes overhead. Verify the actual raw bytes."""
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname!='graph.microsoft.com':
        raise ValueError('Unsupported attachment verification address.')
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/octet-stream'})
    digest=hashlib.sha256();size=0
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=35) as response:
        while chunk:=response.read(256*1024):
            size+=len(chunk)
            if size>MAX_FILE:raise ValueError('An uploaded attachment exceeds its allowed size.')
            digest.update(chunk)
    return size,digest.hexdigest()


def send_large(graph, sender, draft_mime, files, remember_draft=lambda value: None):
    validate_size(files)
    base = graph.GRAPH_ROOT + '/users/' + urllib.parse.quote(sender, safe='')
    draft_id = None
    try:
        token = graph.access_token()
        result = graph._request('POST', base + '/messages', token=token,
                                body=base64.b64encode(draft_mime), headers={'Content-Type': 'text/plain'})
        draft_id = result.get('id')
        if not draft_id or not result.get('isDraft'):
            raise ValueError('Microsoft did not confirm an unsent draft.')
        remember_draft(draft_id)
        url = base + '/messages/' + urllib.parse.quote(draft_id, safe='')
        for f in files:
            if f['size'] < 3 * 1024 * 1024:
                graph._request('POST', url + '/attachments', token=token, headers={'Content-Type':'application/json'},body=json.dumps({
                    '@odata.type': '#microsoft.graph.fileAttachment', 'name': f['name'],
                    'contentType': content_type(f['name']), 'contentBytes': base64.b64encode(f['data']).decode('ascii')}).encode())
            else:
                session = graph._request('POST', url + '/attachments/createUploadSession', token=token,
                                         headers={'Content-Type':'application/json'},body=json.dumps({'AttachmentItem': {'attachmentType': 'file', 'name': f['name'],
                                                                  'size': f['size'], 'contentType': content_type(f['name'])}}).encode())
                if session.get('nextExpectedRanges') != ['0-']:
                    raise ValueError('Microsoft did not start an attachment session.')
                for start in range(0, f['size'], CHUNK):
                    put_chunk(session['uploadUrl'], f['data'][start:start + CHUNK], start, f['size'])
        # Confirm all files before allowing the single send attempt.
        check = graph._request('GET', url + '?$select=isDraft,toRecipients,ccRecipients,bccRecipients,subject,internetMessageHeaders', token=token)
        if not check.get('isDraft'):
            raise ValueError('The billing draft is no longer unsent.')
        reviewed=BytesParser(policy=policy.default).parsebytes(draft_mime)
        for header,field in [('To','toRecipients'),('Cc','ccRecipients'),('Bcc','bccRecipients')]:
            wanted=Counter(address.lower() for _,address in getaddresses(reviewed.get_all(header,[])))
            present=Counter(item.get('emailAddress',{}).get('address','').lower() for item in check.get(field,[]))
            if wanted!=present:raise ValueError('The draft recipients differ from the reviewed package.')
        if str(reviewed['Subject'])!=check.get('subject'):raise ValueError('The draft subject differs from the reviewed package.')
        headers={h['name'].lower():h['value'] for h in check.get('internetMessageHeaders',[])}
        if str(reviewed.get('In-Reply-To',''))!=headers.get('in-reply-to',''):raise ValueError('Microsoft did not retain the reviewed original email thread.')
        attachments = graph._request('GET', url + '/attachments?$select=id,name,size,isInline', token=token)
        attached=[f for f in attachments.get('value',[]) if not f.get('isInline')]
        if Counter(f.get('name') for f in attached)!=Counter(f['name'] for f in files) or attachments.get('@odata.nextLink'):
            raise ValueError('The draft attachments do not match the reviewed package.')
        actual=Counter()
        for f in attached:
            size,digest=attachment_digest(url+'/attachments/'+urllib.parse.quote(f['id'],safe='')+'/$value',token)
            actual[(f['name'],size,digest)]+=1
        expected=Counter((f['name'],f['size'],hashlib.sha256(f['data']).hexdigest()) for f in files)
        if actual!=expected:raise ValueError('The uploaded file contents do not match the reviewed package.')
    except Exception as ex:
        if draft_id:
            try:
                graph._request('DELETE', base + '/messages/' + urllib.parse.quote(draft_id, safe=''), token=graph.access_token())
            except Exception:
                pass  # An unsent orphan draft cannot create a duplicate send.
        raise BeforeSendFailure('Attachment preparation failed before sending. No billing email was sent. Reopen and prepare the package again.') from ex
    # Never retry this call, and never classify a failure here as a pre-send error.
    graph._request('POST', url + '/send', token=graph.access_token())
