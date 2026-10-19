"""Node-only dispatches must classify from evidence, not the node itself."""
import operations_intake as op

body = 'Spectrum node down. Outage please run ASAP. Please roll a splicer. 3134 Wash Davis Rd, Summerton, SC 29148'
for subject in ('3M025', 'FW: [EXTERNAL] FW: 3M025', 'RE: 1O69E'):
    message = {'subject': subject, 'body': {'contentType': 'text', 'content': body}}
    assert op.spectrum_scope(message, [])
    item = op.parse(message, [])
    assert item['type'] == 'Outage', item
    assert item['address'] == '3134 Wash Davis Rd', item
    assert item['city'] == 'Summerton' and item['market'] == 'Columbia', item

assert op.category_for_message('3M025', 'Spectrum node details') == 'Standard'
assert op.category_for_message('3M025', 'No outage. Please roll a splicer ASAP.') == 'Standard'
assert op.category_for_message('3M025', 'CRQ1234 maintenance window. Low light, please roll a splicer.') == 'CRQ'
assert op.category_for_message('PID 7021433', body) == 'Standard'
assert op.category_for_message('CRQ1234', body) == 'CRQ'
try:
    op.spectrum_scope({'subject':'3M025','body':{'content': body + ' Kinetic'}}, [])
except ValueError:
    pass
else:
    raise AssertionError('Mixed customer scope must still require review')
print('Node-only outage dispatch, Summerton routing, CRQ precedence and scope guards verified.')
