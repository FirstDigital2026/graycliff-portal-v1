import unittest,threading,time,hmac,hashlib,json,os,sys,types
from unittest.mock import patch
try:
    from flask import Flask
except ImportError:
    # The local artifact runtime omits Flask. Exercise the same route callback;
    # the deployed test uses Flask's real test client without any mail transport.
    request=types.SimpleNamespace()
    request.get_data=lambda:request.raw
    request.get_json=lambda **kwargs:json.loads(request.raw)
    sys.modules['flask']=types.SimpleNamespace(request=request,jsonify=lambda data:data)
    class Flask:
        def __init__(self,*args):pass
        def post(self,path):
            def register(fn):self.callback=fn;return fn
            return register
        def test_client(self):
            def post(path,data,content_type,headers=None):
                request.raw=data;request.headers=headers or {};result=self.callback();return types.SimpleNamespace(status_code=result[1])
            return types.SimpleNamespace(post=post)
import mail_automation as m

class ImmediateMailTests(unittest.TestCase):
    def test_signed_wake_starts_processing_without_email_side_effects(self):
        app=Flask('fixture-immediate-mail');store=type('Store',(),{'token':'fixture-key'})();m.install(app,store,None);client=app.test_client()
        raw=json.dumps({'eventId':'12345678-1234-1234-1234-123456789012'}).encode();path='/api/portal/process-updates';ts=str(int(time.time()));canonical='fdgc-mail-bridge-v1\n'+ts+'\nPOST\n'+path+'\n'+hashlib.sha256(raw).hexdigest();sig=hmac.new(store.token.encode(),canonical.encode(),hashlib.sha256).hexdigest()
        with patch.dict(os.environ,{'GRAYCLIFF_BRIDGE_SECRET':''}),patch.object(m,'wake') as wake,patch.object(m.graph,'_request',side_effect=AssertionError('No live email transport')):
            self.assertEqual(client.post(path,data=raw,content_type='application/json').status_code,403);wake.assert_not_called()
            result=client.post(path,data=raw,content_type='application/json',headers={'X-FDGC-Time':ts,'X-FDGC-Signature':sig})
            self.assertEqual(result.status_code,202);wake.assert_called_once_with(store,None)
    def test_new_wake_during_processing_is_not_lost(self):
        entered=threading.Event();release=threading.Event();twice=threading.Event();calls=[]
        def prepare(*args):
            calls.append(1)
            if len(calls)==1:entered.set();release.wait(2)
            else:twice.set()
            return {'ok':True}
        with patch.object(m,'prepare',side_effect=prepare):
            m.wake(None,None);self.assertTrue(entered.wait(1));m.wake(None,None);release.set();self.assertTrue(twice.wait(1))
            limit=time.monotonic()+1
            while m.WAKE_GUARD.locked() and time.monotonic()<limit:time.sleep(.005)
        self.assertEqual(len(calls),2)

if __name__=='__main__':unittest.main()
