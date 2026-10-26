"""Fictional original-chain fixtures; no email transport is used."""
import unittest
from email.message import EmailMessage
from unittest.mock import patch
import urllib.parse
import field_thread as field
import mail_automation as mail


def message(mid, sender, to, cc=(), subject='RE: [EXTERNAL] Test outage', auto='no', at='2026-10-20T12:00:00Z', cid='verified'):
    def people(items):
        return [{'emailAddress': {'address': a}} for a in items]
    return {'subject': subject, 'from': people([sender])[0],
            'toRecipients': people(to), 'ccRecipients': people(cc),
            'internetMessageId': '<'+mid+'@example.com>', 'conversationId': cid,
            'receivedDateTime': at,
            'internetMessageHeaders': [{'name': 'Auto-Submitted', 'value': auto}]}


class Store:
    def list_row_attachments(self, *args):
        return [{'id': 1, 'name': 'assignment.eml', 'createdAt': '2026-10-21'}]
    def download_attachment(self, *args):
        source = EmailMessage()
        source['From'] = 'office@firstdigitalsc.com'
        source['To'] = mail.SENDER
        source['Subject'] = 'FW: Test outage'
        source['Message-ID'] = '<source@example.com>'
        source.set_content('From: body-only@example.net\nCc: never-route@example.net')
        return source.as_bytes(), 'assignment.eml', 'message/rfc822'


class FieldThreadTests(unittest.TestCase):
    def setUp(self):
        self.event = {'sheetId': '1', 'rowId': '2'}
        self.base = mail.validate_route(mail.from_graph(message('forward', 'dispatcher@example.com',
            ['office@firstdigitalsc.com'], subject='FW: [EXTERNAL] Test outage')))
        self.rows = [
            message('original', 'customer@example.com', ['coordinator@example.com', 'team@example.com'], ['office@firstdigitalsc.com']),
            message('forward', 'dispatcher@example.com', ['office@firstdigitalsc.com'], subject='FW: [EXTERNAL] Test outage'),
            message('latest', 'coordinator@example.com', ['office@firstdigitalsc.com'], at='2026-10-20T14:00:00Z'),
            message('automated', mail.SENDER, ['unrelated@example.com'], auto='auto-generated'),
            message('different', 'unrelated@example.com', ['wrong@example.com'], subject='RE: Different job')]

    def resolve(self, rows=None):
        with patch.object(field, 'exact_record', return_value={'conversationId': 'verified'}), \
             patch.object(field, 'conversation_records', return_value=self.rows if rows is None else rows):
            return field.resolve(Store(), 'fake', self.event, self.base)

    def test_all_verified_human_participants_and_original_reply_anchor(self):
        route = self.resolve()
        self.assertEqual(set(route['to'] + route['cc']), {
            'customer@example.com', 'coordinator@example.com', 'team@example.com',
            'office@firstdigitalsc.com', 'dispatcher@example.com'})
        self.assertEqual(route['messageId'], '<latest@example.com>')
        self.assertFalse(field.forwarded(route['subject']))
        self.assertEqual(len(route['to']+route['cc']), len(set(route['to']+route['cc'])))

    def test_direct_original_retains_existing_billing_compatible_route(self):
        base = {**self.base, 'subject': 'RE: Test outage'}
        with patch.object(field, 'exact_record', side_effect=AssertionError('unexpected lookup')):
            self.assertIs(field.resolve(Store(), 'fake', self.event, base), base)

    def test_missing_verified_original_holds(self):
        with self.assertRaises(mail.ThreadReview):
            self.resolve([self.rows[1]])

    def test_unverified_source_holds(self):
        with patch.object(field, 'exact_record', return_value=None):
            with self.assertRaises(mail.ThreadReview):
                field.resolve(Store(), 'fake', self.event, self.base)

    def test_conversation_is_exact_and_never_silently_truncated(self):
        for response in ({'value': [], '@odata.nextLink': 'unchecked'},
                         {'value': [message('wrong', 'x@example.com', [], cid='other')]}):
            with patch.object(mail.graph, '_request', return_value=response):
                with self.assertRaises(mail.ThreadReview):
                    field.conversation_records('fake', 'office@firstdigitalsc.com', 'verified')

    def test_exact_source_ambiguity_holds(self):
        with patch.object(mail.graph, '_request', return_value={'value': [{}, {}]}):
            with self.assertRaises(mail.ThreadReview):
                field.exact_record('fake', 'office@firstdigitalsc.com', '<source@example.com>')

    def test_queries_filter_by_identifiers_only(self):
        with patch.object(mail.graph, '_request', return_value={'value': []}) as request:
            field.conversation_records('fake', 'office@firstdigitalsc.com', 'verified')
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[1]).query)
            self.assertEqual(query['$filter'], ["conversationId eq 'verified'"])
            self.assertNotIn('$search', query)


if __name__ == '__main__':
    unittest.main()
