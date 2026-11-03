import unittest
from unittest.mock import patch
import operations_intake as intake


def sheet(records):
    names = list(dict.fromkeys(k for record in records for k in record if k != 'id'))
    return {'columns': [{'id': i, 'title': n} for i, n in enumerate(names)],
            'rows': [{'id': r.get('id', index), 'cells': [
                {'columnId': i, 'value': r.get(n, '')} for i, n in enumerate(names)]}
                for index, r in enumerate(records)]}


class Store:
    def __init__(self):
        self.ledger = [{'Subject': 'FW: [EXTERNAL] Cutover Willow Trace',
                        'State': 'Imported', 'Detail': 'Reviewed in portal by office.',
                        'Job Key': 'florence:42'}]
        self.jobs = [{'id': 42, 'Task Name': '1408 Willow Trace', 'City': 'Florence',
                      'Portal Job Type': 'CRQ', 'Portal CRQ ID': 'CRQ206596',
                      'Status': 'On site', 'Assigned Technician': 'Existing technician'}]

    def list_sheets(self):
        return [{'name': intake.LEDGER, 'id': 999}]

    def get_sheet(self, sid, **kwargs):
        return sheet(self.ledger if sid == 999 else self.jobs if sid == intake.AREA['Florence'] else [])


class ReviewedCutoverTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.message = {'subject': 'Re: FW: [EXTERNAL] Cutover Willow Trace',
                        'conversationId': 'new-outlook-calendar-chain',
                        'from': {'emailAddress': {'address': 'coordinator@spectrum.com'}},
                        'body': {'content': 'Open and close CRQ ticket with ROC.'}}

    def test_new_chain_inherits_exact_office_confirmed_job(self):
        with patch.object(intake.graph, '_candidate_document_texts', return_value=[]):
            result = intake.message_item(self.store, self.message, [])
        self.assertEqual((result['address'], result['market'], result['type']),
                         ('1408 Willow Trace', 'Florence', 'CRQ'))
        self.assertNotIn('planned_date', result)

    def test_forwarded_calendar_location_matches_title_without_optional_drive(self):
        self.message['body']['content'] = 'Where: 1408 Willow Trace Dr (1408 Willow Trace Dr, Florence, South Carolina 29501)'
        self.assertEqual(intake.reviewed_cutover_match(self.store, self.message)[2]['id'], 42)

    def test_different_location_or_city_cannot_inherit_subject(self):
        for location in ['21408 Willow Trace Dr, Florence, SC 29501',
                         '1408 Willow Trace Dr, Columbia, SC 29201']:
            self.message['body']['content'] = 'Where: ' + location
            with self.assertRaisesRegex(ValueError, 'location conflicts'):
                intake.reviewed_cutover_match(self.store, self.message)

    def test_different_crq_is_not_same_event(self):
        self.message['body']['content'] = 'CRQ206597'
        with self.assertRaisesRegex(ValueError, 'CRQ conflicts'):
            intake.reviewed_cutover_match(self.store, self.message)

    def test_multiple_office_confirmed_jobs_stay_in_review(self):
        self.store.ledger.append(dict(self.store.ledger[0], **{'Job Key': 'columbia:43'}))
        with self.assertRaisesRegex(ValueError, 'multiple jobs'):
            intake.reviewed_cutover_match(self.store, self.message)

    def test_unreviewed_changed_generic_and_untrusted_mail_cannot_learn_a_match(self):
        self.store.ledger[0]['Detail'] = 'Automatically imported.'
        self.assertIsNone(intake.reviewed_cutover_match(self.store, self.message))
        self.store.ledger[0]['Detail'] = 'Reviewed in portal by office.'
        for subject in ['Cutover', 'Update', 'Cutover Willow Trace phase two']:
            self.message['subject'] = subject
            self.assertIsNone(intake.reviewed_cutover_match(self.store, self.message))
        self.message['subject'] = 'Cutover Willow Trace'
        self.message['from']['emailAddress']['address'] = 'stranger@example.invalid'
        self.assertIsNone(intake.reviewed_cutover_match(self.store, self.message))

    def test_mixed_customer_scope_is_still_rejected(self):
        self.message['body']['content'] = 'Kinetic assignment'
        with self.assertRaisesRegex(ValueError, 'mixed Spectrum scope'):
            intake.message_item(self.store, self.message, [])


if __name__ == '__main__':
    unittest.main()
