"""Read image-only billing PDFs privately, with exact financial reconciliation."""
import json,re,subprocess,tempfile,os,gc
from pathlib import Path
from decimal import Decimal
import kinetic_finance as k

def skeleton(s):
    return re.sub('1+','1',k.norm(s).translate(str.maketrans({'I':'1','L':'1','O':'0','S':'5'})))

def scan_money(s):
    # OCR sometimes reads a thousands comma as a period. The final two
    # decimal places remain mandatory; line arithmetic must still reconcile.
    whole,cents=s.rsplit('.',1)
    return k.money(re.sub(r'[,.]','',whole)+'.'+cents)

def codes(raw,rate):
    token=raw.split()[0]
    if token.upper()=='FDH':
        parts=raw.split();token+=''.join(parts[1:2])
        if len(parts)>2 and parts[2].upper().startswith('DU'):token+=parts[2]
    key=k.norm(token);matches=[c for c in k.REV if c==key]
    if not matches:matches=[c for c in k.REV if skeleton(c)==skeleton(key)]
    if not matches:matches=[c for c in k.REV if skeleton(c).startswith(skeleton(key))]
    if not matches:raise ValueError('Scanned billing code could not be verified')
    priced=[c for c in matches if rate is not None and k.money(k.REV[c]['rate'])==rate]
    return priced or matches

def scanned_invoice(pages,alternative=False):
    full='\n'.join(p['text'] for p in pages)
    if not re.search(r'First\s+Digital',full,re.I) or not re.search(r'G?raycliff',full,re.I):raise ValueError('Scanned contractor/payer not verified')
    bodies=[p['invoiceBody'] for p in pages if p.get('invoiceBody')]
    weeks=set(re.findall(r'Week Ending:\s*(\d+)/(\d+)/(\d+)',full,re.I))
    projects=set(re.findall(r'Job\s*#\s*:\s*(\d{3}[A-Z][A-Z0-9]+)',full,re.I))
    totals=set(k.money(v) for v in re.findall(r'Total:\s*\$([\d,]+\.\d{2})','\n'.join(bodies),re.I))
    if len(weeks)!=1 or len(projects)!=1 or len(totals)!=1:raise ValueError('Scanned invoice identity or total is ambiguous')
    lines=[]
    for page in (p for p in pages if p.get('invoiceBody')):
        body=page['invoiceBody']
        rawlines=body.splitlines()
        if alternative:
            financial=[raw for raw in rawlines if '$' in raw and not re.search(r'\bTotal:',raw,re.I)]
            alternate=[raw for raw in page.get('financeBody','').splitlines() if re.fullmatch(r'[\d$S,.\s|Il!:()\-]+',raw.strip()) and re.search(r'\d[,.]\d{2}',raw)]
            if len(alternate)==len(financial):
                # Pair rows only when both independent reads found the same
                # row count. Keep the code from the full-width read.
                rawlines=[raw.split('$')[0]+alt for raw,alt in zip(financial,alternate)]
        for raw in rawlines:
            amounts=list(re.finditer(r'\$\s*(\d[\d,.]*\.\d{2})(?!\d)',raw))
            if not amounts or re.search(r'\bTotal:',raw,re.I):continue
            # Ignore non-financial headers. Every monetary detail row must map
            # to a Kinetic work code, regardless of the town printed nearby.
            if not re.match(r'^[A-Z0-9(]',raw,re.I):continue
            final=amounts[-1];actual=scan_money(final.group(1));rate=None;q=None
            # Accounting parentheses are signed credits, not positive revenue.
            # Require both sides and retain the independent quantity/rate check.
            before=raw[:final.start()].rstrip();after=raw[final.end():].lstrip()
            credit=before.endswith('(') and after.startswith(')')
            if before.endswith('(')!=after.startswith(')'):raise ValueError('Scanned amount sign is ambiguous')
            if credit:actual=-actual
            if len(amounts)>=2:
                rate=scan_money(amounts[-2].group(1))
                qty=raw[amounts[-2].end():final.start()].strip().rstrip('(').strip().strip(':!')
                qty={'I':'1','|':'1','l':'1'}.get(qty,qty)
                if re.fullmatch(r'-?[\d,]+(?:\.\d+)?',qty):q=Decimal(qty.replace(',',''))
            else:
                qty=re.search(r'(\d[\d,]*(?:\.\d+)?)\s*$',raw[:final.start()])
                if qty:q=Decimal(qty.group(1).replace(',',''))
            candidates=codes(raw,rate)
            if q is None:raise ValueError('Scanned invoice quantity not verified')
            if rate is None or k.money(q*Decimal(rate)/100)!=actual:
                # A damaged rate glyph can be recovered only when the printed
                # quantity and extended amount agree with one known card rate.
                rates={k.money(k.REV[c]['rate']) for c in candidates if k.money(q*Decimal(k.REV[c]['rate']))==actual}
                if len(rates)!=1:raise ValueError('Scanned quantity, rate and amount do not reconcile')
                rate=next(iter(rates))
            canonical=candidates[0] if len(candidates)==1 else k.norm(raw.split()[0])
            card_rates={Decimal(k.REV[c]['rate']) for c in candidates};cost=k.COST.get(canonical) if len(candidates)==1 else None
            lines.append({'code':canonical,'codeCandidates':candidates,'quantity':str(q),'amountCents':actual,'rateCents':rate,'rateCardAmountCents':k.money(q*next(iter(card_rates))) if len(card_rates)==1 else None,'subCostEstimateCents':k.money(q*Decimal(cost['rate'])) if cost else None,'description':raw})
    total=next(iter(totals))
    if not lines or sum(l['amountCents'] for l in lines)!=total:raise ValueError('Scanned invoice detail does not match printed total')
    m,d,y=map(int,next(iter(weeks)))
    return {'kind':'invoice','sourceType':'automatic private OCR','business':'Kinetic','projectCode':next(iter(projects)).upper(),'week':k.date(y,m,d).isoformat(),'amountCents':total,'lines':lines}

