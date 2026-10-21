"""Evidence-based recognition of forwarded Spectrum dispatch formats."""
import base64, email, email.policy, hashlib, re, urllib.parse
from datetime import datetime, timezone
import graph_import as graph

PUSH = re.compile(r'\bpush[ -]+light\b',re.I)
NODE = re.compile(r'\b\d[A-Z]\d{1,3}E\b',re.I)
STREET = r"\d{1,6}\s+(?:US\s+\d+|[A-Za-z0-9 .#'\-]+?\s(?:RD|ROAD|ST|STREET|AVE|AVENUE|DR|DRIVE|CT|COURT|LN|LANE|HWY|HIGHWAY|BLVD|BOULEVARD|CIR|CIRCLE|WAY|PKWY|PARKWAY))(?:\s+(?:EXT|EXTENSION))?(?:\s+(?:N|S|E|W|NE|NW|SE|SW))?"
EXTRA_CITIES = {'mcbee':'Florence','cheraw':'Florence','lynchburg':'Florence','aiken':'Columbia','jackson':'Columbia','barnwell':'Columbia','orangeburg':'Columbia','blair':'Columbia','summerton':'Columbia','edgefield':'Columbia','johnston':'Columbia'}

def newer_source(incoming, previous):
    if not previous:return True
    try:
        def parse(value):
            stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        return parse(incoming)>=parse(previous)
    except ValueError:return False

def crq_id(subject):
    m=re.search(r'\bCRQ\s*[-#:]?\s*(\d{6,15})\b',subject,re.I)
    return 'CRQ'+m.group(1) if m else ''

def appointment(body):
    # Only the first appointment header is current. Quoted older windows cannot win.
    where=re.search(r'(?im)^\s*Where:\s*([^\r\n]+)',body)
    when=re.search(r'(?im)^\s*When:\s*([^\r\n]+)',body)
    out={}
    if where:
        site=re.search(r'\b('+STREET+r')\s*[, (]+(?:\1\s*,\s*)?([A-Za-z ]{2,35})\s*,\s*(?:SC|South Carolina)\b',where.group(1),re.I)
        # Outlook often repeats the street in parentheses before the city.
        if not site:
            site=re.search(r'\(('+STREET+r')\s*,\s*([A-Za-z ]{2,35})\s*,\s*(?:SC|South Carolina)\b',where.group(1),re.I)
        if site:out.update(address=site.group(1).strip(),city=site.group(2).strip().title())
    if when:
        value=when.group(1)
        numeric=re.search(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',value)
        words=re.search(r'\b[A-Za-z]+\s+\d{1,2},?\s+20\d{2}\b',value)
        for candidate in [numeric.group() if numeric else '',words.group() if words else '']:
            for fmt in ['%m/%d/%Y','%m/%d/%y','%B %d, %Y','%B %d %Y','%b %d, %Y','%b %d %Y']:
                try:out['planned_date']=datetime.strptime(candidate,fmt).date().isoformat();break
                except ValueError:pass
            if out.get('planned_date'):break
    return out

def trusted_no_light(message, body):
    subject=str(message.get('subject',''))
    if not re.search(r'\b(?:no|low)[ -]+light\b',subject,re.I) or not NODE.search(subject):return False
    if not re.search(r'\b'+STREET+r'[,\s]+[^\r\n]{0,40}\bSC\b',subject,re.I):return False
    sender=message.get('from',{}).get('emailAddress',{}).get('address','').lower()
    if sender.endswith('@graycliffent.com'):return True
    return sender.endswith('@firstdigitalsc.com') and bool(re.search(r'(?im)^\s*From:\s*[^\r\n]*@graycliffent\.com\b',body))

def resolved_incident(body):
    # A completed construction survey is not a restored outage.
    text=re.sub(r'\b(?:construction\s+)?survey\s+completed\s*[:?]?\s*(?:yes|no)?','',body,flags=re.I)
    return bool(re.search(r'\b(?:resolved|restored|completed)\b',text,re.I))

def enrich_push_parent(token,message,attachments):
    """Recover a missing NTP only from the exact direct parent in the forwarding user's mailbox."""
    if not PUSH.search(str(message.get('subject',''))):return attachments
    if any(re.search(r'PRISM\s+ID\s*:',t,re.I) and re.search(r'Work\s+Order\s*#',t,re.I) for _,t in graph._candidate_document_texts(attachments)):return attachments
    sender=message.get('from',{}).get('emailAddress',{}).get('address','').lower()
    if not sender.endswith('@firstdigitalsc.com'):return attachments
    raw=graph.get_message_mime(token,message['id']);source=email.message_from_bytes(raw,policy=email.policy.default)
    mids=re.findall(r'<[^<>\r\n]+>',str(source.get('In-Reply-To','')))
    if len(mids)!=1:return attachments
    root=graph.GRAPH_ROOT+'/users/'+urllib.parse.quote(sender,safe='')
    query=urllib.parse.urlencode({'$filter':"internetMessageId eq '"+mids[0].replace("'","''")+"'",'$top':'2','$select':'id,subject,internetMessageId,body'})
    rows=graph._request('GET',root+'/messages?'+query,token=token).get('value',[])
    if len(rows)!=1:return attachments
    parent=rows[0]
    if parent.get('internetMessageId')!=mids[0] or not PUSH.search(parent.get('subject','')):return attachments
    street=lambda s: re.search(r'\b('+STREET+r')',s,re.I)
    a,b=street(message.get('subject','')),street(parent.get('subject',''))
    if not a or not b or re.sub(r'\W','',a.group(1)).lower()!=re.sub(r'\W','',b.group(1)).lower():raise ValueError('Push Light parent location conflicts with the intake reply.')
    subject_nodes=NODE.findall(message.get('subject',''));parent_nodes=NODE.findall(parent.get('subject',''))
    if subject_nodes and parent_nodes and {n.upper() for n in subject_nodes}!={n.upper() for n in parent_nodes}:raise ValueError('Push Light parent node conflicts with the intake reply.')
    url=root+'/messages/'+urllib.parse.quote(parent['id'],safe='');files=[];page=url+'/attachments?$top=100'
    while page:
        if not page.startswith(url+'/attachments'):raise ValueError('Unexpected parent attachment page.')
        response=graph._request('GET',page,token=token);files+=response.get('value',[]);page=response.get('@odata.nextLink','')
    docs=graph._candidate_document_texts(files)
    if not any(re.search(r'PRISM\s+ID\s*:',t,re.I) and re.search(r'Work\s+Order\s*#',t,re.I) for _,t in docs):return attachments
    parent_raw=graph._request('GET',url+'/$value',token=token)
    if not isinstance(parent_raw,bytes) or not parent_raw:raise ValueError('Original parent email could not be retained.')
    return attachments+files+[{'@odata.type':'#microsoft.graph.fileAttachment','name':'Source-assignment-'+hashlib.sha256(parent_raw).hexdigest()[:20]+'.eml','contentType':'message/rfc822','contentBytes':base64.b64encode(parent_raw).decode()}]
