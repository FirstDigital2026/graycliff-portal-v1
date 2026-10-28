"""Dedicated mailbox intake into the existing operational area sheets.

Uses existing Render credentials; never exports them to WordPress or the browser.
Only the original graycliff Render service consumes the mailbox.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import threading
import sqlite3
import urllib.request
import email
import email.policy
import mimetypes
import io
import zipfile
import base64
from datetime import date, datetime, timedelta, timezone
import graph_import as graph
import intake_formats as formats

AREA = {'Florence': 2515846899388292, 'Columbia': 8067039099834244}
SEARCH_SHEETS = {**AREA, 'History':3889686678294404}
LEDGER = 'Graycliff Email Intake'
GUARD = threading.Lock()
EXTRA = ['Portal Project ID', 'Portal Work Order', 'Portal NTP Due Date',
         'Portal Intake Key', 'Portal Job Type', 'Portal Incident ID',
         'Portal Claimed By', 'Assigned Technician', 'Portal Assigned At',
         'Portal Email Threads', 'Portal Email Updated At','Portal Field Document Review','Portal CRQ ID']

def now():
    return datetime.now(timezone.utc).isoformat()

def internal_due(original):
    return (date.fromisoformat(original) - timedelta(days=1)).isoformat() if original else ''

def vals(sheet, row):
    columns = {str(c['id']): c['title'] for c in sheet['columns']}
    values = {columns.get(str(c['columnId']), ''): c.get('value', c.get('displayValue', '')) for c in row.get('cells', [])}
    # TEXT_NUMBER cells may return an integral ID as 7021433.0.
    # Normalize only integral forms; genuinely different IDs still conflict.
    for name in ('Portal Project ID', 'Portal Work Order'):
        value = str(values.get(name) or '').strip()
        values[name] = re.sub(r'^(\d+)\.0+$', r'\1', value)
    return values

def write(store, sid, values, rid=None):
    # Mutation timeouts are ambiguous. Never blindly retry a row creation.
    sheet = store.get_sheet(sid, force=True)
    cols = {c['title']: c['id'] for c in sheet['columns']}
    cells = [{'columnId': cols[k], 'value': v, 'strict': False} for k,v in values.items() if k in cols]
    row = {'id': rid, 'cells': cells} if rid else {'toBottom': True, 'cells': cells}
    result = store._request('PUT' if rid else 'POST', f'/sheets/{sid}/rows', body=[row], retries=1)
    store.invalidate(f'sheet:{sid}')
    return result['result'][0]

def ensure_columns(store, sid, names):
    sheet = store.get_sheet(sid, force=True)
    existing = {c['title'] for c in sheet['columns']}
    add = [{'title': n, 'type': 'TEXT_NUMBER', 'index': len(existing)} for n in names if n not in existing]
    if add:
        store._request('POST', f'/sheets/{sid}/columns', body=add, retries=1)
        store.invalidate(f'sheet:{sid}')
    return store.get_sheet(sid, force=True)

def ledger_id(store):
    sheets = [s for s in store.list_sheets() if s['name'] == LEDGER]
    if len(sheets) > 1:
        raise RuntimeError('Duplicate intake sheets require office review.')
    if sheets:
        return sheets[0]['id']
    columns = [{'title': 'Message Key', 'type': 'TEXT_NUMBER', 'primary': True}]
    columns += [{'title': n, 'type': 'TEXT_NUMBER'} for n in ['Subject','Received At','State','Detail','Job Key','Last Checked']]
    return store._request('POST','/sheets', body={'name': LEDGER, 'columns': columns}, retries=1)['result']['id']

def audit(store, sid, key, **values):
    sheet = store.get_sheet(sid, force=True)
    found = [r for r in sheet.get('rows',[]) if str(vals(sheet,r).get('Message Key','')) == key]
    return write(store,sid,{'Message Key':key,'Last Checked':now(),**values},found[0]['id'] if found else None)

def category_for_subject(subject):
    # Planned CRQ/night maintenance stays distinct even when its purpose is
    # repairing low light. A night-time emergency alone is still an outage.
    if re.search(r'\bCRQ\s*[-#:]?\s*\d*\b|\b(?:night(?:time)?\s+(?:work|maintenance)|maintenance\s+window)\b',subject,re.I):
        return 'CRQ'
    if re.search(r'\b(?:no[ -]+light|low[ -]+light|down(?:ed)?[ -]+fiber|outage|request map location|emergency|fiber cut|fiber event|fiber engagement|node\s+(?:100%\s+)?down|ECO[ _-]+OUT)\b',subject,re.I):
        return 'Outage'
    return 'Standard'


def spectrum_scope(message, attachments):
    """A Graycliff sender alone does not establish the customer's work scope."""
    text = str(message.get('subject', ''))+'\n'+graph._plain_text((message.get('body') or {}).get('content', ''))
    text += '\n'+'\n'.join(content for _,content in graph._candidate_document_texts(attachments))
    if re.search(r'\b(?:Lumos|Kinetic|SEGRA|SEFH\s*\d+|Lewisville)\b', text, re.I):
        raise ValueError('Outside or mixed Spectrum scope (Lumos/Kinetic/SEGRA). Office review required; no operational job changed.')
    return bool(re.search(r'\bSpectrum\b|@(?:[a-z0-9.-]+\.)?charter\.com\b', text, re.I)) or formats.trusted_no_light(message,graph._plain_text((message.get('body') or {}).get('content','')))


def category_for_message(subject, body):
    category = category_for_subject(subject)
    if category != 'Standard':
        return category
    # A bare node is not itself authorization or an incident identifier. Only
    # promote it when the dispatch body establishes emergency restoration work.
    node = re.sub(r'^(?:(?:RE|FW|FWD):\s*|\[EXTERNAL\]\s*)+', '', subject.strip(), flags=re.I).strip()
    if not (re.fullmatch(r'(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,8}', node, re.I) or (re.match(r'^NODE\s*:',node,re.I) and formats.NODE.search(node))):
        return category
    if category_for_subject(body) == 'CRQ':
        return 'CRQ'
    if re.search(r'\b(?:no|not an?|without)\s+(?:outage|emergency)\b', body, re.I):
        return category
    incident = re.search(r'\b(?:outage|no[ -]+light|low[ -]+light|node\s+(?:100%\s+)?down|fiber\s+cut)\b', body, re.I)
    dispatch = re.search(r'\b(?:please\s+(?:run|roll|dispatch)|roll\s+(?:a\s+)?splicer|ASAP|en\s*route|sent\s+to\s+Graycliff\s+to\s+fix|engag(?:e|ed|ing))\b', body, re.I)
    return 'Outage' if incident and dispatch else category


