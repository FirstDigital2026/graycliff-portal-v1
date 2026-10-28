"""Reconcile billing to remittance advice; never infer bank settlement."""
import re
from datetime import date, timedelta
from statistics import median


def reference_week(reference, invoices):
    """A dated reference must agree with a billing week; amount alone cannot override it."""
    match = re.search(r'(\d{2})(\d{2})(\d{2})?$', reference.strip())
    if not match:
        return None
    month, day, year = match.groups()
    return [i for i in invoices if i['week'][5:] == month+'-'+day
            and (not year or i['week'][2:4] == year)]


def reconcile(invoices, receipts, today=None):
    today = today or date.today()
    rows = [{**i, 'remittances': [], 'matchBasis': []} for i in invoices]
    unmatched = []
    for receipt in receipts:
        candidates = [r for r in rows if r['projectCode'] == receipt['projectCode']]
        dated = reference_week(receipt.get('invoiceRef', ''), candidates)
        basis = 'Invoice reference and project'
        if dated is not None:
            candidates = dated
        else:
            exact = [r for r in candidates if receipt.get('invoiceRef') in
                     [r.get('invoiceRef'), r.get('invoiceNumber')]
                     and receipt.get('invoiceRef')]
            if exact:
                candidates = exact
            else:
                candidates = [r for r in candidates if r['amountCents'] == receipt['amountCents']]
                basis = 'Unique amount and project; reference has no billing date'
        if len(candidates) != 1:
            unmatched.append({**receipt, 'reason': 'No unique billing match; check invoice reference and amount'})
            continue
        candidates[0]['remittances'].append(receipt)
        candidates[0]['matchBasis'].append(basis)

    samples = []
    for row in rows:
        remits = row['remittances']
        reported = sum(r['amountCents'] for r in remits)
        if reported == row['amountCents'] and len(remits) == 1 and remits[0].get('noticeDate'):
            lag = (date.fromisoformat(remits[0]['noticeDate'])-date.fromisoformat(row['week'])).days
            if lag >= 0:
                samples.append((row['week'], lag))
    lags = [lag for _, lag in sorted(samples)[-12:]]
    model = {'sampleCount': len(lags), 'minimumDays': min(lags) if len(lags) >= 3 else None,
             'medianDays': int(median(lags)) if len(lags) >= 3 else None,
             'maximumDays': max(lags) if len(lags) >= 3 else None,
             'basis': 'Observed email notice dates after billing week ending; estimate, not contractual terms'}
    for row in rows:
        reported = sum(r['amountCents'] for r in row['remittances'])
        row.update(reportedCents=reported, differenceCents=row['amountCents']-reported,
                   expectedStart=None, expectedDate=None, expectedEnd=None, overdueDays=0)
        if model['medianDays'] is not None:
            week = date.fromisoformat(row['week'])
            for key, days in [('expectedStart', model['minimumDays']), ('expectedDate', model['medianDays']), ('expectedEnd', model['maximumDays'])]:
                row[key] = (week+timedelta(days=days)).isoformat()
            row['overdueDays'] = max(0, (today-date.fromisoformat(row['expectedEnd'])).days)
        if row['remittances']:
            if row['differenceCents'] == 0:
                row['status'] = 'Remittance matches billing'
            elif row['differenceCents'] > 0:
                row['status'] = 'Short remittance — balance missing'
            else:
                row['status'] = 'Remittance exceeds billing'
        elif row['overdueDays']:
            row['status'] = 'Overdue — no remittance received'
        else:
            row['status'] = 'Awaiting remittance' if row['expectedEnd'] else 'Awaiting remittance — timing not established'
        row['flagged'] = bool(row['differenceCents'] and (row['remittances'] or row['overdueDays']))
    return {'asOf': today.isoformat(), 'model': model, 'weeks': sorted(rows, key=lambda r:r['week'], reverse=True),
            'unmatchedRemittances': unmatched, 'flagCount': sum(r['flagged'] for r in rows)+len(unmatched),
            'missingRemittanceCents': sum(r['differenceCents'] for r in rows if not r['remittances']),
            'overdueCents': sum(r['differenceCents'] for r in rows if r['overdueDays'] and r['differenceCents'] > 0),
            'scope': 'Customer billing checked against remittance notices in the private archive.'}
