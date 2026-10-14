import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sources
from storage import Store

START, END = '2026-10-12', '2026-10-14'


class DatesAndIdentity(unittest.TestCase):
    def test_shanghai_inclusive_day_boundaries(self):
        self.assertFalse(sources.in_window('2026-10-11T15:59:59Z', START, END))
        self.assertTrue(sources.in_window('2026-10-11T16:00:00Z', START, END))
        self.assertTrue(sources.in_window('2026-10-14T15:59:59Z', START, END))
        self.assertFalse(sources.in_window('2026-10-14T16:00:00Z', START, END))
        self.assertEqual(sources.utc_bounds(START, END), ('202610111600', '202610141559'))
        self.assertFalse(sources.in_window('invalid', START, END))

    def test_hf_feature_is_not_original_publication(self):
        item = sources.parse_hf([{'paper': {'id': '2401.12345v3', 'title': 'Old remote sensing model', 'publishedAt': '2024-01-01T00:00:00Z'}}], START)[0]
        self.assertEqual(item['date_basis'], 'hf_featured_day')
        self.assertEqual(item['published_at'], START)
        self.assertEqual(item['original_published_at'], '2024-01-01T00:00:00Z')
        self.assertEqual(item['arxiv_id'], '2401.12345')

    def test_arxiv_versions_merge_and_keep_provenance(self):
        one = sources.make_item('A satellite model', 'https://arxiv.org/abs/2610.12345v1', source='HF', date_basis='hf_featured_day')
        two = sources.make_item('A satellite model revised', 'https://arxiv.org/abs/2610.12345v2', source='arXiv', published_at=START)
        merged = sources.deduplicate([one, two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]['sources']), 2)
        self.assertEqual(merged[0]['date_basis'], 'original_publication')
        self.assertEqual(sources.normalize_arxiv_id('https://arxiv.org/pdf/2610.12345v2.pdf'), '2610.12345')

    def test_arxiv_update_keeps_original_date(self):
        body = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2601.12345v2</id><title>Multimodal hallucination</title><published>2026-01-01T00:00:00Z</published><updated>2026-10-12T08:00:00Z</updated><summary>Grounded model</summary></entry></feed>'''
        item = sources.parse_arxiv(body, START, END)[0]
        self.assertEqual(item['date_basis'], 'updated_this_week')
        self.assertEqual(item['original_published_at'], '2026-01-01T00:00:00Z')
        self.assertIn('未经同行评审', item['status'])


class OpenReviewProvenance(unittest.TestCase):
    def note(self, **kwargs):
        note = dict(id='paper1', forum='paper1', content={'title': {'value': 'Noisy correspondence learning'}, 'abstract': {'value': 'Robust cross-modal learning'}, 'venue': {'value': 'Submitted to ICLR 2027'}})
        note.update(kwargs)
        return note

    def stamp(self, iso):
        return int(sources.parse_date(iso).timestamp() * 1000)

    def test_public_release_date_includes_old_created_note(self):
        item = sources.parse_openreview([self.note(cdate=self.stamp('2026-09-01'), odate=self.stamp(START))], START, END, 'ICLR 2027')
        self.assertEqual(len(item), 1)
        self.assertNotIn('已录用', item[0]['status'])
        self.assertNotEqual(item[0]['published_at'][:10], '2026-09-01')

    def test_creation_and_acceptance_dates_do_not_imply_new_public_submission(self):
        self.assertEqual(sources.parse_openreview([self.note(cdate=self.stamp(START))], START, END, 'ICLR'), [])
        self.assertEqual(sources.parse_openreview([self.note(pdate=self.stamp(START))], START, END, 'ICLR'), [])

    def test_replies_are_not_papers(self):
        note = self.note(id='reply1', forum='paper1', replyto='paper1', odate=self.stamp(START))
        self.assertEqual(sources.parse_openreview([note], START, END, 'ICLR'), [])


class SourceCoverage(unittest.TestCase):
    def test_first_proceedings_run_baselines_history(self):
        class Fetch:
            def get(self, url, **kwargs):
                return b'<a href="https://proceedings.mlr.press/v267/">ICML historic volume</a>'
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'db.sqlite')
            items, confs, warnings = sources.proceedings_watch(Fetch(), store, START, END, [])
            self.assertEqual(items, [])
            self.assertTrue(all(row['baseline'] for row in confs))
            self.assertTrue(all(not row['new_volumes'] for row in confs))

    def test_feed_error_distinct_from_successful_empty(self):
        class Fetch:
            def get(self, url, **kwargs):
                if 'failed' in url:
                    raise OSError('fixture connection failure')
                return b'<rss><channel></channel></rss>'
        feeds = [('empty', 'Empty', 'https://example.com/empty'), ('failed', 'Failed', 'https://example.com/failed')]
        with patch.object(sources, 'NEWS_FEEDS', feeds):
            items, states = sources.collect_news(Fetch(), START, END)
        self.assertEqual(items, [])
        states = {row['id']: row for row in states}
        self.assertEqual(states['empty']['status'], 'ok')
        self.assertEqual(states['failed']['status'], 'error')
        self.assertIn('fixture connection failure', states['failed']['message'])

    def test_noise_directions_and_independent_news_importance(self):
        profile = json.loads((Path(__file__).resolve().parents[1] / 'config/profile.json').read_text())
        item = sources.make_item('Rethinking noisy correspondence and label noise', 'https://example.com/paper', 'Robust multimodal cross-modal correspondence')
        sources.score_item(item, profile['directions'])
        self.assertIn('noisy_labels', item['directions'])
        self.assertGreater(item['research_score'], 30)
        news = sources.make_item('New GPU inference training platform launch', 'https://example.com/news', kind='news')
        sources.score_item(news, profile['directions'])
        self.assertGreater(news['importance_score'], 25)

    def test_private_and_credential_source_urls_rejected(self):
        for url in ['http://example.com/feed', 'https://user:secret@example.com/feed', 'https://127.0.0.1/feed', 'https://[::1]/feed', 'https://10.0.0.1/feed', 'https://localhost/feed', 'https://example.com:8443/feed', 'file:///tmp/x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                sources.validate_public_url(url, resolve=False)

    def test_dns_private_address_rejected(self):
        with patch.object(sources.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.2', 443))]):
            with self.assertRaises(ValueError):
                sources.validate_public_url('https://public-looking.example/feed')

    def test_unparseable_html_does_not_claim_no_news(self):
        class Fetch:
            def get(self, url, **kwargs):
                return b'<html><a href="/news">Latest announcements without dates</a></html>'
        with patch.object(sources, 'NEWS_FEEDS', [('dynamic', 'Dynamic', 'https://example.com/')]):
            _, states = sources.collect_news(Fetch(), START, END)
        self.assertEqual(states[0]['status'], 'partial')


class Persistence(unittest.TestCase):
    def test_restart_preserves_unicode_and_source_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'db.sqlite'
            store = Store(db)
            report = dict(id='r1', created_at='2026-10-14T00:00:00Z', title='遥感 <script> & 噪声', sources=[{'url': 'https://example.com/?a=1&b=2'}])
            store.save_report(report)
            store.set('settings', {'model': 'example'})
            restored = Store(db)
            self.assertEqual(restored.report('r1'), report)
            self.assertEqual(restored.reports()[0], report)
            self.assertEqual(restored.get('settings')['model'], 'example')


if __name__ == '__main__':
    unittest.main()