def parse(message, attachments):
    subject = str(message.get('subject',''))
    body = graph._plain_text((message.get('body') or {}).get('content',''))
    ntp = graph.parse_recognized_ntp(message)
    pid = re.search(r'\b(?:PID|PRISM(?:\s+ID)?|PNG|P(?=\s*#))\s*[:#-]?\s*(\d{6,12})(?!\d)', subject,re.I)
    if not pid:
        pid = re.search(r'^(?:(?:RE|FW|FWD):\s*|\[EXTERNAL\]\s*)*["\s]*(\d{7,12})n?\s+\d+\s',subject,re.I)
    category = category_for_message(subject, body)
    crq = category == 'CRQ'
    outage = category == 'Outage'
    if not ntp and not pid and not crq and not outage and not formats.PUSH.search(subject):
        raise ValueError('Email format needs review: no reliable PID, NTP, CRQ or outage subject.')
    parsed = ntp or graph.ParsedNtp('',pid.group(1) if pid else '',subject)
    # A coordinator's signature address is never a job location.
    if ntp:
        parsed.address = ''
        parsed.city = ''
    document_ids={m.group(1) for _,text in graph._candidate_document_texts(attachments) if re.search(r'Work\s+Order\s*#',text,re.I) and (m:=re.search(r'PRISM\s+ID\s*:\s*(\d+)',text,re.I))}
    if len(document_ids)>1 and not pid:raise ValueError('Multiple NTP jobs are attached. Office review required before importing.')
    parsed = graph.enrich_ntp_from_attachments(parsed,attachments)
    for filename,text in graph._candidate_document_texts(attachments):
        document_pid = re.search(r'PRISM\s+ID\s*:\s*(\d+)',text,re.I)
        if not document_pid or (pid and document_pid.group(1)!=pid.group(1)):
            continue
        work_order = re.search(r'Work\s+Order\s*#?\s*:\s*(\d+)',text,re.I)
        city = re.search(r'\b([A-Za-z]+(?:[ \t]+[A-Za-z]+)?),[ \t]*SC[ \t]+\d{5}\b',text,re.I)
        if work_order:
            parsed.work_order = work_order.group(1)
        if city:
            parsed.city = city.group(1).strip().title()
        lines=text.splitlines()
        for index,line in enumerate(lines):
            if 'Job Address' in line and 'Site Contact' in line:
                left,right=line.index('Job Address'),line.index('Site Contact')
                candidate=next((v for v in lines[index+1:] if v.strip()),'')[left:right].strip()
                if re.match(r'^\d+\s+',candidate):
                    parsed.address=candidate
    if pid and parsed.prism and pid.group(1) != parsed.prism:
        raise ValueError('Subject PID conflicts with the attached NTP. Review the documents.')
    if not parsed.prism and pid:
        parsed.prism = pid.group(1)
    # Address in the subject is safer than a coordinator's address in a signature.
    ntp_location=bool(parsed.address and parsed.city and parsed.prism and parsed.work_order)
    address = re.search(r'\b('+formats.STREET+r')(?![A-Za-z])',subject,re.I)
    if address and not parsed.address:
        parsed.address = address.group(1).strip()
    appointment = formats.appointment(body) if crq else {}
    if appointment.get('address') and not parsed.address:
        parsed.address=appointment['address'];parsed.city=appointment['city']
    if outage:
        # Incident emails frequently have no PID and put the site only in the body.
        # Require a full location with city/state; exclude known signature offices.
        pattern=r'\b(\d{1,6}\s+[A-Za-z0-9 .#\'-]{2,65}?\s(?:RD|ROAD|ST|STREET|AVE|AVENUE|DR|DRIVE|CT|COURT|LN|LANE|HWY|HIGHWAY))\.?\s*(?:NE|NW|SE|SW)?[, ]+([A-Za-z ]{2,30})[, ]+SC\b'
        # A single explicitly identified MST service site outranks affected homes.
        mst_sites={}
        for mst in re.finditer(r'\bMST\s+(?:feeding|(?:service\s+)?address\s*[:=-]|(?:located\s+)?at)\s*'+pattern,body,re.I):
            street,city=mst.group(1).strip(),mst.group(2).strip()
            mst_sites[normalized_address(street)]=(street,city)
        destination=formats.dispatch_site(body)
        if destination:
            parsed.address,parsed.city=destination
        elif len(mst_sites)==1:
            parsed.address,parsed.city=next(iter(mst_sites.values()))
        elif len(mst_sites)>1:
            raise ValueError('Multiple explicit MST service sites. Office review is required.')
        sites={}
        for found in re.finditer(pattern,body,re.I):
            street,city=found.group(1).strip(),found.group(2).strip()
            compact=re.sub(r'[^a-z0-9]','',street.lower())
            if compact in {'170pontiaccenterdr','3998claussenroad','3235scashuadr'}:
                continue
            if parsed.address and re.sub(r'[^a-z0-9]','',parsed.address.lower())!=compact:
                continue
            sites[compact]=(street,city)
        if len(sites)==1:
            parsed.address,parsed.city=next(iter(sites.values()))
        elif len(sites)>1:
            raise ValueError('Incident email contains multiple possible sites. Office review is required.')
        if parsed.city.lower()=='orangeburge':
            parsed.city='Orangeburg'
    parsed.city=re.sub(r'^(?:Road|Rd|Lane|Ln|Blvd|Boulevard|Dr|Drive|St|Street|Ct|Court|Ave|Avenue)\s+', '', parsed.city, flags=re.I)
    location_text = subject + ' ' + parsed.city + ' ' + parsed.address
    # A city in the job subject outranks column-layout artifacts in a PDF.
    subject_cities = [c for c in graph.FLORENCE_CITIES | graph.COLUMBIA_CITIES | ({'cheraw','sumter','lynchburg'} | set(formats.EXTRA_CITIES)) if re.search(r'\b'+re.escape(c)+r'\b',subject,re.I)]
    if len(subject_cities)==1 and not ntp_location:
        parsed.city=subject_cities[0].title()
    if not parsed.city:
        cities = graph.FLORENCE_CITIES | graph.COLUMBIA_CITIES | ({'cheraw','sumter','orangeburg','barnwell','blair','lynchburg'} | set(formats.EXTRA_CITIES))
        matches = [c for c in cities if re.search(r'\b'+re.escape(c)+r'\b',location_text,re.I)]
        if matches:
            parsed.city = max(matches,key=len).title()
    parsed.market = graph._market_for(parsed.city,parsed.address)
    if parsed.city.lower() in {'cheraw','lynchburg'}:
        parsed.market = 'Florence'
    if parsed.city.lower() in {'orangeburg','barnwell','blair','summerton','edgefield'}:
        parsed.market = 'Columbia'
    if parsed.city.lower() in formats.EXTRA_CITIES:parsed.market=formats.EXTRA_CITIES[parsed.city.lower()]
    # For unmapped towns, an explicit Graycliff dispatch list is area evidence.
    # Keep known-city routing authoritative; ambiguous lists never select an area.
    if not parsed.market and parsed.address and parsed.city:
        routing = str((message.get('body') or {}).get('content',''))+' '+json.dumps({k:message.get(k,[]) for k in ('toRecipients','ccRecipients')})
        areas={a.title() for a in re.findall(r'(?<![a-z0-9_.+-])ntp-(columbia|florence)@graycliffent\.com(?![a-z0-9_.-])',routing,re.I)}
        if len(areas)==1:
            parsed.market=areas.pop()
    # Do not use dates in a quoted chain as a current NTP deadline.
    return {'pid':parsed.prism,'wo':parsed.work_order,'address':parsed.address,
            'city':parsed.city,'market':parsed.market,'ntp_due':parsed.due_date,
            'type':category,
            'subject':subject,'body':body,'crq_id':formats.crq_id(subject),'incident_id':formats.incident_id(subject),'planned_date':appointment.get('planned_date','')}

