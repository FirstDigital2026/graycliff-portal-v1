"""Read-only actual billing totals from accepted production package snapshots."""
import json,re
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP


def cents(value):
    if isinstance(value,bool) or value is None:return None
    try:
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<0 or amount>Decimal('9999999999'):return None
        return int(amount.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)*100)
    except (InvalidOperation,ValueError,TypeError):return None


def report(db):
    with db() as c:
        rows=c.execute("SELECT id,job_key,snapshot,sent_at FROM billing_package_outbox WHERE state='ACCEPTED' ORDER BY created_at DESC,id DESC").fetchall()
    jobs=[];seen=set();excluded=0
    for row in rows:
        # Old internal tests used suffixed keys. They are not customer revenue.
        if not re.fullmatch(r'\d+:\d+',row['job_key']):excluded+=1;continue
        try:snap=json.loads(row['snapshot'])
        except (ValueError,TypeError):continue
        source=snap.get('input',{})
        if source.get('testTo') or str(source.get('sheetId'))+':'+str(source.get('rowId'))!=row['job_key']:
            excluded+=1;continue
        if row['job_key'] in seen:continue
        seen.add(row['job_key']);pricing=snap.get('pricing') or {};lines=[]
        for line in pricing.get('lines',[]):
            if not isinstance(line,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,24}',str(line.get('code',''))):continue
            amount=cents(line.get('total'))
            if amount is not None:lines.append({'code':line['code'],'amountCents':amount,'quantity':line.get('quantity',0)})
        jobs.append({'sourceKey':row['job_key'],'packageId':row['id'],'sheetId':str(source['sheetId']),
                     'rowId':str(source['rowId']),'title':snap.get('draft',{}).get('title',''),
                     'sentAt':row['sent_at'] or '', 'amountCents':cents(pricing.get('amount')),
                     'lines':lines,'source':'Sent billing spreadsheet'})
    return {'jobs':jobs,'excludedTestSends':excluded,'currency':'USD'}
