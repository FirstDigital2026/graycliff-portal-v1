"""Expand forwarded field updates through an exact, verified mail conversation.

Billing deliberately continues using mail_automation.resolve_thread and its
separate area recipient policy. Quoted bodies and Bcc are never routing input.
"""
from __future__ import annotations

import re
import urllib.parse
import mail_automation as mail

FIELDS = ('subject,from,toRecipients,ccRecipients,replyTo,internetMessageId,'
          'internetMessageHeaders,conversationId,receivedDateTime')


def subject_key(subject):
    value = str(subject or '').strip()
    while True:
        clean = re.sub(r'^\s*(?:(?:re|fw|fwd)\s*:|\[external\])\s*', '', value, flags=re.I)
        if clean == value:
            return re.sub(r'\s+', ' ', value).casefold()
        value = clean


def forwarded(subject):
    value = re.sub(r'^\s*(?:re\s*:\s*)*', '', str(subject or ''), flags=re.I)
    return bool(re.match(r'\s*(?:fw|fwd)\s*:', value, re.I))


def exact_record(token, mailbox, message_id):
    if not re.fullmatch(r'<[^<>\s\r\n]+>', message_id):
        raise mail.ThreadReview('The source email has no valid Message-ID.')
    query = urllib.parse.urlencode({
        '$filter': "internetMessageId eq '" + message_id.replace("'", "''") + "'",
        '$select': FIELDS, '$top': '2'})
    root = mail.graph.GRAPH_ROOT + '/users/' + urllib.parse.quote(mailbox, safe='') + '/messages'
    rows = mail.graph._request('GET', root + '?' + query, token=token).get('value', [])
    if len(rows) > 1:
        raise mail.ThreadReview('The exact source message matched more than once. Review its thread.')
    return rows[0] if rows else None


def conversation_records(token, mailbox, conversation_id):
    query = urllib.parse.urlencode({
        '$filter': "conversationId eq '" + conversation_id.replace("'", "''") + "'",
        '$select': FIELDS, '$top': '100'})
    root = mail.graph.GRAPH_ROOT + '/users/' + urllib.parse.quote(mailbox, safe='') + '/messages'
    rows = mail.graph._request('GET', root + '?' + query, token=token)
    # Never send to a silently truncated participant list or an unchecked URL.
    if rows.get('@odata.nextLink'):
        raise mail.ThreadReview('The original conversation needs recipient review because it exceeds 100 messages.')
    values = rows.get('value', [])
    if any(row.get('conversationId') != conversation_id for row in values):
        raise mail.ThreadReview('An email outside the verified conversation was returned.')
    return values


def resolve(store, token, event, base):
    if not forwarded(base.get('subject', '')):
        return base
    sid, rid = int(event['sheetId']), int(event['rowId'])
    files = [a for a in store.list_row_attachments(sid, rid)
             if str(a.get('name', '')).lower().endswith('.eml')]
    files.sort(key=lambda a: (a.get('createdAt', ''), str(a.get('id', ''))), reverse=True)
    if not files:
        raise mail.ThreadReview('No source email is attached for field reply-all verification.')
    raw, _, _ = store.download_attachment(sid, files[0]['id'])
    source = mail.from_mime(raw)
    people = source['from'] + source['to'] + source['cc'] + base['from'] + base['to'] + base['cc']
    boxes = list(dict.fromkeys([a for a in people if a.endswith('@firstdigitalsc.com')] + [mail.SENDER]))[:8]
    identity = None
    for box in boxes:
        # A mailbox can lack a forwarded copy; no body or broad subject search substitutes for it.
        try:
            record = exact_record(token, box, source['messageId'])
        except mail.graph.GraphImportError:
            continue
        if record and record.get('conversationId'):
            identity = box, record['conversationId']
            break
    if identity is None:
        raise mail.ThreadReview('The forwarded field-update conversation could not be verified. No recipients guessed.')
    target_subject = subject_key(base['subject'])
    candidates = []
    for message in conversation_records(token, *identity):
        route = mail.from_graph(message)
        if subject_key(route['subject']) != target_subject:
            continue
        if mail.SENDER in route['from'] or route.get('autoSubmitted', 'no') not in {'', 'no'}:
            continue
        if not mail.external(route):
            continue
        valid = mail.validate_route(route)
        candidates.append((message.get('receivedDateTime', ''), valid))
    originals = [(at, route) for at, route in candidates if not forwarded(route['subject'])
                 and any(not address.endswith('@firstdigitalsc.com') for address in route['from'])]
    if not originals:
        # Assignments are often forwarded by Graycliff. Their actual incoming
        # Graph message is the received customer chain, even with an FW subject.
        # Keep exact conversation/subject verification and header-only routing.
        originals = [(at, route) for at, route in candidates
                     if any(address.endswith(('@graycliffent.com', '@spectrum.com', '@charter.com'))
                            for address in route['from'])]
    if not originals:
        raise mail.ThreadReview('The verified conversation has no original customer-chain message. Review recipients.')
    # Keep the original reply chain, while retaining all human participants from
    # that exact conversation, including colleagues who forwarded the assignment.
    anchor = max(originals, key=lambda item: item[0])[1]
    routes = [anchor, base] + [route for _, route in candidates]
    to = list(dict.fromkeys(address for route in routes for address in route['to']))
    cc = list(dict.fromkeys(address for route in routes for address in route['cc'] if address not in to))
    return mail.validate_route({**anchor, 'to': to, 'cc': cc})