def normalized_address(text):
    text = str(text).lower()
    for long,short in [('road','rd'),('street','st'),('avenue','ave'),('drive','dr'),('court','ct'),('lane','ln'),('highway','hwy')]:
        text = re.sub(r'\b'+long+r'\b',short,text)
    return re.sub(r'[^a-z0-9]','',text)

def locate(store, item, linked=None):
    found = []
    for market,sid in SEARCH_SHEETS.items():
        sheet = store.get_sheet(sid,force=True)
        for row in sheet.get('rows',[]):
            v = vals(sheet,row)
            title = str(v.get('Task Name',''))
            incoming_incident=item.get('incident_id','')
            existing_incident=formats.incident_id(v.get('Portal Incident ID','')) or formats.incident_id(title) or formats.incident_id(str(v.get('Notes','')).split('\n')[0])
            # A hub/address can have many outages. Different tickets are different work.
            if incoming_incident and existing_incident and incoming_incident!=existing_incident:
                continue
            match_incident=bool(incoming_incident and incoming_incident==existing_incident)
            match_pid = item['pid'] and (str(v.get('Portal Project ID','')) == item['pid'] or re.search(r'(?<!\d)'+re.escape(item['pid'])+r'(?!\d)',title))
            match_wo = item['wo'] and str(v.get('Portal Work Order','')) == item['wo']
            crqid=item.get('crq_id','');match_crq=bool(crqid and crqid in {str(v.get('Portal CRQ ID','')),formats.crq_id(title),formats.crq_id(str(v.get('Notes','')))})
            if match_crq and item.get('pid') and v.get('Portal Project ID') and item['pid']!=v['Portal Project ID']:raise ValueError('CRQ identifier conflicts with the supplied PID.')
            normalize = lambda text: re.sub(r'[^a-z0-9]','',str(text).lower())
            address = normalized_address(item['address'])
            # Legacy rows often have a CRQ in the title but no PID column.
            # Accept only the full extracted address on one unique row; never fuzzy-match.
            canonical_title = title.lower()
            for long,short in [('road','rd'),('street','st'),('avenue','ave'),('drive','dr'),('court','ct'),('lane','ln'),('highway','hwy')]:
                canonical_title=re.sub(r'\b'+long+r'\b',short,canonical_title)
            address_pattern=r'(?<![a-z0-9])'+r'[\W_]*'.join(map(re.escape,address))+r'(?![a-z0-9])'
            address_exists = len(address)>7 and re.search(address_pattern,canonical_title)
            city_matches = not item['city'] or not v.get('City') or normalize(v['City']).startswith(normalize(item['city']))
            if address_exists and not city_matches and not match_pid and not match_wo:
                raise ValueError('This street address already exists with a different city. Review before creating a job.')
            match_address = address_exists and city_matches
            existing_crq=str(v.get('Portal CRQ ID') or formats.crq_id(title) or formats.crq_id(str(v.get('Notes',''))))
            if match_address and crqid and existing_crq and crqid!=existing_crq:raise ValueError('This location already has a different CRQ. Review the maintenance event before linking.')
            existing_type = str(v.get('Portal Job Type') or ('CRQ' if re.search(r'\bCRQ',title+' '+str(v.get('Notes','')),re.I) else 'Standard'))
            same_thread=bool(linked and linked[1]==sid and linked[2]['id']==row['id'])
            if not same_thread and match_address and (item.get('type')=='Project')!=(existing_type=='Project') and not match_pid and not match_wo:
                raise ValueError('A project at this location is separate from an existing non-project job. Office review required.')
            if not same_thread and match_address and item.get('type')=='Outage' and existing_type!='Outage' and not match_pid and not match_wo:
                raise ValueError('An outage at this address may be a separate event from the existing regular/CRQ job. Office review required before attaching or creating.')
            if match_address and item['pid'] and v.get('Portal Project ID') and item['pid']!=v['Portal Project ID']:
                raise ValueError('This address already has a different project ID. Office review required.')
            if match_pid or match_wo or match_address or match_crq or match_incident:
                found.append((market,sid,row,v))
    if len(found)>1:
        raise ValueError('Multiple existing jobs match this identifier. Review before changing a job.')
    return found[0] if found else None

