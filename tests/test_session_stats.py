"""Focused accounting tests: streaming deduplication, request scope and cache overlap."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPTS = Path(__file__).parents[1] / 'plugins/you-are-here/skills/you-are-here/scripts'

def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

stats = module('session_stats')
pmap = module('pmap')


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(x) + '\n' for x in data))


def response(id, usage, text='text'):
    return {'type': 'assistant', 'timestamp': '2026-09-24T00:01:00Z',
            'message': {'id': id, 'model': 'test-model', 'usage': usage,
                        'content': [{'type': 'text', 'text': text}]}}


class SessionStatsTest(unittest.TestCase):
    def test_map_collection_binds_usage_to_the_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, run = Path(tmp) / 's.jsonl', Path(tmp) / 'r.jsonl'
            put(source, [response('one', {'input_tokens': 100, 'output_tokens': 10})])
            put(run, [
                {'type': 'event_msg', 'timestamp': '2026-10-13T00:00:00.500Z', 'payload': {'type': 'task_started', 'turn_id': 'x'}},
                {'type': 'token_usage_record', 'payload': {'turn_id': 'x', 'response_id': 'r1',
                 'usage': {'input_tokens': 220, 'output_tokens': 2}}},
                {'type': 'event_msg', 'timestamp': '2026-10-13T00:01:00Z', 'payload': {'type': 'task_complete', 'turn_id': 'x'}},
            ])
            report = {'original': stats.claude_session(source), 'skill_run': stats.codex_turn(run, 'x')}
            # Facts currently record whole seconds; timezone offsets describe the same instant.
            current = pmap.render_analysis(report, 'zh', collected_at='2026-10-13T08:00:00+08:00')
            preview = current.split('</summary>')[0]
            self.assertIn('222', preview)
            self.assertIn('10-13', preview)
            for stamp in ('2026-10-24T00:00:00Z', None):
                historical = pmap.render_analysis(report, 'zh', collected_at=stamp)
                preview = historical.split('</summary>')[0]
                self.assertIn('110', preview)  # original conversation remains available
                self.assertIn('未采集', preview)
                self.assertNotIn('222', preview)
                self.assertIn('222', historical)  # prior invocation is explicitly retained in details
            self.assertTrue(report['skill_run']['usage_available'])  # rendering never mutates the report

    def test_claude_stream_and_child_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            usage = {'input_tokens': 10, 'cache_read_input_tokens': 20,
                     'cache_creation_input_tokens': 30, 'output_tokens': 4}
            final = {**usage, 'output_tokens': 9, 'output_tokens_details': {'thinking_tokens': 3}}
            put(path, [response('one', usage), response('one', final),
                       {'type': 'user', 'timestamp': '2026-09-24T00:00:00Z', 'message': {'content': 'please build'}},
                       {'type': 'user', 'isMeta': True, 'message': {'content': 'skill injection'}},
                       {'type': 'user', 'message': {'content': '<task-notification>done</task-notification>'}}])
            put(path.with_suffix('') / 'subagents/agent-child.jsonl', [response('two', usage)])
            result = stats.claude_session(path)
            self.assertEqual(result['tokens']['total'], 133)
            self.assertEqual(result['tokens']['cache_read'], 40)
            self.assertEqual(result['tokens']['reasoning'], 3)
            self.assertEqual(result['requests'], 2)
            self.assertEqual(result['duplicate_usage_rows'], 1)
            self.assertEqual(result['subagents'], 1)
            self.assertNotIn('please build', json.dumps(result))

    def test_codex_turn_scope_cache_and_cumulative_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run.jsonl'
            usage = {'input_tokens': 100, 'cached_input_tokens': 60, 'cache_write_input_tokens': 20,
                     'output_tokens': 10, 'reasoning_output_tokens': 4, 'total_tokens': 110}
            request = {'type': 'token_usage_record', 'payload': {'turn_id': 'selected', 'response_id': 'r1',
                       'usage': usage, 'turn_token_usage': {'total_tokens': 110}}}
            events = [
                {'type': 'event_msg', 'timestamp': '2026-10-13T00:00:00Z', 'payload': {'type': 'task_started', 'turn_id': 'selected'}},
                {'type': 'turn_context', 'payload': {'turn_id': 'selected', 'model': 'test-codex'}},
                request, request,
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': 999999}}}},
                {'type': 'event_msg', 'timestamp': '2026-10-13T00:01:00Z', 'payload': {'type': 'task_complete', 'turn_id': 'selected', 'duration_ms': 60000}},
                {'type': 'token_usage_record', 'payload': {'turn_id': 'other', 'response_id': 'r2', 'usage': usage}},
            ]
            put(path, events)
            result = stats.codex_turn(path, 'selected')
            self.assertEqual(result['tokens']['total'], 110)
            self.assertEqual(result['tokens']['input'], 20)
            self.assertEqual(result['tokens']['output'], 10)
            self.assertEqual(result['requests'], 1)
            self.assertEqual(result['duplicate_usage_rows'], 1)
            self.assertEqual(result['ignored_cumulative_rows'], 1)
            self.assertTrue(result['counter_matches'])
            self.assertTrue(result['complete'])
            self.assertEqual(result['end'], '2026-10-13T00:01:00Z')
            with self.assertRaises(ValueError):
                stats.codex_turn(path, 'missing')

    def test_cumulative_only_is_unavailable_not_zero_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run.jsonl'
            put(path, [{'type': 'event_msg', 'timestamp': '2026-10-13T00:00:00Z', 'payload': {'type': 'task_started', 'turn_id': 'x'}},
                       {'type': 'event_msg', 'timestamp': '2026-10-13T00:00:01Z', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': 10}}}}])
            self.assertFalse(stats.codex_turn(path, 'x')['usage_available'])
            path.write_text('{broken\n')
            with self.assertRaises(ValueError):
                list(stats.rows(path))

    def test_usage_rendering_escapes_transcript_derived_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, run = Path(tmp) / 's.jsonl', Path(tmp) / 'r.jsonl'
            put(source, [response('one', {'input_tokens': 100, 'output_tokens': 10})])
            put(run, [{'type': 'event_msg', 'timestamp': '2026-10-13T00:00:00Z', 'payload': {'type': 'task_started', 'turn_id': 'x'}}])
            report = {'original': stats.claude_session(source), 'skill_run': stats.codex_turn(run, 'x'), 'generated_at': 'now',
                      'notes': ['<img src=x onerror=bad()>']}
            report['original']['models'][0]['name'] = '<script>bad()</script>'
            html = pmap.render_analysis(report, 'zh')
            self.assertNotIn('<script>', html)
            self.assertIn('&lt;script&gt;', html)
            self.assertIn('未采集', html)
            self.assertIn('session-usage', html)
            self.assertEqual(pmap.render_analysis(None, 'zh'), '')


if __name__ == '__main__':
    unittest.main()
