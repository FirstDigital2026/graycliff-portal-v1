from test_email_attachments import Store
import operations_intake as op

def replace(store, **values):
    for cell in store.row['cells']:
        name=next(c['title'] for c in store.columns if c['id']==cell['columnId'])
        if name in values: cell['value']=values[name]

s=Store()
replace(s, **{'Task Name':'1332 Thompson Lane CRQ','City':'Johnsonville,SC','Portal Project ID':'','Portal Work Order':'','Portal Job Type':'','Notes':'CRQ needed'})
m={'id':'legacy','subject':'FW: PID 6483646 - 1332 Thompson Ln, Johnsonville, SC 29555','body':{'content':'Documents attached.'}}
item=op.message_item(s,m,[])
assert op.locate(s,item)[2]['id']==99
op.save_message(s,'',m,item,[],'legacy',{'created':0,'updated':0})
assert op.vals(s.get_sheet(op.AREA['Columbia']),s.row)['Portal Job Type']=='CRQ'
assert op.vals(s.get_sheet(op.AREA['Columbia']),s.row)['Status']=='In Progress'
for subject in ['FW: P#7021433 Opt_Port_Act_5033 ERWIN CT','FW: Push Light-5033 Erwin Ct-pid-7021433','FW: "7021433 5033 Erwin Ct"']:
    assert op.parse({'subject':subject,'body':{'content':''}},[])['pid']=='7021433',subject
s=Store()
replace(s,**{'Task Name':'40 Pearson Road cell tower','Portal Project ID':'','Portal Work Order':''})
item=op.message_item(s,{'subject':'FW: PR - 40 Pearson Rd','body':{'content':'Map attached.'}},[])
assert item['address']=='40 Pearson Road cell tower'
assert op.locate(s,item)[2]['id']==99
try:
    op.locate(Store(),{'pid':'9999999','wo':'','address':'5033 Erwin Court','city':'Elgin'})
    raise AssertionError('Conflicting PID matched by address')
except ValueError: pass
try:
    op.locate(Store(),{'pid':'','wo':'','address':'5033 Erwin Ct','city':'Different City'})
    raise AssertionError('Existing address with conflicting city was treated as a new job')
except ValueError: pass
try:
    op.message_item(Store(),{'subject':'FW: PR - 999 Unknown Road','body':{'content':''}},[])
    raise AssertionError('Unknown address accepted without reliable job data')
except ValueError: pass
print('Legacy road aliases, exact existing-address fallback, subject IDs, CRQ preservation and conflict rejection passed.')
