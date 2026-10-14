import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pipeline
import sources
from storage import Store


class OfflineEndToEnd(unittest.TestCase):
    def test_all_source_fixtures_and_fake_codex_subprocess(self):
        """Explicit synthetic fixtures; never written to app's user data directory."""
        root=Path(__file__).resolve().parents[1]
        profile=json.loads((root/'config/profile.json').read_text())
        start,end='2026-10-12','2026-10-14'
        atom=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2610.12345v2</id><title>Fixture remote sensing foundation model</title><summary>Satellite multimodal model</summary><published>2026-10-12T00:00:00Z</published><updated>2026-10-13T00:00:00Z</updated></entry></feed>'''
        rss=b'''<rss><channel><item><title>Fixture open-source model launch</title><link>https://example.com/news</link><pubDate>Tue, 13 Oct 2026 00:00:00 GMT</pubDate><description>New model training and inference release.</description></item></channel></rss>'''
        class Fetch:
            def __init__(self,store,**kwargs):self.store=store
            def get(self,url,**kwargs):
                if 'arxiv' in url:return atom
                if 'mlr.press' in url:return b'<a href="https://proceedings.mlr.press/v267/">Fixture ICML</a>'
                if 'thecvf.com' in url:return b'<a href="https://openaccess.thecvf.com/CVPR2026/">Fixture CVPR</a>'
                if 'aclanthology.org' in url:return b'<a href="https://aclanthology.org/events/acl-2026/">Fixture ACL</a>'
                return rss
            def json(self,url,**kwargs):
                if 'daily_papers' in url:return [{'paper':{'id':'2610.12345','title':'Fixture remote sensing foundation model','summary':'Satellite multimodal model','publishedAt':'2026-10-12T00:00:00Z'}}]
                if '/groups?' in url:return {'groups':[{'content':{'submission_id':{'value':'Fixture/-/Submission'}}}]}
                if '/notes?' in url:return {'notes':[{'id':'fixture_public','forum':'fixture_public','odate':1791849600000,'content':{'title':{'value':'Fixture noisy correspondence learning'},'abstract':{'value':'Label noise and robust multimodal correspondence'},'venue':{'value':'Submitted to ICLR 2027'}}}]}
                return []
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp); executable=directory/'codex'
            executable.write_text('''#!/usr/bin/env python3
import sys,json
args=sys.argv[1:]
prompt=sys.stdin.read()
evidence=json.loads(prompt[prompt.index('{"profile":'):])['untrusted_evidence']
output={'summary':'离线 fixture，非真实 Codex 分析','items':[{'id':row['id'],'summary':'基于合成摘要','why_it_matters':'fixture 方向分析','directions':row['directions'],'score':81,'related_work':['invented anchor'],'suggested_action':'fixture 小实验'} for row in evidence]}
open(args[args.index('--output-last-message')+1],'w').write(json.dumps(output))
'''); executable.chmod(0o755)
            store=Store(directory/'db.sqlite')
            settings=json.loads(json.dumps(pipeline.DEFAULT_SETTINGS));settings['codex_path']=str(executable)
            with patch.object(sources,'Fetcher',Fetch),patch.object(sources,'NEWS_FEEDS',[('fixture','Fixture news','https://example.com/feed')]):
                report=pipeline.collect_report(store,profile,settings,start,end,True,data_dir=directory/'runtime')
            self.assertEqual(report['analysis_mode'],'codex', report['warnings'])
            self.assertEqual(len(report['papers']),2)
            self.assertEqual(len(report['news']),1)
            self.assertEqual(len(report['conferences']),3)
            self.assertTrue(all(row['baseline'] for row in report['conferences']))
            self.assertEqual(report['status'],'partial')
            self.assertTrue(all(item.get('ai_enriched') for item in report['papers']+report['news']))
            self.assertTrue(all('invented anchor' not in item['related_work'] for item in report['papers']+report['news']))
            merged=next(item for item in report['papers'] if 'remote sensing' in item['title'])
            self.assertGreaterEqual(len(merged['sources']),2)
            self.assertEqual(store.report(report['id']),report)


if __name__=='__main__':unittest.main()
