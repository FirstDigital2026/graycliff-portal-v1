"""Separate Spectrum billing/remittance evidence; never infer bank settlement."""
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from kinetic_tracking import reconcile


def line_totals(doc):
    result=defaultdict(lambda:[Decimal(0),0])
    lines=doc.get('lines',[]) if doc['kind']=='spectrum_invoice' else [l for r in doc.get('records',[]) for l in r['lines']]
    for line in lines:
        code=line['code'].removeprefix('70-').rstrip('+')
        result[code][0]+=Decimal(str(line['quantity']));result[code][1]+=line['amountCents']
    return {code:tuple(values) for code,values in result.items() if any(values)}


def build(docs,today=None):
    invoices=[d for d in docs if d['kind']=='spectrum_invoice']
    recaps=[d for d in docs if d['kind']=='spectrum_recap']
    area_projects=defaultdict(set);review=[]
    # Establish recap area -> invoice project only through matching full code totals.
    for recap in recaps:
        for invoice in invoices:
            if (recap['week'],recap['amountCents'])==(invoice['week'],invoice['amountCents']) and line_totals(recap)==line_totals(invoice):
                area_projects[recap['area']].add(invoice['projectCode'])
    all_invoices=list(invoices)
    for recap in recaps:
        projects=area_projects[recap['area']]
        if len(projects)!=1:
            review.append({'filename':recap['filename'],'reason':'No unique invoice project established for this recap area.'});continue
        all_invoices.append({**recap,'projectCode':next(iter(projects)),'sourceType':'recap'})
    project_areas={p:area for area,projects in area_projects.items() if len(projects)==1 for p in projects}
    groups=defaultdict(list)
    for d in all_invoices:groups[(d['projectCode'],d['week'])].append(d)
    unique=[]
    for key,versions in groups.items():
        if len({d['amountCents'] for d in versions})!=1:
            review.append({'filename':versions[0]['filename'],'reason':'Conflicting billing totals for '+str(key)+'; excluded.'});continue
        d=next((d for d in versions if d['kind']=='spectrum_invoice'),versions[0])
        unique.append({**d,'area':project_areas.get(d['projectCode'],'')})
    projects={d['projectCode'] for d in invoices};payments={};conflicts=set()
    for d in docs:
        if d['kind']!='remittance':continue
        stamp=(d.get('source') or {}).get('sentAt') or (d.get('source') or {}).get('receivedAt') or ''
        for line in d['lines']:
            if line['projectCode'] not in projects:continue
            key=(d['paymentNumber'],line['invoiceRef'],line['projectCode'])
            item={**line,'paymentNumber':d['paymentNumber'],'paymentDate':d['paymentDate'],'noticeDate':stamp[:10] or None,'sourceFile':d['filename']}
            if key in payments and item['amountCents']!=payments[key]['amountCents']:conflicts.add(key)
            elif key not in payments or (item['noticeDate'] and not payments[key]['noticeDate']):payments[key]=item
    receipts=[r for key,r in payments.items() if key not in conflicts]
    today=today or datetime.now(ZoneInfo('America/New_York')).date()
    weeks=[];unmatched=[];models=[]
    for project in sorted(projects):
        t=reconcile([d for d in unique if d['projectCode']==project],[r for r in receipts if r['projectCode']==project],today)
        weeks+=t['weeks'];unmatched+=t['unmatchedRemittances']
        models.append({'projectCode':project,'area':project_areas.get(project,''),'notice':t['model'],'payment':t['paymentModel']})
    review += [{'filename':str(key),'reason':'Conflicting remittance versions; excluded.'} for key in conflicts]
    return {'currency':'USD','invoices':unique,'review':review,'revenueCents':sum(d['amountCents'] for d in unique),
            'receiptsCents':sum(w['reportedCents'] for w in weeks),
            'tracking':{'asOf':today.isoformat(),'weeks':sorted(weeks,key=lambda w:(w['week'],w['projectCode']),reverse=True),
                        'models':models,'unmatchedRemittances':unmatched,'flagCount':sum(w['flagged'] for w in weeks)+len(conflicts),
                        'overdueCents':sum(w['differenceCents'] for w in weeks if w['overdueDays'] and w['differenceCents']>0)}}


def report(db):
    from kinetic_finance import initialize
    initialize(db)
    with db() as c:docs=[json.loads(r['snapshot']) for r in c.execute('SELECT snapshot FROM private_commercial_documents')]
    return build(docs)
