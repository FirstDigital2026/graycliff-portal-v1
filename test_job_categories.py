from operations_intake import category_for_subject

cases = {
    'FW: low light 7b84e': 'Outage',
    'No-Light at MST': 'Outage',
    'RE: PID #7030382 - ECO OUT_103 Sycamore St - Down Fiber Span': 'Outage',
    'Fiber Engagement Notification INC005002516962': 'Outage',
    'SCarolinaMDN Ciena Fiber Event INC005002477572': 'Outage',
    'CRQ000013093008 2A052A/B low light': 'CRQ',
    'CRQ 181972 7430 Fullard St': 'CRQ',
    'Nighttime maintenance - 464 Oak Ave': 'CRQ',
    'No light emergency tonight': 'Outage',
    'PID 7021433 5033 Erwin Ct': 'Standard',
    'NTP 3725254 permanent repair': 'Standard',
}
for subject, expected in cases.items():
    assert category_for_subject(subject) == expected, subject
print('Three job categories verified, including low-light CRQ and nighttime outage cases.')

from test_email_attachments import Store
from test_legacy_intake import replace
import operations_intake as op
s=Store()
replace(s, **{'Task Name':'117 S Main St(CRQ 138317)','Portal Project ID':'','Portal Work Order':'','Portal Job Type':'CRQ','City':'Darlington'})
try:
    op.locate(s,{'pid':'','wo':'','address':'117 S Main St','city':'Darlington','type':'Outage'})
except ValueError as e:
    assert 'separate event' in str(e)
else:
    raise AssertionError('A new outage must not silently attach to an older CRQ at the same address')
print('Outage and old CRQ at the same address require event review.')
