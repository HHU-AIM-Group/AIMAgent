import errno
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import codex_bridge
import launch
import sources
from storage import Store

START, END = '2026-10-12','2026-10-14'

def stamp(value):
    return int(sources.parse_date(value).timestamp()*1000)


class ProvenanceEdges(unittest.TestCase):
    def test_acceptance_mark_is_distinct_from_first_online(self):
        note={'id':'paper','forum':'paper','odate':stamp('2026-09-01'),'pdate':stamp(START),'content':{'title':{'value':'Remote sensing study'},'venue':{'value':'ICLR 2027 Poster'},'venueid':{'value':'ICLR.cc/2027/Conference'}}}
        item=sources.parse_openreview([note],START,END,'ICLR2027')[0]
        self.assertEqual(item['date_basis'],'accepted_this_week')
        self.assertTrue(item['original_published_at'].startswith('2026-09-01'))

    def test_update_is_not_first_publication(self):
        note={'id':'paper','forum':'paper','odate':stamp('2026-09-01'),'mdate':stamp(START),'content':{'title':{'value':'Remote sensing study'},'venue':{'value':'Submitted to ICLR 2027'}}}
        item=sources.parse_openreview([note],START,END,'ICLR2027')[0]
        self.assertEqual(item['date_basis'],'updated_this_week')
        self.assertIn('未核验',item['status'])

    def test_html_card_date_cannot_leak_to_neighbor_link(self):
        html=b'<html><a href="/old"><h3>Old release</h3><time>Oct 1, 2026</time></a><a href="/new"><h3>New release</h3><time>Oct 13, 2026</time></a><a href="/unknown"><h3>Undated release</h3></a></html>'
        items=sources.parse_news_html(html,'Official','https://example.com/',START,END)
        self.assertEqual([(x['title'],x['url']) for x in items],[('New release','https://example.com/new')])

    def test_new_proceedings_volume_harvests_and_preserves_first_seen(self):
        class Fetch:
            stage=0
            def get(self,url,**kwargs):
                if url=='https://proceedings.mlr.press/':
                    return b'<a href="/v1/">Old</a>' + (b'<a href="/v2/">New</a>' if self.stage else b'')
                if url=='https://proceedings.mlr.press/v2/':
                    return b'<a href="paper.html">Remote sensing multimodal model</a>'
                if url.endswith('/paper.html'):
                    return b'<div id="abstract">A satellite foundation model.</div>'
                return b''
        directions=[{'id':'remote','keywords':['remote sensing'],'anchors':['Anchor'],'name':'Remote'}]
        fetch=Fetch()
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db.sqlite')
            with patch.object(sources,'now_iso',return_value='2026-10-10T00:00:00Z'):
                self.assertEqual(sources.proceedings_watch(fetch,store,START,END,directions)[0],[])
            fetch.stage=1
            with patch.object(sources,'now_iso',return_value='2026-10-12T00:00:00Z'):
                items,confs,_=sources.proceedings_watch(fetch,store,START,END,directions)
            self.assertEqual(len(items),1)
            self.assertEqual(items[0]['date_basis'],'first_observed')
            self.assertEqual(items[0]['abstract'],'A satellite foundation model.')
            with patch.object(sources,'now_iso',return_value='2026-10-14T00:00:00Z'):
                again=sources.proceedings_watch(fetch,store,START,END,directions)[0]
            self.assertEqual(again[0]['published_at'],'2026-10-12T00:00:00Z')

    def test_diversity_shortlist_reserves_narrow_directions(self):
        papers=[]
        for number in range(40):
            item=sources.make_item('Remote paper %d'%number,'https://example.com/%d'%number)
            item.update(directions=['remote'],research_score=99)
            papers.append(item)
        narrow=sources.make_item('Rare noisy correspondence','https://example.com/rare')
        narrow.update(directions=['noise'],research_score=40)
        papers.append(narrow)
        result=codex_bridge.select_shortlist(papers,[],[{'id':'remote'},{'id':'noise'}])
        self.assertIn(narrow,result)
        self.assertEqual(len(result),24)

    def test_ai_shortlist_caps_full_week_and_balances_news(self):
        papers=[]
        for number in range(50):
            item=sources.make_item('Paper %d'%number,'https://example.com/p%d'%number)
            item.update(directions=['remote'],research_score=90-number)
            papers.append(item)
        news=[]
        for number in range(20):
            item=sources.make_item('News %d'%number,'https://example.com/n%d'%number,kind='news')
            item['importance_score']=90-number
            news.append(item)
        directions=[{'id':'remote'}]
        full=codex_bridge.select_shortlist(papers,news,directions)
        self.assertEqual(len(full),24)
        self.assertEqual(sum(item['kind']=='news' for item in full),6)
        measured=codex_bridge.select_shortlist(papers[:14],news[:10],directions)
        self.assertEqual(len(measured),24)
        self.assertEqual(sum(item['kind']=='news' for item in measured),10)
        retained=codex_bridge.select_shortlist(papers,[],directions,paper_limit=250,news_limit=0,total_limit=250)
        self.assertEqual(len(retained),50)


class LauncherSafety(unittest.TestCase):
    def test_occupied_port_falls_forward_without_touching_owner(self):
        class Server:
            server_port=8766
            def serve_forever(self): pass
            def server_close(self): pass
        calls=[]
        def factory(address,app):
            calls.append(address)
            if address[1]==8765: raise OSError(errno.EADDRINUSE,'in use')
            return Server()
        with patch.object(launch,'RadarApp',return_value=object()),patch.object(launch,'RadarServer',side_effect=factory),patch.object(sys,'argv',['launch.py','--no-browser']),patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(launch.main(),0)
        self.assertEqual(calls,[('127.0.0.1',8765),('127.0.0.1',8766)])

    def test_other_bind_failures_are_not_hidden(self):
        with patch.object(launch,'RadarApp',return_value=object()),patch.object(launch,'RadarServer',side_effect=OSError(errno.EACCES,'denied')),patch.object(sys,'argv',['launch.py','--no-browser']):
            with self.assertRaises(OSError): launch.main()

if __name__=='__main__': unittest.main()
