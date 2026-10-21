"""Attach the portal's verified, field-safe completion photographs."""
import base64
import hashlib
import re
import json
import urllib.error


def attach(draft, event, store, bridge, review):
    if event.get('status') != 'Field complete':
        return draft
    try:
        result = bridge(store, 'POST', '/fdgc/v1/completion-photos', {'jobKey': event['jobKey']})
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        try:
            message = json.loads(exc.read(4096)).get('message', '')
        except (ValueError, AttributeError):
            message = ''
        raise review(str(message)[:400] or 'Completion photos need office review. No photos were omitted.')
    if result.get('jobKey') != event['jobKey'] or not isinstance(result.get('photos'), list):
        raise review('Completion photos could not be verified. Office review required.')
    photos = result['photos']
    if len(photos) > 20:
        raise review('Completion photo package needs office review; no photos were omitted.')
    attachments = []
    total = 0
    seen = set()
    for photo in photos:
        try:
            raw = base64.b64decode(photo['content'], validate=True)
        except (ValueError, KeyError, TypeError):
            raise review('A completion photo is unavailable. Office review required.')
        digest = hashlib.sha256(raw).hexdigest()
        if not raw.startswith(b'\xff\xd8') or digest != photo.get('sha256') or digest in seen:
            raise review('Completion photo verification failed. Office review required.')
        name = photo.get('name', '')
        if not re.fullmatch(r'Field-photo-[0-9]{1,2}\.jpg', name):
            raise review('Unexpected completion photo filename.')
        seen.add(digest)
        total += len(raw)
        if total > 1572864:
            raise review('Completion photo package is too large; no photos were omitted.')
        attachments.append({'name': name, 'content': photo['content'], 'mime': 'image/jpeg'})
    # Completion collects all saved field pictures, including outage pictures.
    # Avoid sending the same newly submitted outage photo a second time inline.
    output = {**draft, 'attachments': attachments}
    if attachments:
        output['text'] += '\n\n'+str(len(attachments))+' field photo(s) attached.'
        output['html'] = output['html'].replace('<hr>', '<p>'+str(len(attachments))+' field photo(s) attached.</p><hr>', 1)
    return output
