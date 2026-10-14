import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_bridge as bridge
from sources import make_item


class ModelTrustBoundary(unittest.TestCase):
    def setUp(self):
        self.item = make_item('Trusted title', 'https://arxiv.org/abs/2610.12345', published_at='2026-10-12')
        self.valid = {'summary': '基于摘要的总结', 'items': [{'id': self.item['id'], 'summary': '基于摘要', 'why_it_matters': '关联', 'directions': ['noisy_labels'], 'score': 80, 'related_work': [], 'suggested_action': '核验实验'}]}

    def test_rejects_unknown_ids_duplicates_and_forged_facts(self):
        cases = []
        unknown = copy.deepcopy(self.valid); unknown['items'][0]['id'] = 'invented'; cases.append(unknown)
        forged = copy.deepcopy(self.valid); forged['items'][0]['url'] = 'https://evil.example/'; cases.append(forged)
        title = copy.deepcopy(self.valid); title['items'][0]['title'] = 'Invented'; cases.append(title)
        duplicate = copy.deepcopy(self.valid); duplicate['items'] *= 2; cases.append(duplicate)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bridge.validate_enrichment(payload, [self.item], ['noisy_labels'])
        self.assertEqual(self.item['title'], 'Trusted title')
        self.assertEqual(self.item['url'], 'https://arxiv.org/abs/2610.12345')

    def test_rejects_malformed_types_and_empty_analysis(self):
        for payload in [[], {}, {'summary': 'x', 'items': []}, {'summary': 42, 'items': []}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bridge.validate_enrichment(payload, [self.item], ['noisy_labels'])
        invalid = copy.deepcopy(self.valid); invalid['items'][0]['score'] = True
        with self.assertRaises(ValueError):
            bridge.validate_enrichment(invalid, [self.item], ['noisy_labels'])

    def test_valid_analysis_references_only_allowed_candidate(self):
        summary, updates = bridge.validate_enrichment(self.valid, [self.item], ['noisy_labels'])
        self.assertEqual(summary, self.valid['summary'])
        self.assertEqual(list(updates), [self.item['id']])

    def test_subprocess_has_bounded_readonly_noninteractive_options(self):
        command = bridge.build_command('/absolute/codex', 'gpt-6.1-sol', '/tmp/task', '/tmp/schema', '/tmp/output')
        for option in ['--ephemeral', '--ignore-user-config', '--output-schema', '--output-last-message']:
            self.assertIn(option, command)
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
        with self.assertRaises(ValueError):
            bridge.build_command('/absolute/codex', 'gpt;evil', '.', 's', 'o')


if __name__ == '__main__':
    unittest.main()
