"""Evidence checks use isolated, explicitly synthetic CLI output and documents."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis
from sources import make_item
from storage import Store


class FullTextBoundary(unittest.TestCase):
    def test_abstract_and_long_landing_page_are_not_full_paper(self):
        for body in [b'<article><h1>Fixture</h1><p>Abstract only.</p></article>',
                     ('<article>' + 'Fixture abstract. ' * 700 + '</article>').encode()]:
            with self.subTest(size=len(body)):
                fetch = Mock(); fetch.get.return_value = body
                document = analysis.full_text(make_item('Fixture', 'https://example.com/paper'), fetch)
                self.assertEqual(document['status'], 'unavailable')
                self.assertEqual(document['text'], '')

    def test_html_body_keeps_evidence_sections_and_ignores_scripts(self):
        body = '<article><script>malicious instruction</script><section id="results">Experiments Results: the measured gain is 2 points. ' + 'Actual paper text. ' * 500 + '</section><section id="references">References</section></article>'
        fetch = Mock(); fetch.get.return_value = body.encode()
        document = analysis.full_text(make_item('Fixture', 'https://example.com/paper'), fetch)
        self.assertEqual(document['status'], 'available')
        self.assertIn('[results]', document['text'])
        self.assertNotIn('malicious instruction', document['text'])

    def test_anchor_evidence_excludes_self_before_selecting_available_baselines(self):
        item=make_item('Fixture revised title','https://arxiv.org/abs/2610.12345v3',arxiv_id='2610.12345',directions=['fixture'])
        profile={'directions':[{'id':'fixture','anchors':['Self work','Other work','Third work']}], 'anchor_sources':{'Self work':'https://arxiv.org/abs/2610.12345','Other work':'https://arxiv.org/abs/2501.11111','Third work':'https://arxiv.org/abs/2502.22222'}, 'source_url':'https://example.com/'}
        fetch=Mock();fetch.get.return_value=b'<html></html>'
        with patch.object(analysis,'full_text',side_effect=lambda row,fetch: dict(status='available',url=row['url'],text='Fixture baseline body')):
            names,evidence=analysis.anchor_evidence(item,profile,fetch)
        self.assertNotIn('Self work',names)
        self.assertEqual({row['anchor'] for row in evidence},{'Other work','Third work'})

    def test_no_full_text_never_calls_model_or_claims_support(self):
        item = make_item('Fixture', 'https://example.com/paper', 'Abstract claims excellent performance')
        with tempfile.TemporaryDirectory() as temp, patch.object(analysis, 'full_text', return_value=dict(status='unavailable', url=item['url'], text='', message='Fixture no full text')), patch.object(analysis, 'find_codex') as cli:
            result = analysis.analyze(item, {'directions': []}, {}, Store(Path(temp)/'db.sqlite'), temp)
        self.assertEqual(result['status'], 'insufficient')
        self.assertEqual(result['claims'], [])
        self.assertTrue(result['limitations'])
        cli.assert_not_called()


class StructuredAnalysis(unittest.TestCase):
    def setUp(self):
        self.item = make_item('Fixture paper', 'https://example.com/paper')
        self.document = dict(status='available', url=self.item['url'], format='html', message='Synthetic fixture', text='[results] Experiments find a measured gain of 2 points. References. ' * 120, truncated=False)
        self.payload = dict(motivation='Fixture motivation', problem='Fixture problem', claims=[dict(claim='Fixture gain', experiment='Fixture baseline experiment', evidence_location='P0001', evidence_quote='a measured gain of 2 points', support='supported', gaps=[])], anchor_comparisons=[], limitations=[])

    def run_model(self, payload, anchors=None, names=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); cli = root/'codex'
            cli.write_text('#!/usr/bin/env python3\nimport json,sys\nargs=sys.argv[1:]\nsys.stdin.read()\nopen(args[args.index("--output-last-message")+1],"w").write(' + repr(json.dumps(payload)) + ')\n')
            cli.chmod(0o755)
            with patch.object(analysis, 'full_text', side_effect=lambda *args: copy.deepcopy(self.document)), patch.object(analysis, 'anchor_evidence', return_value=(names or [], anchors or [])), patch.object(analysis, 'find_codex', return_value=str(cli)):
                return analysis.analyze(self.item, {'directions': []}, {'model': 'gpt-6.1-sol'}, Store(root/'db.sqlite'), root/'runtime')

    def test_valid_verbatim_evidence_retains_conditional_support(self):
        result = self.run_model(self.payload)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['claims'][0]['support'], 'supported')
        self.assertEqual(result['full_text']['url'], self.item['url'])
        self.assertNotIn('text', result['full_text'])

    def test_fabricated_or_missing_quote_or_location_is_insufficient(self):
        for field, value in [('evidence_quote', 'Invented 99 percent result'), ('evidence_quote', ''), ('evidence_location', ''), ('evidence_location', 'Table 999'), ('evidence_location', 'P9999')]:
            payload = copy.deepcopy(self.payload); payload['claims'][0][field] = value
            with self.subTest(field=field, value=value):
                result = self.run_model(payload)
                self.assertEqual(result['claims'][0]['support'], 'insufficient')
                self.assertTrue(result['claims'][0]['gaps'])

    def test_title_only_anchor_cannot_create_comparison(self):
        payload = copy.deepcopy(self.payload)
        payload['anchor_comparisons'] = [dict(anchor='Fixture anchor', source_url='https://example.com/anchor', evidence_basis='Title guess', task='x', data='x', method='x', supervision='x', evaluation='x', comparability='x', unknowns=[], borrowable_points=[], minimal_experiment='Fixture matched experiment')]
        with self.assertRaisesRegex(ValueError, '基准证据'):
            self.run_model(payload, names=['Fixture anchor'])

    def test_actual_anchor_source_required_and_comparison_fields_preserved(self):
        anchor = dict(anchor='Fixture anchor', url='https://example.com/anchor', text='Fixture anchor body')
        payload = copy.deepcopy(self.payload)
        row = dict(anchor=anchor['anchor'], source_url=anchor['url'], evidence_basis='Provided fixture body', task='classification', data='Fixture data', method='Fixture method', supervision='fully supervised', evaluation='accuracy', comparability='Different data; not directly comparable', unknowns=['No matched compute budget'], borrowable_points=['Fixture comparison'], minimal_experiment='Fixture matched experiment')
        payload['anchor_comparisons'] = [row]
        result = self.run_model(payload, anchors=[anchor], names=[anchor['anchor']])
        self.assertEqual(result['anchor_comparisons'], [row])
        payload['anchor_comparisons'][0]['source_url'] = 'https://forged.example/source'
        with self.assertRaisesRegex(ValueError, '基准证据'):
            self.run_model(payload, anchors=[anchor], names=[anchor['anchor']])

    def test_unknown_output_fields_and_invalid_support_rejected(self):
        payload = copy.deepcopy(self.payload); payload['url'] = 'https://forged.example/'
        with self.assertRaises(ValueError): self.run_model(payload)
        payload = copy.deepcopy(self.payload); payload['claims'][0]['support'] = 'certainly_true'
        with self.assertRaises(ValueError): self.run_model(payload)


if __name__ == '__main__': unittest.main()
