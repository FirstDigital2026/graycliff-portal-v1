from pathlib import Path
import sys, hashlib, base64, subprocess
from email.message import EmailMessage
import operations_intake as op
assert len(op.SIGNATURE_IMAGE_HASHES)==7
logo=b'verified-signature-logo'
op.SIGNATURE_IMAGE_HASHES.add(hashlib.sha256(logo).hexdigest())
assert op.signature_graphic('image001.png','image/png',logo)
assert not op.signature_graphic('image001.png','image/png',b'work-photo')
assert not op.signature_graphic('logo-damage-photo.jpg','image/jpeg',b'work-photo')
assert not op.signature_graphic('map.pdf','application/pdf',logo)
mail=EmailMessage();mail.set_content('Job photos and map.')
mail.add_alternative('<html><img src="cid:photo"><img src="cid:logo"><img src="data:image/png;base64,'+base64.b64encode(b'data-picture').decode()+'"></html>',subtype='html')
html=mail.get_payload()[1]
html.add_related(b'work-photo',maintype='image',subtype='png',cid='<photo>',filename='image001.png',disposition='inline')
html.add_related(logo,maintype='image',subtype='png',cid='<logo>',filename='image002.png',disposition='inline')
mail.add_attachment(b'plan-pdf',maintype='application',subtype='pdf',filename='map.pdf')
raw=mail.as_bytes()
op.graph.get_message_mime=lambda *args:raw
class Store:
 def __init__(self):self.files=[]
 def list_row_attachments(self,*args):return [{'name':n} for n,m,d in self.files]
 def attach_file_to_row(self,*args,filename,mime_type,data):self.files.append((filename,mime_type,data))
store=Store();op.capture_email_files(store,'token',{'id':'sample'},1,2,[],'fixture')
assert len(store.files)==4,store.files
assert any(n.endswith('.eml') and d==raw for n,m,d in store.files)
assert any(d==b'work-photo' for n,m,d in store.files)
assert any(d==b'data-picture' for n,m,d in store.files)
assert any(d==b'plan-pdf' for n,m,d in store.files)
assert not any(d==logo for n,m,d in store.files)
op.capture_email_files(store,'token',{'id':'sample'},1,2,[],'fixture')
assert len(store.files)==4
print('PASS: signature logos skipped; CID photos, data images, documents, original email and deduplication preserved.')
