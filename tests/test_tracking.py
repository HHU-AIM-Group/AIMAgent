"""Deterministic monitoring regressions; synthetic snapshots stay in temporary DBs."""
import base64
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tracking
from sources import make_item
from storage import Store

REPO = 'https://github.com/fixture/research'


class FixtureFetch:
    def __init__(self):
        self.sha = 'commit-one'; self.release = 'v1'; self.updated = '2026-10-12'
        self.files = ['README.md', 'train.py']; self.readme = ''; self.failed = False
        self.links = ''
    def get(self, url, **kwargs):
        if 'export.arxiv.org' in url:
            return b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2610.12345v2</id></entry></feed>'
        return ('<html><a href="'+REPO+'">Code</a>'+self.links+'</html>').encode()
    def json(self, url, **kwargs):
        if self.failed: raise OSError('Fixture temporary GitHub outage')
        if '/commits/' in url: return {'sha':self.sha}
        if '/releases?' in url: return [{'tag_name':self.release,'html_url':REPO+'/releases/tag/'+self.release}]
        if '/git/trees/' in url: return {'tree':[{'type':'blob','path':name} for name in self.files]}
        if url.endswith('/readme'): return {'content':base64.b64encode(self.readme.encode()).decode()}
        return {'default_branch':'main','updated_at':self.updated}


