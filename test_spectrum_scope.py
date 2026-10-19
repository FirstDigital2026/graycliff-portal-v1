import operations_intake as op
from test_email_attachments import Store

base={'subject':'PID 9000001 999 Test Road Columbia SC','body':{'content':'Spectrum work order. Address: 999 Test Road, Columbia SC.'}}
assert op.message_item(Store(),base,[])['pid']=='9000001'
for excluded in ['Lumos','SEGRA','Kinetic','SEFH15101','Lewisville']:
    for message in [dict(base,subject=base['subject']+' '+excluded),dict(base,conversationId='known-chain',body={'content':'Spectrum and '+excluded})]:
        try:op.message_item(Store(),message,[])
        except ValueError as exc:assert 'Spectrum scope' in str(exc)
        else:raise AssertionError('Non-Spectrum mail reached an operational job')
try:op.message_item(Store(),dict(base,body={'content':'Graycliff job with no verified customer'}),[])
except ValueError as exc:assert 'scope is not established' in str(exc)
else:raise AssertionError('Unknown new customer accepted')
reply={'subject':'Re: PID 7021433 5033 Erwin Ct','body':{'content':'Follow-up photos'},'conversationId':'known-chain'}
assert op.message_item(Store(),reply,[])['pid']=='7021433'
print('Spectrum-only creation, mixed/out-of-scope rejection, and existing-job replies passed.')
