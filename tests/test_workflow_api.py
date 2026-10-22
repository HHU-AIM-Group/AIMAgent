"""Local HTTP integration tests; all reports and jobs are disposable fixtures."""
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

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import pipeline
from sources import make_item


class ResearchWorkflowAPI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.radar=app.RadarApp(self.temp.name)
        self.item=make_item('Explicit fixture old report','https://arxiv.org/abs/2610.12345',arxiv_id='2610.12345')
        self.report=dict(id='abcdef0123456789',start='2026-10-12',end='2026-10-14',created_at='2026-10-14T00:00:00Z',analysis_mode='heuristic',summary='Explicit fixture',papers=[self.item],news=[],sources=[],warnings=[])
        self.radar.store.save_report(self.report)
        self.server=app.RadarServer(('127.0.0.1',0),self.radar)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()
    def request(self,method,path,payload=None,csrf=True):
        headers={}
        if payload is not None:
            headers={'Content-Type':'application/json','X-CSRF-Token':self.radar.csrf_token if csrf else ''}
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=8)
        conn.request(method,path,body=json.dumps(payload).encode() if payload is not None else None,headers=headers)
        res=conn.getresponse();body=res.read();status=res.status;conn.close()
        return status,json.loads(body)
    def finish(self,jid):
        for _ in range(150):
            status,result=self.request('GET','/api/jobs/'+jid)
            job=result['job']
            if job['status'] in ('completed','failed'): return job
            time.sleep(.01)
        self.fail('fixture job did not finish')

    def test_old_report_analysis_persists_for_detail_and_export_without_changing_facts(self):
        result=dict(item_id=self.item['id'],status='insufficient',full_text={'status':'unavailable'},motivation='',problem='',claims=[],anchor_comparisons=[],limitations=['Explicit no full-text fixture'])
        with patch.object(app,'analyze',return_value=result):
            status,data=self.request('POST','/api/analysis',{'report_id':self.report['id'],'item_id':self.item['id']})
            self.assertEqual(status,202);job=self.finish(data['job']['id'])
        self.assertEqual(job['status'],'completed')
        status,detail=self.request('GET','/api/analysis/'+self.item['id'])
        self.assertEqual(detail['analysis'],result)
        report=self.radar.store.report(self.report['id']);self.assertEqual(report['papers'][0]['research_analysis'],result)
        self.assertEqual(report['papers'][0]['title'],self.item['title'])
        self.assertEqual(report['papers'][0]['url'],self.item['url'])
        self.assertIn('Explicit no full-text fixture',pipeline.report_markdown(report))

    def test_failed_analysis_retains_prior_result(self):
        previous={'status':'completed','motivation':'Prior fixture'}
        self.radar.store.set('analysis:'+self.item['id'],previous)
        with patch.object(app,'analyze',side_effect=RuntimeError('Fixture model outage')):
            status,data=self.request('POST','/api/analysis',{'report_id':self.report['id'],'item_id':self.item['id']})
            self.assertEqual(status,202);job=self.finish(data['job']['id'])
        self.assertEqual(job['status'],'failed')
        self.assertEqual(self.request('GET','/api/analysis/'+self.item['id'])[1]['analysis'],previous)
        self.assertEqual(self.radar.store.report(self.report['id']),self.report)

    def test_insufficient_retry_retains_prior_completed_analysis(self):
        previous={'status':'completed','motivation':'Prior successful fixture','claims':[]}
        self.radar.store.set('analysis:'+self.item['id'],previous)
        report=copy.deepcopy(self.report);report['papers'][0]['research_analysis']=previous
        self.radar.store.save_report(report)
        insufficient={'status':'insufficient','motivation':'','claims':[],'limitations':['Fixture source outage']}
        with patch.object(app,'analyze',return_value=insufficient):
            status,data=self.request('POST','/api/analysis',{'report_id':self.report['id'],'item_id':self.item['id']})
            self.assertEqual(status,202);job=self.finish(data['job']['id'])
        self.assertEqual(job['status'],'failed')
        self.assertEqual(self.request('GET','/api/analysis/'+self.item['id'])[1]['analysis'],previous)
        self.assertEqual(self.radar.store.report(self.report['id']),report)

    def test_saved_snapshot_registers_without_old_report_and_survives_restart(self):
        status,data=self.request('POST','/api/tracking',{'item':self.item,'tracked':True})
        self.assertEqual(status,200)
        restored=app.RadarApp(self.temp.name)
        self.assertEqual(restored.store.get('tracking:items')[0]['item']['title'],self.item['title'])
        self.assertEqual(self.request('POST','/api/tracking',{'report_id':'not-found','item_id':self.item['id']})[0],400)
        self.assertEqual(self.request('POST','/api/tracking',{'item':self.item,'tracked':False})[0],200)
        self.assertEqual(self.request('GET','/api/tracking')[1]['items'],[])

    def test_new_mutations_require_csrf_and_reject_private_snapshot_urls(self):
        for endpoint,payload in [('/api/analysis',{'item':self.item}),('/api/tracking',{'item':self.item}),('/api/tracking/check',{})]:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.request('POST',endpoint,payload,csrf=False)[0],403)
        malicious=copy.deepcopy(self.item);malicious['full_text_url']='https://127.0.0.1/private'
        self.assertEqual(self.request('POST','/api/analysis',{'item':malicious})[0],400)
        self.assertEqual(self.request('POST','/api/tracking',{'item':malicious})[0],400)


class ReportExportCompatibility(unittest.TestCase):
    def test_new_export_fields_and_only_current_tracking_changes(self):
        item=make_item('Explicit fixture','https://example.com/paper')
        item['research_analysis']=dict(status='completed',motivation='Fixture motivation',problem='Fixture problem',full_text={'url':item['url']},claims=[dict(claim='Fixture claim',experiment='Fixture experiment',evidence_location='P0001',evidence_quote='<script>fixture</script>',support='insufficient',gaps=['Fixture gap'])],anchor_comparisons=[dict(anchor='Fixture anchor',source_url='https://example.com/anchor',evidence_basis='Fixture body',task='Fixture task',data='Fixture data',method='Fixture method',supervision='Fixture labels',evaluation='Fixture metric',comparability='Fixture mismatch',unknowns=['Fixture unknown'],borrowable_points=['Fixture idea'],minimal_experiment='Fixture small experiment')],limitations=[])
        report=dict(start='2026-10-12',end='2026-10-14',analysis_mode='heuristic',summary='Fixture',papers=[item],news=[],sources=[],warnings=[],tracking_updates=dict(last_checked_at='now',changes=[dict(at='before',message='old-change'),dict(at='now',message='new-change')],items=[]))
        exported=pipeline.report_markdown(report)
        for value in ['Fixture motivation','Fixture claim','P0001','Fixture mismatch','Fixture small experiment','new-change']:
            self.assertIn(value,exported)
        self.assertNotIn('old-change',exported);self.assertNotIn('<script>',exported)
        del item['research_analysis'];del report['tracking_updates']
        legacy=pipeline.report_markdown(report)
        self.assertNotIn('单篇全文核对',legacy);self.assertNotIn('本次跨周追踪',legacy)


if __name__=='__main__':unittest.main()