def thread_match(store, message):
    thread = str(message.get('conversationId') or '')
    if not thread:
        return None
    matches = []
    for market,sid in SEARCH_SHEETS.items():
        sheet = store.get_sheet(sid,force=True)
        for row in sheet.get('rows',[]):
            old = vals(sheet,row)
            try:
                threads = json.loads(str(old.get('Portal Email Threads') or '[]'))
            except (ValueError,TypeError):
                threads = []
            if thread in threads:
                matches.append((market,sid,row,old))
    if len(matches)>1:
        raise ValueError('This email chain is linked to multiple jobs. Office review required.')
    return matches[0] if matches else None

def message_item(store, message, attachments):
    confirmed_scope = spectrum_scope(message, attachments)
    linked = thread_match(store,message)
    try:
        item = parse(message,attachments)
    except ValueError as exc:
        if str(exc).startswith(('Incident email contains multiple possible sites.', 'Multiple explicit MST service sites.')):
            subject=str(message.get('subject',''))
            incident=formats.incident_id(subject)
            exact=locate(store,{'pid':'','wo':'','address':'','city':'','incident_id':incident}) if incident else None
            if exact:
                old=exact[3]
                return {'pid':str(old.get('Portal Project ID','')),'wo':str(old.get('Portal Work Order','')),
                        'address':str(old.get('Task Name','')),'city':str(old.get('City','')),
                        'market':exact[0],'ntp_due':'','type':str(old.get('Portal Job Type') or 'Outage'),
                        'subject':subject,'body':graph._plain_text((message.get('body') or {}).get('content','')),
                        'incident_id':incident}
        if not str(exc).startswith('Email format needs review:'):
            raise
        if not linked:
            subject=str(message.get('subject',''))
            address=re.search(r'\b(\d{1,6}\s+(?:US\s+\d+|[A-Za-z0-9 .#\'-]+?\s(?:RD|ROAD|ST|STREET|AVE|AVENUE|DR|DRIVE|CT|COURT|LN|LANE|HWY|HIGHWAY)))(?![A-Za-z])',subject,re.I)
            if address:
                linked=locate(store,{'pid':'','wo':'','address':address.group(1),'city':''})
            if not linked:
                raise
        # Only an established chain may supply context for an otherwise unrecognized reply.
        subject = str(message.get('subject',''))
        if re.search(r'\b(?:PID|PRISM|CRQ)\s*[:#]?\s*\d+',subject,re.I):
            raise ValueError('Reply identifiers could not be verified. Office review required.')
        old=linked[3]
        item={'pid':str(old.get('Portal Project ID','')),'wo':str(old.get('Portal Work Order','')),
              'address':str(old.get('Task Name','')),'city':str(old.get('City','')),
              'market':linked[0],'ntp_due':'','type':str(old.get('Portal Job Type') or 'Standard'),
              'subject':subject,'body':graph._plain_text((message.get('body') or {}).get('content',''))}
    # Replies may inherit the scope of one exact existing operational job.
    # New jobs require affirmative Spectrum evidence, not just a PID or city.
    if not confirmed_scope and not linked and not locate(store,item):
        raise ValueError('Spectrum work scope is not established. Office review required before creating a job.')
    return item

