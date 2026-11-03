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

    def test_sky_sports_plus_london_time_handles_summer_and_winter(self):
        parse = self.ns['_parse_mirrored_start']
        date = self.ns['datetime']
        summer = parse('Sky Sports + 01: Tennis A v B @ 1 Jul 06:30 PM London', date(2026, 7, 1).date())
        winter = parse('Sky Sports + 01: Tennis A v B @ 1 Nov 06:30 PM London', date(2026, 11, 1).date())
        self.assertEqual(summer.hour, 17)
        self.assertEqual(winter.hour, 18)
        self.assertEqual(self.ns['_clean_mirrored_event_title']('Sky Sports + 09: NFL Ravens @ Bills @ 1 Nov 05:25 PM London'), 'NFL Ravens @ Bills')

    def test_sky_sports_plus_event_schedule_and_manifest(self):
        import xml.etree.ElementTree as ET
        labels = [
            'Sky Sports + 01: Tennis A v B @ 1 Nov 06:30 PM London',
            'Sky Sports + 02: Tennis C v D @ 1 Nov 10:00 AM London',
            'Sky Sports + 03: NO EVENT',
            'Sky Sports + 04: Old fixture @ 31 Oct 06:30 PM London',
            'Sky Sports+',
        ]
        folder = self.prepare_process(labels)
        (folder / 'events.txt').write_text('c0\nc1\nc2\nc3\n')
        root = ET.fromstring(self.requests.get.return_value.content)
        for i, label in enumerate(labels):
            programme = ET.SubElement(root, 'programme', channel=f'c{i}', start='20261101000000 +0000', stop='20261102000000 +0000')
            ET.SubElement(programme, 'title').text = label if i < 4 else 'Original linear schedule'
        self.requests.get.return_value.content = ET.tostring(root)
        real_datetime = self.ns['datetime']
        utc = self.ns['UTC']
        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 1, 12, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        self.assertEqual(result.find("channel[@id='c0']/display-name").text, 'Sky Sports+ 01')
        upcoming = result.find("programme[@channel='c0']")
        self.assertEqual(upcoming.findtext('title'), 'Upcoming Event: Tennis A v B at 6.30pm')
        self.assertEqual(upcoming.get('stop'), '20261101183000 +0000')
        active = result.find("programme[@channel='c1']")
        self.assertEqual(active.findtext('title'), 'Tennis C v D')
        self.assertEqual(active.get('stop'), '20261101140000 +0000')
        self.assertEqual(result.find("programme[@channel='c2']/title").text, 'No event today')
        self.assertEqual(result.find("programme[@channel='c3']/title").text, 'No further event scheduled')
        self.assertEqual(result.find("programme[@channel='c4']/title").text, 'Original linear schedule')
        self.requests.post.assert_not_called()
        manifest = set(Path(__file__).with_name('event_mirror_channel_ids.txt').read_text().splitlines())
        self.assertTrue({f'dummy-{i}' for i in range(1412, 1472)} <= manifest)

    def test_fa_cup_pipe_labels_parse_fixture_and_empty_channels(self):
        parse = self.ns['_parse_european_league_label']
        today = self.ns['datetime'](2026, 5, 16).date()
        fixture = parse('FA Cup 01 | Chelsea 15:00 Manchester City 16/05', today)
        self.assertEqual(fixture['title'], 'Chelsea vs Manchester City')
        self.assertEqual(fixture['start_utc'].isoformat(), '2026-05-16T14:00:00+00:00')
        self.assertEqual(parse('FA Cup 02 |', today)['title'], '')
        self.assertEqual(parse('FA Cup 20 | NO EVENT', today)['title'], '')
        self.assertIsNone(parse('FA Cup 21 |', today))
        winter = parse('FA Cup 01 | Chelsea 15:00 Manchester City 01/11', self.ns['datetime'](2026, 11, 1).date())
        self.assertEqual(winter['start_utc'].hour, 15)

    def test_fa_cup_mirror_schedule_has_clean_match_names(self):
        import xml.etree.ElementTree as ET
        labels = [
            'FA Cup 01 | Chelsea 15:00 Manchester City 16/05',
            'FA Cup 02 |',
            'FA Cup 03 | Old Club 15:00 Other Club 15/05',
        ]
        folder = self.prepare_process(labels)
        root = ET.fromstring(self.requests.get.return_value.content)
        ids = [f'dummy-{i}' for i in range(12375, 12378)]
        for channel, channel_id, label in zip(root.findall('channel'), ids, labels):
            channel.set('id', channel_id)
            row = ET.SubElement(root, 'programme', channel=channel_id, start='20260516000000 +0000', stop='20260517000000 +0000')
            ET.SubElement(row, 'title').text = label
        self.requests.get.return_value.content = ET.tostring(root)
        (folder / 'football.txt').write_text('\n'.join(ids))
        real_datetime, utc = self.ns['datetime'], self.ns['UTC']
        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 5, 16, 12, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        programmes = result.findall("programme[@channel='dummy-12375']")
        self.assertEqual([p.findtext('title') for p in programmes], [
            'Upcoming Match: Chelsea vs Manchester City at 2pm',
            'Chelsea vs Manchester City', 'Match ended',
        ])
        self.assertEqual(programmes[1].get('start'), '20260516140000 +0000')
        self.assertEqual(programmes[1].get('stop'), '20260516163000 +0000')
        self.assertEqual(result.find("programme[@channel='dummy-12376']/title").text, 'No match today')
        self.assertEqual(result.find("programme[@channel='dummy-12377']/title").text, 'No match today')
        manifest = set(Path(__file__).with_name('football_mirror_channel_ids.txt').read_text().splitlines())
        self.assertTrue({f'dummy-{i}' for i in range(12375, 12395)} <= manifest)

    def test_national_league_tv_uses_football_not_general_event_rules(self):
        import xml.etree.ElementTree as ET
        labels = [
            'National League TV 01: FC Halifax Town vs. Yeovil @ 1 Nov 03:00 PM London',
            'National League TV 02: NO EVENT',
            'National League TV 03: Old Club vs. Other Club @ 31 Oct 03:00 PM London',
            'National League TV 04: Barrow vs. Eastleigh @ 1 Nov 11:00 AM London',
        ]
        folder = self.prepare_process(labels)
        football_manifest = Path(__file__).with_name('football_mirror_channel_ids.txt')
        event_manifest = Path(__file__).with_name('event_mirror_channel_ids.txt')
        (folder / 'football.txt').write_text(football_manifest.read_text())
        (folder / 'events.txt').write_text(event_manifest.read_text())
        root = ET.fromstring(self.requests.get.return_value.content)
        ids = [f'dummy-{i}' for i in range(12575, 12579)]
        for channel, channel_id, label in zip(root.findall('channel'), ids, labels):
            channel.set('id', channel_id)
            programme = ET.SubElement(root, 'programme', channel=channel_id, start='20261101000000 +0000', stop='20261102000000 +0000')
            ET.SubElement(programme, 'title').text = label
        self.requests.get.return_value.content = ET.tostring(root)
        real_datetime, utc = self.ns['datetime'], self.ns['UTC']
        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 1, 12, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        programmes = result.findall("programme[@channel='dummy-12575']")
        self.assertEqual([p.findtext('title') for p in programmes], [
            'Upcoming Match: FC Halifax Town vs. Yeovil at 3pm',
            'FC Halifax Town vs. Yeovil', 'Match ended',
        ])
        self.assertEqual(programmes[1].get('start'), '20261101150000 +0000')
        self.assertEqual(programmes[1].get('stop'), '20261101173000 +0000')
        for channel_id in ['dummy-12576', 'dummy-12577']:
            rows = result.findall(f"programme[@channel='{channel_id}']")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].findtext('title'), 'No match today')
        active = result.find("programme[@channel='dummy-12578']")
        self.assertEqual(active.findtext('title'), 'Barrow vs. Eastleigh')
        self.assertEqual(active.get('stop'), '20261101133000 +0000')
        self.assertEqual(result.find("channel[@id='dummy-12575']/display-name").text, 'National League TV 01')
        all_titles = [p.findtext('title') for p in result.findall('programme')]
        self.assertNotIn('No event today', all_titles)
        self.requests.post.assert_not_called()

    def test_national_league_tv_ids_are_exclusively_football_mirrors(self):
        ids = {f'dummy-{i}' for i in range(12575, 12625)}
        football = set(Path(__file__).with_name('football_mirror_channel_ids.txt').read_text().splitlines())
        events = set(Path(__file__).with_name('event_mirror_channel_ids.txt').read_text().splitlines())
        self.assertTrue(ids <= football)
        self.assertFalse(ids & events)

    def test_event_duration_rules_and_specific_overrides(self):
        duration = self.ns['_estimated_event_duration']
        for title, hours in [
            ('Premier League: A vs B', 2.5), ('Football: A vs B', 2.5),
            ('NBA basketball: A vs B', 3), ('MLB baseball: A vs B', 4),
            ('NFL American football: A vs B', 4), ('College football: A vs B', 4),
            ('PGA Golf', 6), ('Cricket T20', 6),
            ('Cincinnati Football Press Conference', 2), ('The Pat McAfee Show', 2),
            ('The Golics', 2), ('Unidentified A vs B', 4),
        ]:
            with self.subTest(title=title):
                self.assertEqual(duration(title).total_seconds(), hours * 3600)

    def test_event_bounds_preserve_source_end_and_cap_next_start(self):
        ET, dt, utc = self.ns['ET'], self.ns['datetime'], self.ns['UTC']
        def event(start, stop, title, channel='c0'):
            row = ET.Element('programme', channel=channel, start=start, stop=stop)
            ET.SubElement(row, 'title').text = title
            ET.SubElement(row, 'desc').text = 'Original description'
            return row
        # Actual source end wins over the four-hour fallback, and the next
        # known start wins over even an overlapping actual source end.
        first = event('20261102080000 +0000', '20261103000000 +0000', 'Morning event')
        second = event('20261102110000 +0000', '20261103000000 +0000', 'NBA basketball')
        third = event('20261102130000 +0000', '20261103000000 +0000', 'Afternoon event')
        source = [event('20261102080000 +0000', '20261102093000 +0000', 'Morning event'),
                  event('20261102110000 +0000', '20261102150000 +0000', 'NBA basketball')]
        rows = [first, second, third]
        self.ns['_bound_event_programmes'](rows, [first, second, third], source, dt(2026, 11, 2, 23, tzinfo=utc))
        self.assertEqual(first.get('stop'), '20261102093000 +0000')
        self.assertEqual(second.get('stop'), '20261102130000 +0000')
        self.assertEqual(third.get('stop'), '20261102170000 +0000')
        self.assertNotIn('estimated', first.findtext('desc'))
        self.assertIn('End time estimated.', third.findtext('desc'))
        notices = [r for r in rows if r.findtext('title') == 'No further event scheduled']
        self.assertEqual([(r.get('start'), r.get('stop')) for r in notices], [
            ('20261102093000 +0000', '20261102110000 +0000'),
            ('20261102170000 +0000', '20261103000000 +0000'),
        ])

    def test_event_estimate_crosses_midnight_and_ignores_source_placeholders(self):
        ET, dt, utc = self.ns['ET'], self.ns['datetime'], self.ns['UTC']
        event = ET.Element('programme', channel='c0', start='20261102230000 +0000', stop='20261103000000 +0000')
        ET.SubElement(event, 'title').text = 'Unidentified event'
        placeholder = ET.Element('programme', channel='c0', start=event.get('start'), stop='20261103000000 +0000')
        ET.SubElement(placeholder, 'title').text = 'No event today'
        rows = [event]
        self.ns['_bound_event_programmes'](rows, [event], [placeholder], dt(2026, 11, 3, 1, tzinfo=utc))
        self.assertEqual(event.get('stop'), '20261103030000 +0000')
        self.assertEqual(rows[1].get('start'), '20261103030000 +0000')
        self.assertEqual(rows[1].get('stop'), '20261104000000 +0000')
        self.assertIn('End time estimated.', event.findtext('desc'))

    def test_generic_provider_events_expire_in_published_xml_without_ai(self):
        ET = self.ns['ET']
        labels = [
            'ESPN+ 01: Unknown event @ 2 Nov 08:00AM London',
            'Disney+ Events 01: NBA basketball @ 2 Nov 08:00AM London',
            'DAZN UK 01: Football: A vs B @ 2 Nov 08:00 AM London',
            'MAX UK 01: PGA Golf @ 2 Nov 08:00AM London',
            'Paramount+ 01: Press Conference @ 2 Nov 03:00AM ET',
            'ESPN+ 02: Overnight event @ 1 Nov 11:00PM London',
            'LIVE EVENT 02 - 3AM Unknown event',
            'LIVE EVENT 01',
        ]
        folder = self.prepare_process(labels)
        real_datetime, utc = self.ns['datetime'], self.ns['UTC']
        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 2, 23, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        root = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for i, stop in enumerate(['120000', '110000', '103000', '140000', '100000']):
            rows = root.findall(f"programme[@channel='c{i}']")
            self.assertEqual(rows[0].get('stop'), f'20261102{stop} +0000')
            self.assertIn('End time estimated.', rows[0].findtext('desc'))
            self.assertEqual(rows[1].findtext('title'), 'No further event scheduled')
        overnight = root.findall("programme[@channel='c5']")
        self.assertEqual(overnight[0].get('stop'), '20261102030000 +0000')
        self.assertEqual(overnight[1].findtext('title'), 'No further event scheduled')
        live = root.findall("programme[@channel='c6']")
        self.assertEqual(live[0].get('stop'), '20261102120000 +0000')
        self.assertEqual(live[1].findtext('title'), 'No further event scheduled')
        continuous = root.findall("programme[@channel='c7']")
        self.assertEqual(len(continuous), 1)
        self.assertEqual(continuous[0].findtext('title'), 'UFC Special Feed')
        self.assertNotIn('estimated', continuous[0].findtext('desc'))
        self.requests.post.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def test_programme_date_labels_remove_only_separated_metadata(self):
        clean = self.ns['_clean_programme_date_labels']
        for raw, expected in [
            ('Tue, 11/3 - LALIGA Highlight Show', 'LALIGA Highlight Show'),
            ('Tue 11/3, The Rich Eisen Show', 'The Rich Eisen Show'),
            ('11/3 - LALIGA Highlight Show', 'LALIGA Highlight Show'),
            ('Tuesday, 03/11/2026 | Football', 'Football'),
            ('2026-11-03: Tennis', 'Tennis'),
            ('Football - Tue, 11/3', 'Football'),
            ('Upcoming Event: Tue, 11/3 - Show at 5pm', 'Upcoming Event: Show at 5pm'),
            ('Formula 1 2026', 'Formula 1 2026'),
            ('The 11/3 Story', 'The 11/3 Story'),
            ('November 3: A Documentary', 'November 3: A Documentary'),
            ('30 for 30', '30 for 30'),
            ('Tue, 11/3', 'Tue, 11/3'),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(clean(raw), expected)
                self.assertEqual(clean(clean(raw)), expected)

    def test_date_cleanup_in_published_xml_preserves_schedule_and_channel_ids(self):
        ET = self.ns['ET']
        folder = self.prepare_process([
            'ESPN+ 07: Tue, 11/3 - LALIGA Highlight Show @ Nov 03 12:00PM ET',
            'ESPN+ 08: Tue, 11/3 - The Rich Eisen Show @ Nov 03 12:00PM ET',
            'Ordinary channel',
        ])
        source = ET.fromstring(self.requests.get.return_value.content)
        row = ET.SubElement(source, 'programme', channel='c2', start='20261103170000 +0000', stop='20261103180000 +0000')
        ET.SubElement(row, 'title').text = 'Tue 11/3, Retained provider show'
        self.requests.get.return_value.content = ET.tostring(source)
        real_datetime, utc = self.ns['datetime'], self.ns['UTC']
        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 3, 16, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for i, title in enumerate(['LALIGA Highlight Show', 'The Rich Eisen Show']):
            rows = result.findall(f"programme[@channel='c{i}']")
            self.assertEqual(rows[0].findtext('title'), f'Upcoming Event: {title} at 5pm')
            self.assertEqual(rows[1].findtext('title'), title)
            self.assertEqual(rows[1].get('start'), '20261103170000 +0000')
        retained = result.find("programme[@channel='c2']")
        self.assertEqual(retained.findtext('title'), 'Retained provider show')
        self.assertEqual(retained.get('stop'), '20261103180000 +0000')
        self.requests.post.assert_not_called()

    def test_migration_keeps_success_and_removes_blanket_failures(self):
        now = self.ns['datetime'].now(self.ns['UTC'])
        cache = {'entries': {'success': {'result': 'Keep me', 'created_at': now.isoformat()},
                             'legacy-failure': {'failed': True, 'retry_after': (now + self.ns['timedelta'](hours=6)).isoformat()}}}
        self.ns['_prune_ai_cache'](cache)
        self.assertEqual(set(cache['entries']), {'success'})


if __name__ == '__main__':
    unittest.main()





