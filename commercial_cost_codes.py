"""Resolve summary cost codes only from reconciled, full-code recap detail."""
import re
from collections import defaultdict, Counter
from decimal import Decimal
import kinetic_finance as k


def fingerprint(s):return re.sub('[^A-Z0-9]','',k.norm(s)).translate(str.maketrans({'O':'0','I':'1','L':'1'}))


def enrich(doc,pages):
    groups=defaultdict(list)
    for i,line in enumerate(doc['lines']):
        if line.get('subCostEstimateCents') is None:groups[tuple(sorted(line.get('codeCandidates') or []))].append(i)
    replacements={};evidence=[]
    for candidates,indices in groups.items():
        if not candidates:continue
        details=[]
        for page in pages:
            if page.get('invoiceBody'):continue
            versions=[]
            for text in [page.get('recapRows',''),page.get('recapBody',''),page.get('text','')]:
                found=[]
                for raw in text.splitlines():
                    compact=fingerprint(raw)
                    matches=[c for c in candidates if compact.startswith(fingerprint(c))]
                    fallback=False
                    if not matches and all(c.startswith('BFOV(') for c in candidates) and re.match(r'^B[I1]?F[O0]V',raw,re.I):
                        way=re.search(r'([12IQ])W',raw,re.I)
                        if way:fallback=True;matches=[c for c in candidates if '('+way[1].upper().replace('I','1').replace('Q','2')+'W)' in c]
                    if not matches and all(c.startswith('BM60(') for c in candidates) and re.match(r'^BM',raw,re.I) and not re.search(r'^BM\S*\(2\)',raw,re.I):
                        prefix=raw.split('UNDERGROUND')[0]
                        if re.search(r'1\s*/?\s*4\)P',prefix,re.I):fallback=True;matches=[c for c in candidates if ')PF' in c]
                    if matches and not fallback:
                        size=max(len(fingerprint(c)) for c in matches);matches=[c for c in matches if len(fingerprint(c))==size]
                    if not matches:continue
                    costs=[k.COST.get(c) for c in matches]
                    if not all(costs) or len({c['rate'] for c in costs})!=1:continue
                    match=re.search(r'([\d,ISOl|]+(?:\.\d+)?)\s+\$(\d[\d,. ]*\d)\s*\|?\s*$',raw,re.I)
                    if not match:continue
                    if '.' not in match[2]:continue
                    q=Decimal(match[1].upper().replace(',','').translate(str.maketrans({'I':'1','S':'5','O':'0','L':'1','|':'1'})))
                    # OCR can split or misplace currency separators. Keep every
                    # digit; cents, quantity/rate and whole-family totals must agree.
                    amount=int(re.sub(r'\D','',match[2]))
                    rates={doc['lines'][i].get('rateCents') for i in indices}
                    if len(rates)!=1 or None in rates or k.money(q*Decimal(next(iter(rates)))/100)!=amount:continue
                    found.append({'code':matches[0] if len(matches)==1 else ' / '.join(matches),'codeCandidates':matches,
                                    'quantity':str(q),'amountCents':amount,'rateCents':next(iter(rates)),
                                    'rateCardAmountCents':None,'subCostEstimateCents':k.money(q*Decimal(costs[0]['rate'])),
                                    'subRateCents':k.money(costs[0]['rate']),'description':raw,'costCodeSourcePage':page['page']})
                versions.append(found)
            # Combine independently validated rows without counting repeated OCR
            # reads twice. Preserve legitimate identical rows by their max count.
            counts=Counter();examples={}
            for version in versions:
                seen=Counter()
                for line in version:
                    sig=(line['subRateCents'],line['quantity'],line['amountCents'])
                    seen[sig]+=1
                    if sig not in examples or len(line['codeCandidates'])<len(examples[sig]['codeCandidates']):examples[sig]=line
                counts|=seen
            details += [examples[sig] for sig,count in counts.items() for _ in range(count)]
        if not details:continue
        if sum(Decimal(l['quantity']) for l in details)!=sum(Decimal(doc['lines'][i]['quantity']) for i in indices):continue
        if sum(l['amountCents'] for l in details)!=sum(doc['lines'][i]['amountCents'] for i in indices):continue
        replacements[indices[0]]=details
        for i in indices[1:]:replacements[i]=[]
        evidence.append({'summaryLines':[i+1 for i in indices],'recapPages':sorted({l['costCodeSourcePage'] for l in details}),'method':'Full recap codes; quantities and customer amounts exactly match summary'})
    if replacements:
        doc={**doc,'lines':[line for i,old in enumerate(doc['lines']) for line in replacements.get(i,[old])]}
        doc['costCodeEvidence']=evidence
        if sum(l['amountCents'] for l in doc['lines'])!=doc['amountCents']:raise ValueError('Enriched recap total mismatch')
    return doc