def read_pages(data):
    # A service worker and an administrative retry must never run OCR together.
    # The OS releases this lock even if the reader exits unexpectedly.
    if os.name=='posix':
        import fcntl
        with open(Path(tempfile.gettempdir())/'graycliff-commercial-ocr.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:return _read_pages(data)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)
    return _read_pages(data)

def _read_pages(data):
    import pypdfium2 as pdfium
    with tempfile.TemporaryDirectory(prefix='private-commercial-') as folder:
        root=Path(folder);manifest=[]
        doc=pdfium.PdfDocument(data)
        if len(doc)>50:raise ValueError('Scanned PDF exceeds automatic page limit')
        try:
            for i in range(len(doc)):
                page=doc[i];bitmap=page.render(scale=3);im=bitmap.to_pil();path=root/f'page-{i+1}.png';im.save(path)
                item={'page':i+1,'path':str(path),'width':im.width,'height':im.height};im.close();bitmap.close()
                bitmap=page.render(scale=2);im=bitmap.to_pil();low=root/f'page-{i+1}-low.png';im.save(low)
                item.update(lowPath=str(low),lowWidth=im.width,lowHeight=im.height);manifest.append(item);im.close();bitmap.close();page.close()
        finally:doc.close()
        # Release page/bitmap buffers before starting the OCR subprocess.
        gc.collect()
        path=root/'manifest.json';path.write_text(json.dumps(manifest))
        result=subprocess.run(['node','--max-old-space-size=160',str(Path(__file__).with_name('commercial_ocr.cjs')),str(path)],capture_output=True,text=True,timeout=600,check=True)
        pages=json.loads(result.stdout)
    return pages

def read_pdf(data):
    pages=read_pages(data)
    from commercial_cost_codes import enrich
    try:return enrich(scanned_invoice(pages),pages),pages
    except ValueError:
        try:return enrich(scanned_invoice(pages,alternative=True),pages),pages
        except ValueError:pass
        # Text-based remittance and recap parsers also get the full OCR output.
        text='\n'.join(p['text'] for p in pages)
        try:return k.remittance(text),pages
        except ValueError:pass
        try:return k.invoice(text),pages
        except ValueError:raise ValueError('Automatic OCR could not reconcile this PDF; retained privately for retry')
