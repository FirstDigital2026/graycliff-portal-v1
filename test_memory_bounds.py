import ast
import pathlib
import unittest
from unittest.mock import patch
from smartsheet_api import SmartsheetClient, CacheItem
import memory_runtime as runtime

class MemoryBoundsTests(unittest.TestCase):
    def client(self):
        c=SmartsheetClient('fictional');c.cache_entries=2;c.cache_bytes=4096;c.cache_item_bytes=2048
        c._request=lambda *a,**k:{'rows':[{'value':a[1]}]}
        return c
    def test_many_sheets_evict_oldest(self):
        c=self.client()
        for i in range(100):c.get_sheet(i)
        self.assertEqual(len(c._cache),2)
        self.assertLessEqual(sum(v.size for v in c._cache.values()),c.cache_bytes)
        self.assertNotIn('sheet:0',c._cache)
    def test_expired_sheet_released_on_other_fetch(self):
        c=self.client();c._cache['sheet:old']=CacheItem(0,{'large':'stale'})
        c.get_sheet(1);self.assertNotIn('sheet:old',c._cache)
    def test_force_drops_old_payload_before_network(self):
        c=self.client();c.get_sheet(1)
        def fetch(*a,**k):
            self.assertNotIn('sheet:1',c._cache)
            return {'rows':[]}
        c._request=fetch;c.get_sheet(1,force=True)
    def test_oversize_is_returned_but_never_retained(self):
        c=self.client();c._request=lambda *a,**k:{'value':'X'*10000}
        self.assertEqual(len(c.get_sheet(1)['value']),10000)
        self.assertFalse(c._cache)
    def test_recent_sheet_survives_capacity_eviction(self):
        c=self.client();c.get_sheet(1);c.get_sheet(2);c.get_sheet(1);c.get_sheet(3)
        self.assertIn('sheet:1',c._cache);self.assertNotIn('sheet:2',c._cache)
    def test_cleanup_runs_on_success_and_failure_without_hiding_error(self):
        c=self.client();c.get_sheet(1)
        with patch.object(runtime.ctypes,'CDLL'):
            self.assertEqual(runtime.background_job(lambda:42,c)(),42)
            self.assertFalse(c._cache)
            c.get_sheet(1)
            def fail():raise ValueError('original failure')
            with self.assertRaisesRegex(ValueError,'original failure'):runtime.background_job(fail,c)()
            self.assertFalse(c._cache)
    def test_scheduler_serializes_jobs_and_keeps_retry_intervals(self):
        tree=ast.parse(pathlib.Path(__file__).with_name('app.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='start_scheduler')
        class Scheduler:
            def __init__(self,**kw):self.kw=kw;self.jobs=[]
            def add_job(self,fn,*a,**kw):self.jobs.append((fn,kw))
            def start(self):pass
        funcs=['sync_technician_contacts','sync_billing_queue','sync_mobile_field_sheets','import_graycliff_mailbox','process_graycliff_status_updates']
        import os
        space={'scheduler':None,'os':os,'BackgroundScheduler':Scheduler,'ThreadPoolExecutor':lambda n:n,'background_job':runtime.background_job,'store':self.client(),**{n:(lambda:None) for n in funcs}}
        with patch.dict(os.environ,{'ENABLE_BACKGROUND_SYNC':'true'}):
            exec(compile(ast.Module(body=[fn],type_ignores=[]),'scheduler','exec'),space);space['start_scheduler']()
        sched=space['scheduler'];self.assertEqual(sched.kw['executors']['default'],1)
        self.assertEqual(len(sched.jobs),5)
        self.assertTrue(all(hasattr(fn,'__wrapped__') for fn,_ in sched.jobs))
        self.assertEqual(sched.jobs[3][1]['minutes'],1)
        self.assertGreaterEqual(sched.kw['job_defaults']['misfire_grace_time'],300)

if __name__=='__main__':unittest.main()
