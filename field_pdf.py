"""Create pricing-free technical PDF copies; never alter retained originals."""
import io,json,re,subprocess,sys,tempfile,hashlib,os
from pathlib import Path

PRICE=re.compile(r'\b(?:Total\s+(?:Project\s+)?Cost|PO\s*Amount|Unit\s+(?:Price|Rate)|(?:Total|Extended)\s+(?:Price|Cost|Amount)|Sub\s*Payout|Emp\s*Payout|Invoice\s*Total)\s*:?\s*[$£€]?\s*[\d,]+(?:\.\d+)?',re.I)
LABEL=re.compile(r'\b(?:Total\s+(?:Project\s+)?Cost|PO\s*Amount|Unit\s+(?:Price|Rate)|(?:Total|Extended)\s+(?:Price|Cost|Amount)|Sub\s*Payout|Emp\s*Payout|Invoice\s*Total)\b|[$£€]\s*\d',re.I)
NTP=re.compile(r'Notice\s+to\s+Proceed|PRISM\s+ID\s*:[\s\S]*Work\s+Order\s*#\s*:',re.I)

def regions(words):
    """Locate entire labeled monetary fields, including their values."""
    rows=[]
    for w in sorted(words,key=lambda w:(w[1],w[0])):
        row=next((r for r in rows if abs(r[0][1]-w[1])<max(3,(w[3]-w[1])*.35)),None)
        if row is None:rows.append([w])
        else:row.append(w)
    boxes=[]
    for row in rows:
        row.sort(key=lambda w:w[0]);text='';spans=[]
        for w in row:
            start=len(text);text+=str(w[4])+' ';spans.append((start,len(text)-1,w))
        matches=list(PRICE.finditer(text))
        # Currency amounts without a label are removed in their entirety too.
        matches+=list(re.finditer(r'[$£€]\s*[\d,]+(?:\.\d+)?',text))
        masked=text
        for m in sorted(matches,key=lambda m:m.start(),reverse=True):masked=masked[:m.start()]+' '*(m.end()-m.start())+masked[m.end():]
        if LABEL.search(masked):raise ValueError('Unresolved pricing table or amount; retain original privately')
        for m in matches:
            hit=[w for start,end,w in spans if start<m.end() and end>m.start()]
            if hit:boxes.append((min(w[0] for w in hit)-1,min(w[1] for w in hit)-1,max(w[2] for w in hit)+1,max(w[3] for w in hit)+1))
    return boxes

def ocr_image(raw,folder,index):
    from PIL import Image
    target=folder/('image-'+str(index)+'.png')
    with Image.open(io.BytesIO(raw)) as im:
        if im.width*im.height>25000000:raise ValueError('Image exceeds bounded field review')
        im.convert('RGB').save(target)
    result=subprocess.run(['node','--max-old-space-size=128',str(Path(__file__).with_name('field_ocr.cjs')),str(target)],capture_output=True,text=True,timeout=90,check=True)
    return target,json.loads(result.stdout)

def sanitize(raw,folder):
    import pymupdf as fitz
    from PIL import Image,ImageDraw
    doc=fitz.open(stream=raw,filetype='pdf')
    if len(doc)>60:raise ValueError('Document exceeds field review page limit')
    changed=0;checked={}
    for page in doc:
        text=page.get_text()
        if NTP.search(text):raise ValueError('Original NTP remains office-only')
        boxes=regions(page.get_text('words'))
        for box in boxes:page.add_redact_annot(fitz.Rect(box),fill=(1,1,1))
        if boxes:page.apply_redactions(images=0,graphics=0);changed+=len(boxes)
        for image in page.get_images(full=True):
            xref=image[0]
            if xref in checked:continue
            info=doc.extract_image(xref);data=info['image']
            path,read=ocr_image(data,folder,xref)
            if NTP.search(read['text']):raise ValueError('Original NTP image remains office-only')
            boxes=regions(read['words']);checked[xref]=True
            if boxes:
                with Image.open(path) as im:
                    im=im.convert('RGB');draw=ImageDraw.Draw(im)
                    for box in boxes:draw.rectangle(box,fill='white')
                    buf=io.BytesIO();im.save(buf,format='PNG')
                    page.replace_image(xref,stream=buf.getvalue());im.close()
                changed+=len(boxes)
    # Full rewrite removes replaced image bytes, redacted content and metadata.
    doc.set_metadata({})
    while doc.embfile_count():doc.embfile_del(0)
    doc.set_xml_metadata('')
    out=doc.tobytes(garbage=4,deflate=True,clean=True);doc.close()
    return out,changed

def prepare(raw):
    import fcntl
    with open(Path(tempfile.gettempdir())/'graycliff-commercial-ocr.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='field-pdf-') as folder:
            root=Path(folder);(root/'source.pdf').write_bytes(raw)
            result=subprocess.run([sys.executable,str(Path(__file__)),str(root)],capture_output=True,timeout=240,check=True)
            meta=json.loads(result.stdout)
            return (root/'field.pdf').read_bytes(),meta['redactions']

if __name__=='__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(240*1024*1024,240*1024*1024))
    root=Path(sys.argv[1]);out,n=sanitize((root/'source.pdf').read_bytes(),root)
    (root/'field.pdf').write_bytes(out);print(json.dumps({'redactions':n}))