def save_message(store, token, message, item, attachments, key, state, db=None):
    state.pop("acknowledgment",None)
    linked = thread_match(store,message)
    found = locate(store,item,linked)
    if linked:
        if found and (found[1],found[2]['id'])!=(linked[1],linked[2]['id']):
            raise ValueError('Reply identifiers conflict with the job linked to this chain. Office review required.')
        old=linked[3]
        old_incident=formats.incident_id(old.get('Portal Incident ID','')) or formats.incident_id(old.get('Task Name','')) or formats.incident_id(str(old.get('Notes','')).split('\n')[0])
        if item.get('incident_id') and old_incident and item['incident_id']!=old_incident:
            raise ValueError('This email chain contains a different incident ticket. Office review required before linking separate outages.')
        if item['pid'] and old.get('Portal Project ID') and item['pid']!=str(old['Portal Project ID']):
            raise ValueError('Reply PID differs from this chain’s job. Office review required.')
        if item['wo'] and old.get('Portal Work Order') and item['wo']!=str(old['Portal Work Order']):
            raise ValueError('Reply work order differs from this chain’s job. Office review required.')
        found=linked
    if found:
        market,sid,row,old = found
    else:
        market = item['market']
        if market not in AREA or not item['address']:
            raise ValueError('Area or job location is missing. Office review required before releasing to techs.')
        if item['type']=='Outage' and formats.resolved_incident(item['body']):
            raise ValueError('Incident thread includes a resolution. Review before opening another active outage.')
        sid = AREA[market]
        row,old = None,{}
    ensure_columns(store,sid,EXTRA)
    values = {'Portal Project ID':item['pid'] or old.get('Portal Project ID',''),
              'Portal Work Order':item['wo'] or old.get('Portal Work Order','')}
    # Existing CRQ classification and scheduled windows always take precedence.
    jobtype = str(old.get('Portal Job Type','')) or ('CRQ' if re.search('CRQ',str(old.get('Task Name',''))+' '+str(old.get('Notes',''))+' '+str(old.get('Status','')),re.I) else ('Standard' if row else item['type']))
    values['Portal Job Type'] = jobtype
    if jobtype=='Outage' and item.get('incident_id'):
        values['Portal Incident ID']=item['incident_id']
    if item.get('crq_id'):values['Portal CRQ ID']=item['crq_id']
    incoming=message.get('receivedDateTime') or now()
    newer=formats.newer_source(incoming,old.get('Portal Email Updated At'))
    closed = market=='History' or bool(old and (str(old.get('Portal Billing Status','')) == 'Submitted' or re.search(r'complete|billed',str(old.get('Status','')),re.I)))
    if item['ntp_due'] and not closed and newer:
        values['Portal NTP Due Date'] = item['ntp_due']
        if jobtype in {'Standard','Project'}:
            values['Due Date'] = internal_due(item['ntp_due'])
    if jobtype=='CRQ' and item.get('planned_date') and not closed and newer and not old.get('Portal Planning Audit') and not old.get('Portal Office Due Date'):
        values.update({'Portal Planned Date':item['planned_date'],'Due Date':item['planned_date'],'Portal Work Shift':'Night'})
        ensure_columns(store,sid,['Portal Planned Date','Portal Work Shift'])
    thread=str(message.get('conversationId') or '')
    try:
        threads=json.loads(str(old.get('Portal Email Threads') or '[]'))
    except (ValueError,TypeError):
        threads=[]
    if thread and thread not in threads:
        threads.append(thread)
    values['Portal Email Threads']=json.dumps(threads)
    values['Portal Email Updated At']=incoming if newer else old['Portal Email Updated At']
    if not row:
        # Include the stable intake key in the creation itself for ambiguous failures.
        sheet = store.get_sheet(sid,force=True)
        prior = [r for r in sheet.get('rows',[]) if str(vals(sheet,r).get('Portal Intake Key','')) == key]
        if prior:
            row = prior[0]
        else:
            incident = (item.get('incident_id') or 'EV-'+hashlib.sha256(key.encode()).hexdigest()[:12].upper()) if jobtype=='Outage' else ''
            planning_note='\nNTP completion date not supplied. Confirm scheduling date before planning work.' if jobtype=='Standard' and not item['ntp_due'] else ''
            values.update({'Task Name':(item['pid']+' · ' if item['pid'] else '')+item['address'],
                           'City':item['city'],'Portal Intake Key':key,'Portal Incident ID':incident,
                           'Notes':'Email intake: '+item['subject']+planning_note+'\n'+item['body'][:3000]})
            if jobtype=='Outage':
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%dT%H:%M')
                roster = [s for s in store.list_sheets() if s['name']=='Graycliff On Call Rotation']
                if len(roster)==1:
                    schedule=store.get_sheet(roster[0]['id'],force=True)
                    active=[vals(schedule,r) for r in schedule.get('rows',[]) if vals(schedule,r).get('Area')==market and str(vals(schedule,r).get('Start Date',''))<=today<str(vals(schedule,r).get('End Date','')) and vals(schedule,r).get('Email') and vals(schedule,r).get('Not On Call')!='Yes']
                    if len(active)==1:
                        values.update({'Portal Claimed By':active[0].get('Email',''),'Assigned Technician':active[0].get('Technician',''),'Portal Assigned At':now(),'Status':'Assigned'})
            row = write(store,sid,values)
            state['created'] += 1
            if db is not None:
                import intake_receipt
                intake_receipt.reserve(db,market.lower()+':'+str(row['id']),key)
    else:
        write(store,sid,values,row['id'])
        state['updated'] += 1
    capture_email_files(store,token,message,sid,row['id'],attachments,key)
    if jobtype=='Project' and market!='History' and not closed:
        try:
            from mail_automation import bridge
            bridge(store,'POST','/fdgc/v1/project-provision',{'jobKey':market.lower()+':'+str(row['id'])})
        except Exception:
            ensure_columns(store,sid,['Portal Project Setup Review'])
            write(store,sid,{'Portal Project Setup Review':'Project sheet/form setup needs office review; open Office project setup to retry.'},row['id'])
    if market!='History':
        # Nonfinancial details and safe field documents are separate from the
        # retained original NTP/email. A document problem must not suppress dispatch.
        try:
            from field_documents import sync_row
            sync_row(store,sid,row['id'])
            write(store,sid,{'Portal Field Document Review':''},row['id'])
        except Exception:
            write(store,sid,{'Portal Field Document Review':'Field-document extraction needs office review. Original source files remain office-only.'},row['id'])
    # The operational sheets intentionally use comments to mark work complete.
    # Preserve that existing workflow: an incoming email is source material,
    # not a field completion comment. Its full body and images are in the .eml
    # and extracted attachments above, with its receipt in the intake ledger.
    if db is not None:
        import intake_receipt
        state['acknowledgment']=intake_receipt.finish(store,db,token,market.lower()+':'+str(row['id']),key)
    return market.lower()+':'+str(row['id'])

