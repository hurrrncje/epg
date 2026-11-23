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

    def test_local_templates_use_explicit_clues_without_guessing_team_sport(self):
        describe = self.ns['_local_event_description']
        for title, expected in [
            ('Cincinnati Football Press Conference', 'Press conference covering Cincinnati Football.'),
            ('Grand Slam of Curling: Masters', 'Coverage of the Masters event in the Grand Slam of Curling.'),
            ('Tue, 11/3 - LALIGA Highlight Show', 'Highlights from Spanish LaLiga football.'),
            ('Unknown University vs. Other College', 'Coverage of Unknown University against Other College.'),
        ]:
            with self.subTest(title=title):
                self.assertEqual(describe(title), expected)
        self.assertIn('Football coverage', describe('Barcelona U19 vs. Aston Villa U19', 'UEFA Youth League'))
        self.assertIn('snooker', describe('International Championship: Mark Williams vs. John Higgins').lower())
        self.requests.post.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def prepare_process(self, labels):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        for constant, name in [('AI_CACHE_FILE', 'ai_cache.json'), ('OUTPUT_GZ_FILE', 'guide.gz'),
                                ('FOOTBALL_TEAM_CHANNELS_FILE', 'teams.tsv'),
                                ('FOOTBALL_MIRROR_IDS_FILE', 'football.txt'), ('EVENT_MIRROR_IDS_FILE', 'events.txt')]:
            self.ns[constant] = folder / name
        xml = '<tv>' + ''.join(f'<channel id="c{i}"><display-name>{label}</display-name></channel>' for i, label in enumerate(labels)) + '</tv>'
        self.requests.get.return_value = Response(content=xml.encode())
        return folder

    def test_description_generation_ignores_ai_keys_and_legacy_cache(self):
        today = self.ns['datetime'].now(self.ns['EASTERN']).strftime('%b %d')
        folder = self.prepare_process([f'ESPN+ 01: The Pat McAfee Show @ {today} 7:00PM ET'])
        (folder / 'ai_cache.json').write_text(json.dumps({'version': 1, 'entries': {
            'old-result': {'kind': 'event-description', 'created_at': self.ns['datetime'].now(self.ns['UTC']).isoformat(), 'result': 'Same NHL game as e1'},
        }}))
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'key', 'GROQ_API_KEY': 'key'}):
            self.ns['process_epg']()
        xml = gzip.decompress((folder / 'guide.gz').read_bytes()).decode()
        self.assertIn('Sports discussion and interviews on The Pat McAfee Show.', xml)
        self.assertNotIn('Same NHL game as e1', xml)
        self.assertIn('0 description API calls', self.output.getvalue())
        self.requests.post.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def test_matching_provider_description_wins_without_leaking_other_events(self):
        ET = self.ns['ET']
        def row(title, start, stop, desc=None):
            item = ET.Element('programme', channel='c0', start=start, stop=stop)
            ET.SubElement(item, 'title').text = title
            if desc is not None:
                ET.SubElement(item, 'desc').text = desc
            return item
        source = [
            row('Team A vs. Team B', '20261103170000 +0000', '20261103190000 +0000', 'Provider coverage with original commentary and interviews.'),
            row('Different event', '20261103190000 +0000', '20261103210000 +0000', 'Do not copy this unrelated description.'),
        ]
        rows = [row('Upcoming Event: Team A vs. Team B at 5pm', '20261103000000 +0000', '20261103170000 +0000'),
                row('Team A vs. Team B', '20261103170000 +0000', '20261103190000 +0000'),
                row('Team A vs. Team B', '20261104170000 +0000', '20261104190000 +0000'),
                row('No event today', '20261103000000 +0000', '20261104000000 +0000', 'No event today')]
        self.ns['_apply_local_event_descriptions'](rows, source, {})
        for item in rows[:2]:
            self.assertIn('Provider coverage with original commentary', item.findtext('desc'))
            self.assertIn('Scheduled start: 03/11/2026 17:00 GMT.', item.findtext('desc'))
        self.assertNotIn('Provider coverage', rows[2].findtext('desc'))
        self.assertNotIn('unrelated description', rows[2].findtext('desc'))
        self.assertEqual(rows[3].findtext('desc'), 'No event today')

    def test_title_cleanup_requests_titles_only_and_discards_ai_description(self):
        today = self.ns['datetime'].now(self.ns['LONDON']).strftime('%d %b')
        folder = self.prepare_process([f'DAZN UK 01: A vs B @ {today} 07:00 PM London'])
        self.client.models.generate_content.return_value = types.SimpleNamespace(text=json.dumps([
            {'raw': 'A vs B', 'clean_title': 'A vs B', 'description': 'Invented AI description'}]))
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'key', 'GROQ_API_KEY': 'key'}):
            self.ns['process_epg']()
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.requests.post.assert_not_called()
        prompt = self.client.models.generate_content.call_args.kwargs['contents']
        self.assertIn('without a description', prompt)
        xml = gzip.decompress((folder / 'guide.gz').read_bytes()).decode()
        self.assertIn('Coverage of A against B', xml)
        self.assertNotIn('Invented AI description', xml)

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
        stale = 'Sky Sports + 01: Previous fixture @ 31 Oct 09:00 AM London'
        ET.SubElement(root.find("channel[@id='c0']"), 'display-name').text = stale
        for i, label in enumerate(labels):
            if i == 0:
                label = stale
            if i == 1:
                continue  # Only the synced channel name contains this fixture.
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
        self.assertEqual(result.findall("programme[@channel='c0']")[3].get('stop'), '20261101183000 +0000')
        active = result.find("programme[@channel='c1']")
        self.assertEqual(active.findtext('title'), 'Tennis C v D')
        self.assertEqual(active.get('stop'), '20261101140000 +0000')
        self.assertEqual(result.find("programme[@channel='c2']/title").text, 'No event today')
        self.assertEqual(result.find("programme[@channel='c3']/title").text, 'No further event scheduled')
        self.assertEqual(result.find("programme[@channel='c4']/title").text, 'Original linear schedule')
        self.requests.post.assert_not_called()
        manifest = set(Path(__file__).with_name('event_mirror_channel_ids.txt').read_text().splitlines())
        self.assertTrue({f'dummy-{i}' for i in range(1412, 1472)} <= manifest)

    def test_long_event_visible_in_afternoon_and_late_evening(self):
        ET = self.ns['ET']
        root = ET.fromstring('<tv><programme channel="event" start="20261118000000 +0000" '
                             'stop="20261119001500 +0000"><title>Upcoming event</title>'
                             '<desc>Fixture details</desc></programme></tv>')
        self.ns['_split_long_event_listings'](root, {'event'})
        rows = list(root)
        self.assertEqual(rows[0].get('start'), '20261118000000 +0000')
        self.assertEqual(rows[-1].get('stop'), '20261119001500 +0000')
        for left, right in zip(rows, rows[1:]):
            self.assertEqual(left.get('stop'), right.get('start'))
        for hour in (6, 13, 23):
            now = f'20261118{hour:02d}1500 +0000'
            earliest = f'20261118{hour - 6:02d}1500 +0000'
            visible = [r for r in rows if earliest <= r.get('start') <= now < r.get('stop')]
            self.assertEqual([r.findtext('desc') for r in visible], ['Fixture details'])

    def test_native_preview_ends_when_the_broadcast_starts(self):
        ET = self.ns['ET']
        guide = ET.fromstring('<tv><programme channel="event-7" start="20261121120000 +0000" stop="20261121140000 +0000"><title>Next Match: A v B</title></programme><programme channel="event-7" start="20261121132500 +0000" stop="20261121150000 +0000"><title>A v B</title></programme></tv>')
        self.ns['_clean_native_dynamic_rows'](guide, {'event-7'}, set())
        self.assertEqual(guide[0].get('stop'), guide[1].get('start'))
        self.assertEqual(guide[0].findtext('title'), 'Upcoming Event: A v B at 1.25pm')

    def test_playlistlabs_dynamic_ids_use_fixture_time_and_empty_slot_rules(self):
        ET = self.ns['ET']
        folder = self.prepare_process([])
        (folder / 'football.txt').write_text('dummy-22\n')
        root = ET.Element('tv')
        today = self.ns['datetime'].now(self.ns['LONDON']).strftime('%d %b')
        for number, label, text in [
            (20, f'Sky Sports + 09: A v B @ {today} 01:25 PM London', 'Next Match: A v B'),
            (21, 'General Feed 37: NO EVENT', 'No scheduled event'),
            (22, 'National League TV 22: NO MATCH', 'No scheduled event'),
        ]:
            key = f'event-{number}'
            ET.SubElement(ET.SubElement(root, 'channel', id=key), 'display-name').text = label
            row = ET.SubElement(root, 'programme', channel=key)
            ET.SubElement(row, 'title').text = text
        self.requests.get.return_value.content = ET.tostring(root)
        self.ns['process_epg']()
        guide = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        rows = guide.findall("programme[@channel='event-20']")
        self.assertTrue(any(row.findtext('title') == 'A v B' for row in rows))
        self.assertTrue(any(row.findtext('title').startswith('Upcoming Event:') for row in rows))
        self.assertEqual(guide.findtext("programme[@channel='event-21']/title"), 'No event today')
        self.assertEqual(guide.findtext("programme[@channel='event-22']/title"), 'No match today')

    def test_team_and_spfl_empty_slots_use_exact_match_wording(self):
        ET = self.ns['ET']
        guide = ET.Element('tv')
        for channel, title in [('Arsenal.epl', 'No Game Today'), ('event-12476', 'No event today'),
                               ('event-12476', 'Upcoming Match: A vs B'), ('other', 'No event today')]:
            row = ET.SubElement(guide, 'programme', channel=channel)
            ET.SubElement(row, 'title').text = title
            ET.SubElement(row, 'desc').text = title
        self.ns['_normalise_football_notices'](guide, {'Arsenal.epl', 'event-12476'})
        for tag in ('title', 'desc'):
            self.assertEqual([r.findtext(tag) for r in guide],
                             ['No match today', 'No match today', 'Upcoming Match: A vs B', 'No event today'])

    def test_missing_football_listings_get_no_match_today(self):
        import xml.etree.ElementTree as ET
        folder = self.prepare_process([
            'Birmingham City', 'EPL 01', 'EFL02', 'Blackburn Rovers', 'General channel',
            'Burnley', 'EPL03:',
        ])
        (folder / 'football.txt').write_text('c0\nc3\nc5\n')
        root = ET.fromstring(self.requests.get.return_value.content)
        now = self.ns['datetime'].now(self.ns['UTC'])
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stop = start + self.ns['timedelta'](days=1)
        for channel, title in [
            ('c1', 'EPL01'), ('c3', 'Blackburn Rovers vs. Burnley'),
            ('c5', 'BURNLEY:'), ('c6', 'Another listed fixture'),
        ]:
            row = ET.SubElement(root, 'programme', channel=channel,
                                start=start.strftime('%Y%m%d%H%M%S %z'),
                                stop=stop.strftime('%Y%m%d%H%M%S %z'))
            ET.SubElement(row, 'title').text = title
        self.requests.get.return_value.content = ET.tostring(root)

        with patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()

        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for channel in ('c0', 'c1', 'c2', 'c5'):
            rows = result.findall(f"programme[@channel='{channel}']")
            self.assertEqual([row.findtext('title') for row in rows], ['No match today'] * 4)
            self.assertEqual(rows[-1].get('stop'), stop.strftime('%Y%m%d%H%M%S %z'))
        self.assertEqual(result.findtext("programme[@channel='c3']/title"),
                         'Blackburn Rovers vs Burnley')
        self.assertIsNone(result.find("programme[@channel='c4']"))
        self.assertEqual(result.findtext("programme[@channel='c6']/title"),
                         'Another listed fixture')

    def test_club_next_match_uses_opponent_and_uk_clock(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("""<tv><programme channel="club">
          <title>⚽ [EPL] Arsenal - Next: Tue Nov 24, 8:00 PM GMT</title>
          <desc>No game today. Next: ⚽ [EPL] Arsenal vs ⚽ [Bundesliga] Borussia Dortmund on Paramount+. Tue Nov 24: 3:00 PM ET · 8:00 PM UK.</desc>
        </programme></tv>""")
        self.ns['_clean_team_previews'](root, {'club'})
        for tag in ('title', 'desc'):
            self.assertEqual(root.findtext('programme/' + tag),
                             'Next match: Arsenal vs Borussia Dortmund, Tuesday 24 Nov, 20:00')

    def test_club_time_aligns_duplicate_fixture_and_preview_boundary(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("""<tv>
          <channel id="monepg.epl.1"><display-name>Arsenal</display-name></channel>
          <programme channel="monepg.epl.1"><title>Upcoming: Home to Borussia Dortmund, Tue 24 Nov 20:00</title></programme>
          <programme channel="Arsenal.epl"><title>No match today</title><desc>Next Game: Borussia Dortmund @ Arsenal on 2026-11-24 at 04:00PM EST</desc></programme>
          <programme channel="Arsenal.epl" start="20261124000000 +0000" stop="20261124210000 +0000"><title>Upcoming match: Arsenal vs Borussia Dortmund at 21:00</title></programme>
          <programme channel="Arsenal.epl" start="20261124210000 +0000" stop="20261124231500 +0000"><title>Arsenal vs Borussia Dortmund</title></programme>
        </tv>""")
        self.ns['_clean_team_previews'](root, set())
        notice, preview, live = root.findall("programme[@channel='Arsenal.epl']")
        self.assertEqual(notice.findtext('title'), 'Next match: Arsenal vs Borussia Dortmund, Tuesday 24 Nov, 20:00')
        self.assertEqual(preview.get('stop'), live.get('start'))
        self.assertEqual(live.get('start'), '20261124200000 +0000')
        self.assertEqual(live.findtext('title'), 'Arsenal vs Borussia Dortmund')

    def test_description_only_next_matches_without_another_feed(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("""<tv>
          <programme channel="Club.efl"><title>Signing Off</title><desc>Next Game: Visitors @ City on 2026-11-25 at 02:45PM EST</desc></programme>
          <programme channel="Club.spfl"><title>No match today</title><desc>No game today. Next: ⚽ [SPFL] City vs ⚽ [SPFL] Visitors on TV. Wed Nov 25: 2:45 PM ET · 7:45 PM UK.</desc></programme>
          <programme channel="Club.epl"><title>Club interviews</title><desc>Next Game: Visitors @ City on 2026-11-25 at 02:45PM EST</desc></programme>
        </tv>""")
        self.ns['_clean_team_previews'](root, set())
        self.assertEqual([p.findtext('title') for p in root], [
            'Next match: City vs Visitors, Wednesday 25 Nov, 19:45',
            'Next match: City vs Visitors, Wednesday 25 Nov, 19:45',
            'Club interviews',
        ])

    def test_provider_matchday_starts_at_midnight_and_keeps_live_slot(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring("""<tv>
          <programme channel="Everton.epl" start="20261122220000 +0000" stop="20261123020000 +0000"><title>No match today</title></programme>
          <programme channel="Everton.epl" start="20261123140000 +0000" stop="20261123200000 +0000"><title>Next Game: Everton @ Brentford FC on 2026-11-23 at 03:00PM EST</title></programme>
          <programme channel="Everton.epl" start="20261123200000 +0000" stop="20261123221500 +0000"><title>Everton @ Brentford FC</title></programme>
        </tv>""")
        self.ns['_build_football_matchdays'](root, {'Everton.epl'})
        self.assertEqual([(p.get('start')[:12], p.get('stop')[:12], p.findtext('title'))
                          for p in root.findall('programme')], [
            ('202611222200', '202611230000', 'No match today'),
            ('202611230000', '202611232000', 'Upcoming match: Brentford FC vs Everton at 20:00'),
            ('202611232000', '202611232215', 'Brentford FC vs Everton'),
        ])

    def test_omitted_playlist_teams_receive_no_match_today(self):
        import xml.etree.ElementTree as ET
        folder = self.prepare_process(['General channel'])
        (folder / 'teams.tsv').write_text('EFLBirminghamCity.uk\tBirmingham City\nEPLArsenal.uk\tArsenal\n')
        with patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for channel_id in ('EFLBirminghamCity.uk', 'EPLArsenal.uk'):
            self.assertIsNotNone(result.find(f"channel[@id='{channel_id}']"))
            self.assertEqual(result.findtext(f"programme[@channel='{channel_id}']/title"), 'No match today')

    def test_efl_backup_pipe_labels_and_tomorrow_upcoming(self):
        import xml.etree.ElementTree as ET
        tomorrow = self.ns['datetime'].now(self.ns['UTC']).date() + self.ns['timedelta'](days=1)
        label = f'EFL01ⓧ| Bristol City 20:00 Watford (CH) {tomorrow:%d/%m/%Y}'
        parsed = self.ns['_parse_epl_dynamic_label'](label, tomorrow)
        self.assertEqual(parsed['title'], 'Bristol City vs Watford')
        folder = self.prepare_process([label])
        with patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        upcoming = result.find("programme[@channel='c0']")
        self.assertEqual(upcoming.findtext('title'), f'Upcoming match: Bristol City vs Watford at {parsed["start_utc"].astimezone(self.ns["LONDON"]):%H:%M}')
        self.assertEqual(upcoming.get('start')[:8], self.ns['datetime'].now(self.ns['UTC']).strftime('%Y%m%d'))

    def test_news_placeholders_use_detailed_alias_and_backup_schedules(self):
        import xml.etree.ElementTree as ET
        now = self.ns['datetime'](2026, 11, 18, 12, tzinfo=self.ns['UTC'])
        root = ET.fromstring('''<tv>
          <channel id="bbcnews.uk"/><channel id="skynews.uk"/><channel id="Sky.News.be"/>
          <programme channel="bbcnews.uk" start="20261118000000 +0000" stop="20261119000000 +0000"><title>BBC News</title></programme>
          <programme channel="skynews.uk" start="20261118000000 +0000" stop="20261119000000 +0000"><title>Sky News</title></programme>
          <programme channel="Sky.News.be" start="20261118120000 +0000" stop="20261118130000 +0000"><title>Sky News Today</title></programme>
        </tv>''')
        self.requests.get.return_value = Response(content=gzip.compress(b'''<tv>
          <programme channel="BBC.NEWS.HD.uk" start="20261118120000 +0000" stop="20261118130000 +0000"><title>BBC News Now</title></programme>
        </tv>'''))
        self.ns['_fill_uk_news_epg_gaps'](root, now)
        self.assertEqual(root.findtext("programme[@channel='bbcnews.uk']/title"), 'BBC News Now')
        self.assertEqual(root.findtext("programme[@channel='skynews.uk']/title"), 'Sky News Today')
        self.assertEqual(root.findtext("programme[@channel='Sky.News.be']/title"), 'Sky News Today')
        self.requests.get.reset_mock()
        self.ns['_fill_uk_news_epg_gaps'](root, now)
        self.requests.get.assert_not_called()
        manifest = set(Path(__file__).with_name('event_mirror_channel_ids.txt').read_text().splitlines())
        news_ids = {channel_id for ids, _ in self.ns['UK_NEWS_CHANNELS'] for channel_id in ids}
        self.assertFalse(news_ids & manifest)

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
            *(['Upcoming match: Chelsea vs Manchester City at 15:00'] * 3), 'Chelsea vs Manchester City', 'Match ended', 'Match ended',
        ])
        self.assertEqual(programmes[3].get('start'), '20260516140000 +0000')
        self.assertEqual(programmes[3].get('stop'), '20260516163000 +0000')
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
            *(['Upcoming match: FC Halifax Town vs Yeovil at 15:00'] * 3), 'FC Halifax Town vs Yeovil', 'Match ended', 'Match ended',
        ])
        self.assertEqual(programmes[3].get('start'), '20261101150000 +0000')
        self.assertEqual(programmes[3].get('stop'), '20261101173000 +0000')
        for channel_id in ['dummy-12576', 'dummy-12577']:
            rows = result.findall(f"programme[@channel='{channel_id}']")
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0].findtext('title'), 'No match today')
        active = result.find("programme[@channel='dummy-12578'][@start='20261101110000 +0000']")
        self.assertEqual(active.findtext('title'), 'Barrow vs Eastleigh')
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
        self.assertEqual(len(continuous), 4)
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
            self.assertEqual(rows[3].findtext('title'), title)
            self.assertEqual(rows[3].get('start'), '20261103170000 +0000')
        retained = result.find("programme[@channel='c2']")
        self.assertEqual(retained.findtext('title'), 'Retained provider show')
        self.assertEqual(retained.get('stop'), '20261103180000 +0000')
        self.requests.post.assert_not_called()

    def test_all_general_event_groups_replace_empty_provider_rows(self):
        ET = self.ns['ET']
        labels = [
            'DAZN UK 30: NO EVENT', 'DAZN UK 31: ',
            'Paramount+ 30: NO EVENT TODAY', 'Paramount+ 31: ',
            'Disney+ Events 30: NO EVENTS SCHEDULED', 'Disney+ Events 31: ',
            'ESPN+ 30: NO EVENT', 'ESPN+ 31: ',
            'MAX UK 30: NO GAME', 'MAX UK 31: ',
            'LIVE EVENT 01 - NO EVENT', '[PPV EVENT 37] - NO EVENT',
            'General Feed 01: NO EVENTS SCHEDULED',
            'General Feed 02: NO EVENT',
        ]
        folder = self.prepare_process(labels)
        (folder / 'events.txt').write_text('c12\nc13\n')
        root = ET.fromstring(self.requests.get.return_value.content)
        for i, label in enumerate(labels[:-1]):
            for hour in (18, 20):
                row = ET.SubElement(root, 'programme', channel=f'c{i}',
                    start=f'20261103{hour:02d}0000 +0000', stop=f'20261103{hour+2:02d}0000 +0000')
                ET.SubElement(row, 'title').text = label
        self.requests.get.return_value.content = ET.tostring(root)
        dt, utc = self.ns['datetime'], self.ns['UTC']
        class FrozenDatetime(dt):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 3, 19, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for i in range(len(labels)):
            with self.subTest(channel=labels[i]):
                rows = result.findall(f"programme[@channel='c{i}']")
                self.assertEqual(len(rows), 4)
                self.assertEqual(rows[0].findtext('title'), 'No event today')
                self.assertEqual(rows[0].findtext('desc'), 'No event today')
                self.assertEqual(rows[0].get('start'), '20261103000000 +0000')
                self.assertEqual(rows[-1].get('stop'), '20261104000000 +0000')
        self.requests.post.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def test_unparsed_event_is_not_mistaken_for_empty_schedule(self):
        ET = self.ns['ET']
        labels = ['DAZN UK 30: Tennis special (time TBC)',
                  'Paramount+ 30: Tennis special (time TBC)',
                  'Disney+ Events 30: Tennis special (time TBC)',
                  'MAX UK 30: Tennis special (time TBC)']
        folder = self.prepare_process(labels)
        root = ET.fromstring(self.requests.get.return_value.content)
        for i in range(len(labels)):
            row = ET.SubElement(root, 'programme', channel=f'c{i}', start='20261103180000 +0000', stop='20261103200000 +0000')
            ET.SubElement(row, 'title').text = 'Provider tennis coverage'
        self.requests.get.return_value.content = ET.tostring(root)
        with patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for i in range(len(labels)):
            self.assertEqual(result.find(f"programme[@channel='c{i}']/title").text, 'Provider tennis coverage')

    def check_tomorrow_upcoming_slots(self, minute):
        ET, dt, utc = self.ns['ET'], self.ns['datetime'], self.ns['UTC']
        clock = f'12:{minute:02d}AM'
        labels = [
            f'Disney+ Events 57: Jacksonville vs. Florida International @ Nov 04 {clock} London',
            f'ESPN+ 57: Event ESPN @ Nov 04 {clock} London',
            f'MAX UK 57: Event MAX @ Nov 04 {clock} London',
            f'DAZN UK 57: Event DAZN @ 04 Nov {clock} London',
            f'Paramount+ 57: Event Paramount @ 03 Nov 07:{minute:02d}PM ET',
            f'General Feed 57: Event Mirror @ 04 Nov {clock} London',
            f'LIVE EVENT 02 - 7:{minute:02d}PM Event Live',
        ]
        folder = self.prepare_process(labels)
        (folder / 'events.txt').write_text('c5\n')
        source = ET.fromstring(self.requests.get.return_value.content)
        for i, label in enumerate(labels):
            row = ET.SubElement(source, 'programme', channel=f'c{i}',
                start='20261103000000 +0000', stop='20261105000000 +0000')
            ET.SubElement(row, 'title').text = label
        self.requests.get.return_value.content = ET.tostring(source)
        class FrozenDatetime(dt):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 3, 23, 28, tzinfo=utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        with patch.dict(self.ns, {'datetime': FrozenDatetime}), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.ns['process_epg']()
        result = ET.fromstring(gzip.decompress((folder / 'guide.gz').read_bytes()))
        for i in range(len(labels)):
            with self.subTest(channel=labels[i], minute=minute):
                rows = result.findall(f"programme[@channel='c{i}']")
                upcoming = [r for r in rows if r.findtext('title', '').startswith('Upcoming Event:')]
                self.assertEqual(len(upcoming), 4 if minute == 0 else 5)
                self.assertEqual(upcoming[0].get('start'), '20261103000000 +0000')
                self.assertEqual(upcoming[-1].get('stop'), f'2026110400{minute:02d}00 +0000')
                active = [r for r in rows if r.get('start') == upcoming[-1].get('stop')]
                self.assertEqual(len(active), 1)
                self.assertIn('Scheduled start: 04/11/2026 00:', upcoming[0].findtext('desc'))
                self.assertFalse(any(r.findtext('title') == 'No event today' for r in rows))
        self.requests.post.assert_not_called()
        self.client.models.generate_content.assert_not_called()

    def test_tomorrow_midnight_events_have_upcoming_programme_today(self):
        self.check_tomorrow_upcoming_slots(0)

    def test_tomorrow_after_midnight_events_have_continuous_upcoming_programme(self):
        self.check_tomorrow_upcoming_slots(30)

    def test_migration_keeps_success_and_removes_blanket_failures(self):
        now = self.ns['datetime'].now(self.ns['UTC'])
        cache = {'entries': {'success': {'result': 'Keep me', 'created_at': now.isoformat()},
                             'legacy-failure': {'failed': True, 'retry_after': (now + self.ns['timedelta'](hours=6)).isoformat()}}}
        self.ns['_prune_ai_cache'](cache)
        self.assertEqual(set(cache['entries']), {'success'})


if __name__ == '__main__':
    unittest.main()