class PersistentTracking(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root/'db.sqlite'); self.fetch = FixtureFetch()
        self.item = make_item('Fixture paper', 'https://arxiv.org/abs/2610.12345v1', arxiv_id='2610.12345')
        tracking.track_item(self.store, self.item, repositories=[REPO], confirmed=True)
    def tearDown(self): self.temp.cleanup()
    def check(self): return tracking.check_tracked(self.store,self.fetch)

    def test_first_success_is_baseline_restart_and_second_check_no_changes(self):
        state = self.check()
        self.assertEqual(state['changes'], [])
        self.assertEqual(state['items'][0]['snapshot']['arxiv_version'],'v2')
        self.assertEqual(state['items'][0]['snapshot']['code']['status'],'available')
        restored = Store(self.root/'db.sqlite')
        state = tracking.check_tracked(restored,self.fetch)
        self.assertEqual(state['changes'], [])
        self.assertTrue(state['items'][0]['last_checked_at'])

    def test_commit_and_release_changes_retained_without_repeat_notifications(self):
        self.check(); self.fetch.sha = 'commit-two'; self.fetch.release = 'v2'
        state = self.check()
        self.assertEqual({r['field'] for r in state['changes']},{'repository.commit_sha','repository.release'})
        before = copy.deepcopy(state['changes'])
        self.assertEqual(self.check()['changes'],before)
        self.assertEqual(Store(self.root/'db.sqlite').get('tracking:changes'),before)

    def test_metadata_updated_at_is_not_code_update(self):
        self.check(); self.fetch.updated='2026-10-22'
        self.assertEqual(self.check()['changes'],[])

    def test_readme_only_repository_is_placeholder(self):
        self.fetch.files=['README.md','LICENSE','docs/example.py','.github/workflow.js']
        state=self.check()
        self.assertEqual(state['items'][0]['snapshot']['code']['status'],'placeholder')
        self.assertEqual(state['items'][0]['snapshot']['weights']['status'],'unknown')

    def test_linked_weights_and_data_changes_have_history(self):
        self.fetch.readme='Weights: https://huggingface.co/fixture/model-v1\nData: https://huggingface.co/datasets/fixture/data-v1'
        self.check()
        self.fetch.readme='Weights: https://huggingface.co/fixture/model\nData: https://huggingface.co/datasets/fixture/data'
        state=self.check(); snapshot=state['items'][0]['snapshot']
        self.assertEqual(snapshot['weights']['status'],'available')
        self.assertEqual(snapshot['data']['status'],'available')
        self.assertEqual({row['field'] for row in state['changes']},{'weights','data'})

    def test_temporary_failure_preserves_known_status_and_snapshot(self):
        state=self.check(); before=copy.deepcopy(state['items'][0]['snapshot'])
        self.fetch.failed=True; state=self.check()
        self.assertEqual(state['items'][0]['snapshot'],before)
        self.assertEqual(state['changes'],[])
        self.assertTrue(state['items'][0]['errors'])

    def test_first_failure_then_recovery_is_successful_baseline(self):
        self.fetch.failed=True; first=self.check()
        self.assertTrue(first['items'][0]['errors'])
        self.fetch.failed=False; state=self.check()
        self.assertEqual(state['changes'],[])
        self.assertEqual(state['items'][0]['snapshot']['code']['status'],'available')

    def test_same_arxiv_across_week_title_revision_is_one_target(self):
        self.check()
        revised=make_item('Fixture paper revised title','https://arxiv.org/abs/2610.12345v3',arxiv_id='2610.12345')
        tracking.track_item(self.store,revised,repositories=[REPO],confirmed=True)
        state=tracking.tracking_state(self.store)
        self.assertEqual(len(state['items']),1)
        self.assertIsNotNone(state['items'][0]['snapshot'])

    def test_replacing_or_removing_repository_rebuilds_baseline(self):
        self.check()
        tracking.track_item(self.store,self.item,repositories=[],confirmed=True)
        state=self.check()
        self.assertEqual(state['changes'],[])
        self.assertEqual(state['items'][0]['snapshot']['repositories'],[])
        self.assertNotEqual(state['items'][0]['snapshot']['code']['status'],'available')
        tracking.track_item(self.store,self.item,repositories=[REPO],confirmed=True)
        self.fetch.sha='different-configured-baseline'
        self.assertEqual(self.check()['changes'],[])

    def test_similar_huggingface_hostname_is_not_artifact_evidence(self):
        self.fetch.readme='https://huggingface.co.evil.example/fixture/weights'
        self.fetch.links='<a href="https://evil.example/huggingface.co/datasets/fixture">data</a>'
        snapshot=self.check()['items'][0]['snapshot']
        self.assertEqual(snapshot['weights']['status'],'unknown')
        self.assertEqual(snapshot['data']['status'],'unknown')

    def test_resource_message_change_is_not_new_release(self):
        self.fetch.readme='https://huggingface.co/fixture/model'
        self.check()
        self.fetch.links='<a href="https://huggingface.co/fixture/model">weights</a>'
        self.assertEqual(self.check()['changes'],[])

    def test_target_limit_is_explicit_instead_of_silently_unchecked(self):
        for number in range(49):
            tracking.track_item(self.store,make_item('Fixture target '+str(number),'https://example.com/'+str(number)))
        with self.assertRaisesRegex(ValueError,'50'):
            tracking.track_item(self.store,make_item('Fixture 51st target','https://example.com/last'))
        self.assertEqual(len(tracking.tracking_state(self.store)['items']),50)

    def test_new_candidate_link_notifies_after_successful_discovery_baseline(self):
        tracking.track_item(self.store,self.item,repositories=[],confirmed=False)
        first=self.check()
        self.assertEqual(first['changes'],[])
        self.fetch.links='<a href="https://github.com/fixture/new-research">new code candidate</a>'
        state=self.check()
        changes=[row for row in state['changes'] if row['field']=='candidate_repository']
        self.assertEqual(len(changes),1)
        self.assertIn('https://github.com/fixture/new-research',str(changes[0]['after']))
        self.assertNotEqual(state['items'][0]['snapshot']['code']['status'],'available')
        self.assertEqual(self.check()['changes'],state['changes'])

    def test_candidate_discovery_failure_recovery_is_baseline_not_new_code(self):
        tracking.track_item(self.store,self.item,repositories=[],confirmed=False)
        self.check()
        with patch.object(self.fetch,'get',side_effect=OSError('Fixture page failure')):
            self.assertEqual(self.check()['changes'],[])
        self.fetch.links='<a href="https://github.com/fixture/recovered">newly accessible candidate</a>'
        self.assertEqual(self.check()['changes'],[])
        self.assertNotEqual(self.check()['items'][0]['snapshot']['code']['status'],'available')

    def test_bad_repository_urls_rejected(self):
        for url in ['https://127.0.0.1/repo','https://github.com/fixture/research/issues','https://user:secret@github.com/fixture/research','https://evil.example/fixture/research','javascript:alert(1)']:
            with self.subTest(url=url),self.assertRaises(ValueError):
                tracking.track_item(self.store,self.item,repositories=[url])


if __name__=='__main__': unittest.main()