# Verified signature logos, company banners and social icons. Do not infer from
# generic imageNNN names, inline/CID disposition, dimensions or image file size.
# The untouched original email remains attached as the source record.
SIGNATURE_IMAGE_HASHES = {'0990d3f2302c522ed27042d372c114b73ce1f020c47cb4ac8a666e62f96d01d8', '165b6b300b32d80821729c32d3e14d831299ab1f573e61bde6ae6c7a8565acdf', '5ed189834a6c314bfd535d0a4549c2be4b0af40f5fa68203398434b3ddaa2630', '9976e00c16c669dc59a30d2687d6c0349c47e251150d13372583276b172cfcbe', '07191215e8e21acfa604ca800c479bff88ba2a53c5e8c5b3f8bb22b11e975490', 'd71b446a7542d7ee4134ef1296fc119ab9bccf021f9f777d9e4a7f6ca424f707', '9e577e39b49b66457ecbdc2bbec1479b3d6267fd55bda21337aa7027251fab60'}
SIGNATURE_IMAGE_HASHES.update({'cb0628092ddea96bb040221b5c793dbbb792a67d0621bdfba170c07374d85801', '104b981f00ae0305dcb847fa096033e175fe5e71bb9e4175d68a4f414164db42', '104b91fbfbc89b3c41c3246a653763164bdbdf01be58a40155ad994ece4c33e6'})
SIGNATURE_IMAGE_HASHES.update({'9de6407e9e1a46d414c47ecfcd30dc080f128afd44b8e1536ff63da9f381e695', '07191215e8e21acfa604ca800c479bff88ba2a53c5e8c5b3f8bb22b11e975490', '165b6b300b32d80821729c32d3e14d831299ab1f573e61bde6ae6c7a8565acdf', '09e120d0e3ef5cb87380de508ee94057c84dab603b12f2ea9ce0113c850b2535', '93d0457996d09d1dfd97a58f2d3b83606d588f603a6bccf7340288db0c560082', '104b981f00ae0305dcb847fa096033e175fe5e71bb9e4175d68a4f414164db42', '7e1dafd77b4781e9b8a24b0f179fda2ecb2fe3d0f04d9f90dc69b30727afc304', 'bc1143b8e99c4ab60418a2b056e8173bf180b9c5c0bd78ca502782ec5843b165', 'b4dd8313537e9ede03b2437e194bd604e406bbc3e5675c19530a87ad3325b55d', '6504968627c2a31ed1dc4515fb152918ed7c1634c633d485030c95f775ba83c5', '05cb96bf592a3767733489cb628887132f09e66ec67864566adc49c1794ede64', '1c4726a167eaa4e674abaa77c70f94d98221ce9da40ef975593030fb3ab63987', '4654c4a586b709add3be98118f87f213a63fe4830174c954f615669855e7954e', '91806d7e612bb2919b284c13b46b8e46163143af73661b45bf64df977d3c5749', '5ed189834a6c314bfd535d0a4549c2be4b0af40f5fa68203398434b3ddaa2630', '4007fbf56f3c5dd7bd6a086689f7aa60121a9dfc7dee06657d1d12516838c4d2', 'a39168612c1e962a26a145818f5bd87f1e950ac6f2bb768bd1ced6cdbaa93e14', '7e601c52e4f3e99d6d0bfae42f514b903f62b0bb4c6dfcfd55173e0ef1c6bb80', '3299571f903d5869410c038198971c4759cb3054b1abb501b785f16ce940ffb6', '8fe6f6fda9007a6b14a09de2ddfab6e884d1477251c779d00396f59301b9970a', 'e645c3cc4ca42a02d1eaf01e70978a75f654a219ae120b3d3bf298ab3c9a464a', 'd5449e6687366b8e7e7ba3c8c486569e85fc17f2b3fa3c7ae569826209d51437', '9976e00c16c669dc59a30d2687d6c0349c47e251150d13372583276b172cfcbe', 'c23845d3abe592c86d27f5e584656d3f9afb019f4b0cfd83cb4aa7d7165afbf2', '0990d3f2302c522ed27042d372c114b73ce1f020c47cb4ac8a666e62f96d01d8', '9e577e39b49b66457ecbdc2bbec1479b3d6267fd55bda21337aa7027251fab60', 'a35d4871381ef83a109c2904f035fddd0b1a4b99cf42d4b145ed191e562b4354', '85a7330c9cf1b16a02bcdf9c77b95a9535b312d968d198fd8824f48b3c7668ea', 'd71b446a7542d7ee4134ef1296fc119ab9bccf021f9f777d9e4a7f6ca424f707', 'cb0628092ddea96bb040221b5c793dbbb792a67d0621bdfba170c07374d85801', '69aab014b436ca57e34b9df90db5817d97faed5819e36b1dcb81cf13b3281fd6', '104b91fbfbc89b3c41c3246a653763164bdbdf01be58a40155ad994ece4c33e6', '563e6ba1b13e4eee04719b926578da433809f9e3eed21ad7f8b4d0c111295a94'})
SIGNATURE_ASSET_NAME = re.compile(r'^(?:newgcelogotransparent|058_sm_(?:fb|in)|(?:\d{2}-\d{2})?unitekemailbanner|goldbanner|emailsig\d+(?:-\d+)?(?:updated)?|graycliff-logo-esign\d+|unitek-graphic-esign\d+)_[0-9a-f-]{36}(?:-[0-9a-f]{10})?\.(?:png|jpe?g|gif|webp)$', re.I)

def signature_graphic(name, mime, data):
    if not str(mime).lower().startswith('image/'):
        return False
    return (hashlib.sha256(data).hexdigest() in SIGNATURE_IMAGE_HASHES
            or bool(SIGNATURE_ASSET_NAME.fullmatch(os.path.basename(name))))


def capture_email_files(store,token,message,sid,row_id,attachments,key):
    names = {a['name'] for a in store.list_row_attachments(sid,row_id)}
    raw = graph.get_message_mime(token,message['id'])
    if not raw:
        raise RuntimeError('Original email copy unavailable; intake requires review.')
    eml_name = 'Email-'+key[:20]+'.eml'
    if eml_name not in names:
        store.attach_file_to_row(sid,row_id,filename=eml_name,mime_type='message/rfc822',data=raw)
    parts = source_attachments(raw)
    decoded_files = [d for a in attachments if (d:=graph.attachment_bytes(a))] + parts
    # Match content before saving either Graph or MIME copies. Outlook often
    # renames a signature image to image001.png in one of those copies.
    signature_hashes={hashlib.sha256(data).hexdigest() for name,mime,data in decoded_files if signature_graphic(name,mime,data)}
    saved_hashes={m.group(1) for n in names if (m:=re.search(r'-([a-f0-9]{10})(?:\.[^.]+)?$',n))}
    for name,mime,data in decoded_files:
        if signature_graphic(name,mime,data) or hashlib.sha256(data).hexdigest() in signature_hashes:
            continue
        # Hash content, so revised files with the same filename are retained.
        digest=hashlib.sha256(data).hexdigest()[:10]
        if digest in saved_hashes:
            continue
        stem,ext = os.path.splitext(name)
        name = stem[:110]+'-'+digest+ext
        if name not in names:
            zipped_name=name+'.zip'
            if zipped_name not in names:
                try:
                    store.attach_file_to_row(sid,row_id,filename=name,mime_type=mime,data=data)
                except Exception as exc:
                    # Only the explicit unsupported-file response permits ZIP fallback.
                    if not re.search(r'"errorCode"\s*:\s*5021\b',str(exc)):
                        raise
                    archive=io.BytesIO()
                    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                        z.writestr(os.path.basename(name.replace('\\','/')),data)
                    store.attach_file_to_row(sid,row_id,filename=zipped_name,mime_type='application/zip',data=archive.getvalue())
                    names.add(zipped_name)
            names.add(name)
        saved_hashes.add(digest)
    for attachment in attachments:
        kind=attachment.get('@odata.type','')
        if kind=='#microsoft.graph.referenceAttachment':
            raise ValueError('Cloud-linked attachment requires office review; original email retained. Download and attach the referenced file.')
        if kind=='#microsoft.graph.fileAttachment' and not graph.attachment_bytes(attachment) and not any(n==attachment.get('name') for n,_,_ in parts):
            raise ValueError('An attachment could not be extracted; original email retained. Office review required.')

