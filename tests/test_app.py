import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import pipeline
import sources


class DateInput(unittest.TestCase):
    def test_reversed_invalid_future_and_long_ranges(self):
        for start, end in [('2026-10-14','2026-10-12'),('bad','2026-10-14'),('2026-02-30','2026-03-01'),('2026-09-01','2026-10-14'),('2099-01-01','2099-01-02')]:
            with self.subTest(start=start,end=end), self.assertRaises(ValueError):
                pipeline.validate_window(start,end)
        self.assertEqual(pipeline.validate_window('2026-10-01','2026-10-14'), ('2026-10-01','2026-10-14'))


class HttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.radar = app.RadarApp(Path(cls.temp.name))
        cls.server = app.RadarServer(('127.0.0.1',0),cls.radar)
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=8)
        conn.request(method,path,body=body,headers=headers or {})
        res = conn.getresponse(); result=(res.status,dict(res.getheaders()),res.read());conn.close();return result

    def mutation(self, payload, headers=None, path='/api/settings'):
        values={'Content-Type':'application/json','X-CSRF-Token':self.radar.csrf_token}
        values.update(headers or {})
        return self.request('POST',path,json.dumps(payload).encode(),values)

    def test_root_static_and_health(self):
        self.assertEqual(self.request('GET','/')[0],200)
        self.assertEqual(self.request('GET','/static/app.js')[0],200)
        self.assertEqual(self.request('GET','/api/health')[0],200)

    def test_rejects_foreign_host_origin_missing_csrf_and_wrong_type(self):
        self.assertEqual(self.request('GET','/api/bootstrap',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.mutation({}, {'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.mutation({}, {'X-CSRF-Token':''})[0],403)
        self.assertEqual(self.mutation({}, {'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('OPTIONS','/api/settings')[0],405)

    def test_static_path_traversal_and_unsafe_setting_rejected(self):
        for path in ['/static/../app.py','/static/%2e%2e/app.py','/app.py','/data/radar.sqlite3']:
            with self.subTest(path=path):self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.mutation({'extra_feeds':['https://127.0.0.1/private']})[0],400)
        self.assertEqual(self.mutation({'codex_path':'/bin/sh'})[0],400)
        self.assertEqual(self.mutation({'sources':{'arxiv':'yes'}})[0],400)

    def test_settings_persist_and_exports_unicode_special_characters(self):
        self.assertEqual(self.mutation({'model':'gpt-6.1-sol'})[0],200)
        self.assertEqual(app.RadarApp(self.temp.name).settings['model'],'gpt-6.1-sol')
        item=sources.make_item('遥感 <script> & 噪声 [test]','https://example.com/?a=1&b=2',source='fixture',published_at='2026-10-12')
        report=dict(id='abcdef1234567890',start='2026-10-12',end='2026-10-14',created_at='2026-10-14T00:00:00Z',analysis_mode='heuristic',summary='fixture <script>',papers=[item],news=[],sources=[],warnings=[])
        self.radar.store.save_report(report)
        status,headers,body=self.request('GET','/api/reports/abcdef1234567890/export?format=json')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['papers'][0]['title'],item['title'])
        self.assertIn('attachment',headers['Content-Disposition'])
        status,_,body=self.request('GET','/api/reports/abcdef1234567890/export?format=markdown')
        self.assertEqual(status,200);self.assertIn('遥感 & 噪声',body.decode());self.assertIn('fixture &lt;script&gt;',body.decode())
        self.assertEqual(self.request('GET','/api/reports/abcdef1234567890/export?format=exe')[0],400)

    def test_failed_job_retains_previous_report(self):
        self.radar.store.save_report(dict(id='abcdef1234567890',created_at='2026-10-14T00:00:00Z',summary='previous fixture'))
        with patch.object(app,'collect_report',side_effect=RuntimeError('fixture failed source orchestration')):
            status,_,body=self.mutation({'start':'2026-10-12','end':'2026-10-14','use_codex':False},path='/api/runs')
            self.assertEqual(status,202); jid=json.loads(body)['job']['id']
            for _ in range(100):
                job=json.loads(self.request('GET','/api/jobs/'+jid)[2])['job']
                if job['status']=='failed':break
                time.sleep(.01)
        self.assertEqual(job['status'],'failed')
        self.assertIn('fixture failed',job['error'])
        self.assertIsNotNone(self.radar.store.report('abcdef1234567890'))


if __name__=='__main__':unittest.main()
