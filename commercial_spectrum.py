"""Private Spectrum financial evidence, classified by work codes."""
import re
from datetime import date
from decimal import Decimal
import billing_recaps as br


def parse(text, filename):
    if not re.search(r'First\s+Digital',text,re.I):raise ValueError('Contractor not verified')
    if re.search(r'Payroll Recaps',text,re.I):
        d=br.parse_text(text,filename)
        return {**d,'kind':'spectrum_recap','business':'Spectrum','amountCents':d['totalCents']}
    if not re.search(r'Graycliff',text,re.I):raise ValueError('Payer not verified')
    week=re.search(r'Week\s+Ending\s*:?\s*(\d+)/(\d+)/(\d+)',text,re.I)
    project=re.search(r'Job\s*#\s*:\s*([A-Z0-9]+)',text,re.I)
    if not project:project=re.search(r'(?m)^\s*(\d{6,})\s*-\s*(?:FLORENCE|COLUMBIA),\s*SC\b',text,re.I)
    total=re.findall(r'(?m)^\s*Total:\s*(\(?\$[\d,]+\.\d{2}\)?)',text)
    def cents(value):return int(Decimal(re.sub(r'[,$()]','',value))*100)*(-1 if '(' in value else 1)
    if not week or not project or len(total)!=1:raise ValueError('Invoice identity missing')
    text=re.sub(r'(?m)^(\S+[^\n]*?)\n([A-Z 0-9&/-]+)\n(?=\d+\s+\$)',r'\1 \2 ',text)
    lines=[]
    for raw in text.splitlines():
        m=re.match(r'^((?:70-)?[A-Z]{2}\d{2}[A-Z]?\+?|257)\s+(.+?)\s+(-?[\d,.]+)\s+(\(?\$[\d,]+\.\d{2}\)?)\s*(?:\$([\d,]+\.\d+))?\s*$',raw.strip())
        if not m:continue
        code,description,qty,amount,rate=m.groups();q=Decimal(qty.replace(',',''))
        actual=cents(amount)
        if rate is not None and (q*Decimal(rate.replace(',',''))*100).quantize(Decimal('1'))!=actual:raise ValueError('Invoice arithmetic does not reconcile')
        lines.append({'code':re.sub(r'^70-','',code).rstrip('+'),'description':description,'quantity':str(q),'amountCents':actual})
    expected=cents(total[0])
    if not lines or sum(l['amountCents'] for l in lines)!=expected:raise ValueError('Spectrum invoice detail total does not reconcile')
    m,d,y=map(int,week.groups())
    return {'kind':'spectrum_invoice','business':'Spectrum','projectCode':project[1],'week':date(y,m,d).isoformat(),'amountCents':expected,'lines':lines}