def source_attachments(raw):
    """Keep regular files, inline/CID images, attached emails and data-URI pictures."""
    source=email.message_from_bytes(raw,policy=email.policy.default)
    files=[]
    for part in source.walk():
        mime=part.get_content_type();name=part.get_filename()
        if mime=='message/rfc822':
            payload=part.get_payload()
            if isinstance(payload,list) and payload:
                files.append((name or 'attached-message.eml',mime,payload[0].as_bytes(policy=email.policy.default)))
            continue
        if part.is_multipart():
            continue
        data=part.get_payload(decode=True)
        if not data:
            continue
        if name or part.get_content_disposition() in {'attachment','inline'} or part.get('Content-ID'):
            files.append((name or 'inline-picture'+(mimetypes.guess_extension(mime) or '.bin'),mime,data))
        if mime=='text/html':
            html=data.decode(part.get_content_charset() or 'utf-8',errors='replace')
            for image_mime,encoded in re.findall(r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)',html):
                try:
                    picture=base64.b64decode(re.sub(r'\s','',encoded),validate=True)
                except ValueError:
                    continue
                if picture:
                    files.append(('embedded-picture'+(mimetypes.guess_extension(image_mime) or '.bin'),image_mime,picture))
    return files

def seed_imported_threads(store, token, folder, db):
    # Link previously imported messages once, using the existing recorded job key.
    # Never infer a new job from historical mail during this migration.
    with db() as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS operations_mail (message_key TEXT PRIMARY KEY, job_key TEXT, state TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS operations_meta (key TEXT PRIMARY KEY, value TEXT)')
        if connection.execute("SELECT value FROM operations_meta WHERE key='thread_seed_v2'").fetchone():
            return
        recorded={r['message_key']:r['job_key'] for r in connection.execute("SELECT * FROM operations_mail WHERE state='Imported'").fetchall()}
    if recorded:
        import urllib.parse
        user=urllib.parse.quote(graph.mailbox(),safe='')
        fid=urllib.parse.quote(folder,safe='')
        result=graph._request('GET',f'{graph.GRAPH_ROOT}/users/{user}/mailFolders/{fid}/messages?$top=100&$orderby=receivedDateTime%20desc&$select=id,internetMessageId,conversationId',token=token)
        for message in result.get('value',[]):
            key=hashlib.sha256(str(message.get('internetMessageId') or message['id']).encode()).hexdigest()
            jobkey=recorded.get(key,'');thread=message.get('conversationId')
            if not jobkey or not thread:
                continue
            area,rid=jobkey.split(':',1);market=area.title();sid=SEARCH_SHEETS.get(market)
            if not sid:
                continue
            sheet=ensure_columns(store,sid,EXTRA)
            rows=[r for r in sheet.get('rows',[]) if str(r['id'])==rid]
            if len(rows)!=1:
                continue
            old=vals(sheet,rows[0]);threads=json.loads(str(old.get('Portal Email Threads') or '[]'))
            if thread not in threads:
                write(store,sid,{'Portal Email Threads':json.dumps(threads+[thread])},rows[0]['id'])
            try:
                capture_email_files(store,token,message,sid,rows[0]['id'],[],key)
            except Exception as exc:
                audit(store,ledger_id(store),key,State='Needs review',Detail='Attachment backfill needs office review ('+type(exc).__name__+'). Original email remains in Outlook.',**{'Job Key':jobkey})
    with db() as connection:
        connection.execute("INSERT OR REPLACE INTO operations_meta VALUES ('thread_seed_v2','done')")

def notify_import_assignment(store,jobkey,message_key):
    # Only the original email creating this job may notify its automatic assignee.
    # The signed portal endpoint records a durable per-assignment receipt before
    # sending, so retries cannot repeat a phone alert after an uncertain response.
    area,rid=jobkey.split(':',1)
    market={'florence':'Florence','columbia':'Columbia'}.get(area)
    if not market:return None
    sheet=store.get_sheet(AREA[market],force=True)
    rows=[r for r in sheet.get('rows',[]) if str(r['id'])==rid]
    if len(rows)!=1:return None
    v=vals(sheet,rows[0])
    if str(v.get('Portal Intake Key',''))!=message_key or not v.get('Portal Claimed By'):return None
    try:
        from mail_automation import bridge
        return bridge(store,'POST','/fdgc/v1/assignment-alert',{'jobKey':jobkey})
    except Exception:
        return {'state':'NEEDS_ATTENTION','detail':'Assignment saved; phone notification could not be confirmed. Check phone delivery in System checks and contact the technician.'}

def retained_import(store,key,jobkey):
    """A durable receipt only clears a hold when its exact source is still saved."""
    if not jobkey or ':' not in jobkey:return False
    market,rid=jobkey.split(':',1)
    sid=SEARCH_SHEETS.get(market.title())
    if not sid:return False
    sheet=store.get_sheet(sid,force=True)
    matches=[row for row in sheet.get('rows',[]) if str(row['id'])==rid]
    if len(matches)!=1:return False
    name='Email-'+key[:20]+'.eml'
    return any(a.get('name')==name for a in store.list_row_attachments(sid,matches[0]['id']))

def run(store, db):
    # Three historical services use this repository. Only one may consume Inbox.
    if os.getenv('RENDER_SERVICE_ID','') != 'srv-d9p0jflbedkc73ds5tn0':
        return {'ok':True,'configured':False,'message':'Mailbox processing is owned by the original graycliff service.'}
    if not GUARD.acquire(blocking=False):
        return {'ok':True,'message':'Intake already running.'}
    sid = None
    try:
        sid = ledger_id(store)
        token = graph.access_token()
        imported = graph.ensure_mail_folder(token,'Imported')
        review = graph.ensure_mail_folder(token,'Manual Review')
        failed = graph.ensure_mail_folder(token,'Import Failed')
        seed_imported_threads(store,token,imported,db)
        stats = {'created':0,'updated':0,'failed':0,'ignored':0,'replies':0}
        messages = graph.list_recent_messages(token,top=40)
        # Retry recognition holds after parser improvements. Never discard the
        # held source, and never replay an uncertain portal save automatically.
        ledger=store.get_sheet(sid,force=True)
        holds={str(v.get('Message Key')):v for row in ledger.get('rows',[]) if (v:=vals(ledger,row)).get('State')=='Needs review' and str(v.get('Detail','')).startswith(('Incident email contains multiple possible sites.', 'NTP completion date is missing.'))}
        retry_keys=set()
        if holds:
            for held in graph.list_folder_messages(token,review,top=40):
                held_key=hashlib.sha256(str(held.get('internetMessageId') or held['id']).encode()).hexdigest()
                if held_key not in holds:continue
                with db() as connection:
                    connection.execute('CREATE TABLE IF NOT EXISTS portal_intake_review (message_key TEXT PRIMARY KEY, actor TEXT, started REAL, job_key TEXT)')
                    if connection.execute('SELECT 1 FROM portal_intake_review WHERE message_key=?',(held_key,)).fetchone():continue
                messages.append(graph.get_message_details(token,held['id']))
                retry_keys.add(held_key)
        for message in reversed(messages):
            key = hashlib.sha256(str(message.get('internetMessageId') or message['id']).encode()).hexdigest()
            with db() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS operations_mail (message_key TEXT PRIMARY KEY, job_key TEXT, state TEXT)')
                prior = connection.execute('SELECT * FROM operations_mail WHERE message_key=?',(key,)).fetchone()
            phase='recording receipt'
            private_commercial=False
            try:
                if prior and prior['state']=='Imported' and retained_import(store,key,prior['job_key']):
                    audit(store,sid,key,State='Imported',Detail='Existing job and retained original email verified; duplicate review entry reconciled.',**{'Job Key':prior['job_key']})
                    graph.move_message(token,message['id'],imported)
                    continue
                phase='reading documents'
                # Graph's hasAttachments flag is false for messages containing only inline pictures.
                attachments = graph.list_attachments(token,message['id'],inline_content=False)
                attachments = formats.enrich_push_parent(token,message,attachments)
                from kinetic_finance import candidate as is_commercial, ingest as ingest_commercial
                if is_commercial(message,attachments):
                    private_commercial=True
                    phase='saving private commercial evidence'
                    result=ingest_commercial(db,message,attachments,key,token)
                    stats['commercial']=stats.get('commercial',0)+1
                    # Commercial sources never become operational job files or
                    # shared intake attachments. Failed/review copies stay put.
                    continue
                audit(store,sid,key,Subject=message.get('subject',''),**{'Received At':message.get('receivedDateTime',''),'State':'Received','Detail':'Processing email and documents.'})
                from billing_recaps import candidate as is_recap, ingest as ingest_recap
                if is_recap(message,attachments):
                    phase='reconciling billing recap'
                    recap=ingest_recap(store,db,message,attachments,key,token,sid)
                    audit(store,sid,key,State=recap['state'],Detail=recap['detail'])
                    graph.move_message(token,message['id'],review if recap['state']=='Needs review' else imported)
                    stats['recaps']=stats.get('recaps',0)+1
                    continue
                item = message_item(store,message,attachments)
                phase='saving job and source files'
                if key in retry_keys:
                    # Serialize with a portal reviewer and leave the claim in
                    # place if a network failure makes the save uncertain.
                    with db() as connection:
                        try:
                            connection.execute('INSERT INTO portal_intake_review VALUES (?,?,?,?)',(key,'Automatic intake reconciliation',datetime.now(timezone.utc).timestamp(),''))
                        except sqlite3.IntegrityError:
                            continue
                # Recovery retains source files and status without sending a
                # late receipt acknowledgment to the original customer thread.
                jobkey = save_message(store,token,message,item,attachments,key,stats,db=None if key in retry_keys else db)
                phase='confirming import'
                phone=notify_import_assignment(store,jobkey,key)
                detail='Job and original email saved; existing field status preserved.'
                if stats.get('acknowledgment'):detail+=' Receipt acknowledgment: '+stats['acknowledgment']+'.'
                if phone:detail+=' Phone alert: '+phone.get('detail','Delivery needs attention.')
                audit(store,sid,key,State='Imported',Detail=detail,**{'Job Key':jobkey})
                with db() as connection:
                    connection.execute('INSERT OR REPLACE INTO operations_mail VALUES (?,?,?)',(key,jobkey,'Imported'))
                    if key in retry_keys:
                        connection.execute('UPDATE portal_intake_review SET job_key=? WHERE message_key=?',(jobkey,key))
                graph.move_message(token,message['id'],imported)
            except ValueError as exc:
                if private_commercial:
                    stats['failed']+=1
                    continue
                audit(store,sid,key,State='Needs review',Detail=str(exc))
                graph.move_message(token,message['id'],review)
                stats['ignored'] += 1
            except Exception as exc:
                if private_commercial:
                    stats['failed']+=1
                    continue
                # Do not record provider payloads, credentials or private HTTP data.
                diagnostic=type(exc).__name__
                if diagnostic=='SmartsheetError':
                    diagnostic+=': '+str(exc)[:700]
                audit(store,sid,key,Subject=message.get('subject',''),State='Import failed',Detail=f'Failed while {phase}. {diagnostic}. Check any partially saved job before retrying.',**{'Received At':message.get('receivedDateTime','')})
                graph.move_message(token,message['id'],failed)
                stats['failed'] += 1
        # Refresh comparisons after new sends, even on an otherwise empty inbox.
        from billing_recaps import sync_sheet as sync_recap_sheet
        sync_recap_sheet(store,db)
        audit(store,sid,'SYSTEM',Subject='Graycliff mailbox connection',State='Connected',Detail='Inbox checked. '+json.dumps(stats))
        return {'ok':True,'configured':True,**stats,'message':'Mailbox intake checked the operational area sheets.'}
    except Exception:
        if sid:
            try:
                audit(store,sid,'SYSTEM',Subject='Graycliff mailbox connection',State='Connection error',Detail='Automatic intake requires attention. Emails remain in the mailbox for review.')
            except Exception:
                pass
        raise
    finally:
        # Outage reminders must keep running even when mailbox intake fails.
        try:
            request=urllib.request.Request('https://firstdigitalsc.com/wp-cron.php',headers={'User-Agent':'Mozilla/5.0 FirstDigitalGraycliff/0.2'})
            urllib.request.urlopen(request,timeout=15).close()
        except Exception:
            pass
        GUARD.release()
