"""Offline regression checks; never call an AI provider or spend API quota."""
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


class RequestException(Exception):
    pass


class Timeout(RequestException):
    pass


class HTTPError(RequestException):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


class Response:
    def __init__(self, content=b'<tv/>', status=200, text='', headers=None, payload=None):
        self.content, self.status_code = content, status
        self.ok, self.text, self.headers = status < 400, text, headers or {}
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if not self.ok:
            raise HTTPError(self.text, response=self)

    def json(self):
        return self.payload


def groq_result(items):
    return Response(payload={'choices': [{'message': {'content': json.dumps(items)}}],
                             'usage': {'total_tokens': 123}})


class AIProcessingTests(unittest.TestCase):
    def setUp(self):
        self.requests = types.ModuleType('requests')
        self.requests.post, self.requests.get = Mock(), Mock()
        self.requests.HTTPError = HTTPError
        self.requests.RequestException = RequestException
        self.requests.exceptions = types.SimpleNamespace(
            RequestException=RequestException, Timeout=Timeout, ConnectionError=ConnectionError)
        self.client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=Mock()))
        google = types.ModuleType('google')
        genai = types.ModuleType('google.genai')
        genai.Client = lambda **kwargs: self.client
        config = types.ModuleType('google.genai.types')
        config.GenerateContentConfig = lambda **kwargs: kwargs
        config.Tool = lambda **kwargs: kwargs
        config.GoogleSearch = lambda: None
        google.genai, genai.types = genai, config
        with patch.dict(sys.modules, {'requests': self.requests, 'google': google,
                                      'google.genai': genai, 'google.genai.types': config}):
            self.ns = runpy.run_path(str(Path(__file__).with_name('clean_epg.py')))
        # runpy returns a copy; use the functions' actual global dictionary.
        self.ns = self.ns['process_epg'].__globals__
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_retry_after_survives_next_run(self):
        state = self.ns['AIProviderState']({})
        state.record_failure('groq', HTTPError('429', Response(status=429, headers={'retry-after': '120'})))
        retry = self.ns['datetime'].fromisoformat(state.cooldowns['groq'])
        seconds = (retry - self.ns['datetime'].now(self.ns['UTC'])).total_seconds()
        self.assertTrue(118 < seconds <= 120)
        self.assertFalse(self.ns['AIProviderState'](state.cooldowns).available(None, 'key'))

    def test_error_message_retry_and_timeout(self):
        state = self.ns['AIProviderState']()
        state.record_failure('groq', HTTPError('HTTP 429: Please try again in 37m6.528s.'))
        retry = self.ns['datetime'].fromisoformat(state.cooldowns['groq'])
        self.assertAlmostEqual((retry - self.ns['datetime'].now(self.ns['UTC'])).total_seconds(), 2226.528, delta=2)
        state.record_failure('groq', Timeout('read timeout'))
        retry = self.ns['datetime'].fromisoformat(state.cooldowns['groq'])
        self.assertAlmostEqual((retry - self.ns['datetime'].now(self.ns['UTC'])).total_seconds(), 300, delta=2)

    def test_short_provider_backoff_retries_once(self):
        state = self.ns['AIProviderState']()
        self.requests.post.side_effect = [
            Response(status=429, text='short rate limit', headers={'retry-after': '2'}),
            groq_result([{'key': 'e0', 'description': 'Recovered'}]),
        ]
        with patch.object(self.ns['time'], 'sleep') as sleep:
            result = self.ns['_groq_generate_text']('prompt', 'key', state)
        self.assertIn('Recovered', result)
        sleep.assert_called_once()
        self.assertLessEqual(sleep.call_args.args[0], 60)
        self.assertEqual(self.requests.post.call_count, 2)
        self.assertNotIn('groq', state.exhausted)

    def test_short_backoff_does_not_loop(self):
        state = self.ns['AIProviderState']()
        self.requests.post.return_value = Response(status=429, text='still limited', headers={'retry-after': '2'})
        with patch.object(self.ns['time'], 'sleep'):
            with self.assertRaises(HTTPError):
                self.ns['_groq_generate_text']('prompt', 'key', state)
        self.assertEqual(self.requests.post.call_count, 2)
        self.assertIn('groq', state.exhausted)

    def test_partial_title_batch_is_kept(self):
        self.client.models.generate_content.return_value = types.SimpleNamespace(text=json.dumps([
            {'raw': 'A', 'clean_title': 'A clean', 'description': 'A description'}]))
        self.requests.post.return_value = groq_result([
            {'raw': 'B', 'clean_title': 'B clean', 'description': 'B description'}])
        result = self.ns['ai_clean_sport_titles'](['A', 'B'], self.client, 'key')
        self.assertEqual(set(result), {'A', 'B'})
        payload = self.requests.post.call_args.kwargs['json']
        self.assertEqual(json.loads(payload['messages'][0]['content'].split('Input titles (JSON):\n')[1]), ['B'])
        self.assertEqual(payload['reasoning_effort'], 'low')

    def test_compact_keys_partial_descriptions_and_usage(self):
        events = [{'key': 'long-internal-key-A', 'title': 'A', 'context': 'today'},
                  {'key': 'long-internal-key-B', 'title': 'B', 'context': 'today'}]
        self.client.models.generate_content.return_value = types.SimpleNamespace(text='[{"key":"e0","description":"A description"},{"key":"e1","description":" "}]')
        self.requests.post.return_value = groq_result([{'key': 'e0', 'description': 'B description'}])
        state = self.ns['AIProviderState']()
        result = self.ns['ai_describe_events'](events, self.client, 'key', state)
        self.assertEqual(result, {'long-internal-key-A': 'A description', 'long-internal-key-B': 'B description'})
        prompt = self.requests.post.call_args.kwargs['json']['messages'][0]['content']
        self.assertNotIn('long-internal-key', prompt)
        self.assertEqual(state.groq_tokens, 123)

    def prepare_process(self, labels):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        for constant, name in [('AI_CACHE_FILE', 'ai_cache.json'), ('OUTPUT_GZ_FILE', 'guide.gz'),
                                ('FOOTBALL_MIRROR_IDS_FILE', 'football.txt'), ('EVENT_MIRROR_IDS_FILE', 'events.txt')]:
            self.ns[constant] = folder / name
        xml = '<tv>' + ''.join(f'<channel id="c{i}"><display-name>{label}</display-name></channel>' for i, label in enumerate(labels)) + '</tv>'
        self.requests.get.return_value = Response(content=xml.encode())
        return folder

    def test_unattempted_items_are_not_failure_cached(self):
        today = self.ns['datetime'].now(self.ns['EASTERN']).strftime('%b %d')
        folder = self.prepare_process([f'ESPN+ {i:02d}: Event {i} @ {today} 7:00PM ET' for i in range(1, 8)])
        self.client.models.generate_content.side_effect = RuntimeError('429 RESOURCE_EXHAUSTED')
        self.requests.post.return_value = Response(status=429, text='quota', headers={'retry-after': '120'})
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'key', 'GROQ_API_KEY': 'key'}):
            self.ns['process_epg']()
            cache = json.loads((folder / 'ai_cache.json').read_text())
            self.assertEqual(sum(bool(x.get('failed')) for x in cache['entries'].values()), 5)
            self.assertTrue((folder / 'guide.gz').exists())
            self.ns['process_epg']()
        self.assertEqual(self.requests.post.call_count, 1)
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.assertIn('2 unattempted and still pending', self.output.getvalue())

    def test_same_fixture_languages_share_description_and_cache(self):
        today = self.ns['datetime'].now(self.ns['EASTERN']).strftime('%b %d')
        folder = self.prepare_process([f'ESPN+ {i:02d}: A vs B ({language}) @ {today} 7:00PM ET' for i, language in [(1, 'English'), (2, 'Spanish')]])
        self.client.models.generate_content.return_value = types.SimpleNamespace(text='[{"key":"e0","description":"Verified fixture description"}]')
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'key', 'GROQ_API_KEY': 'key'}):
            self.ns['process_epg']()
            xml = gzip.decompress((folder / 'guide.gz').read_bytes()).decode()
            self.assertGreaterEqual(xml.count('Verified fixture description'), 2)
            self.ns['process_epg']()
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.requests.post.assert_not_called()
        self.assertIn('1 newly generated', self.output.getvalue())

    def test_dazn_description_does_not_trigger_second_title_request(self):
        today = self.ns['datetime'].now(self.ns['LONDON']).strftime('%d %b')
        self.prepare_process([f'DAZN UK 01: A vs B @ {today} 07:00 PM London'])
        self.client.models.generate_content.return_value = types.SimpleNamespace(text='[{"key":"e0","description":"Verified fixture description"}]')
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'key', 'GROQ_API_KEY': 'key'}):
            self.ns['process_epg']()
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.requests.post.assert_not_called()

    def test_migration_keeps_success_and_removes_blanket_failures(self):
        now = self.ns['datetime'].now(self.ns['UTC'])
        cache = {'entries': {'success': {'result': 'Keep me', 'created_at': now.isoformat()},
                             'legacy-failure': {'failed': True, 'retry_after': (now + self.ns['timedelta'](hours=6)).isoformat()}}}
        self.ns['_prune_ai_cache'](cache)
        self.assertEqual(set(cache['entries']), {'success'})


if __name__ == '__main__':
    unittest.main()
