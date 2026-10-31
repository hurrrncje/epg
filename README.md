# Daily cleaned EPG

This repository downloads the provider XMLTV feed, cleans programme listings
for `LIVE EVENT 01` through `LIVE EVENT 36`, `[PPV EVENT 37]` through
`[PPV EVENT 40]`, `DAZN UK 01` through `DAZN UK 100`,
`Paramount+ 01` through `Paramount+ 100`, `Disney+ Events 01` through
`Disney+ Events 500`, `ESPN+ 01` through `ESPN+ 400`, `MAX UK 01` through
`MAX UK 100`, and dynamic EPL and other
European football event channels, plus the reviewed mirrored channels listed
in `football_mirror_channel_ids.txt` and `event_mirror_channel_ids.txt`, then
publishes `cleaned_epg.xml.gz`.
GitHub Actions runs every three hours at :45 UK time, starting at 14:45
(Europe/London), and
can also be started manually from the Actions tab. GitHub may delay scheduled
runs during busy periods.
The workflow also saves generated AI results in `ai_cache.json` and commits that file
with the EPG. Unchanged event inputs reuse cached titles and descriptions;
changed or new events may require new AI requests. Gemini is tried first, with
Groq as a web-search-capable fallback when Gemini is unavailable or over quota.
Descriptions are prioritised before title cleanup. Matching event titles and
GMT kickoff times share descriptions across channels, including English and
Spanish feeds. DAZN title processing reuses an available event description.
Partial AI batches retain their successful results; only missing items use the
fallback provider. Groq uses low reasoning effort and compact response keys.

Provider cooldowns persist in `ai_cache.json`. Groq uses its `Retry-After`
header (or the wait in its error message); a wait below one minute gets one
bounded retry. Longer waits defer work until a later workflow run. Timeouts
pause a provider for five minutes. If Gemini gives no quota reset time, its
fallback cooldown is six hours. Only actually attempted, unsuccessful events
receive item cooldowns, normally at most fifteen minutes. Unattempted events
stay pending. The first run after this change removes the old blanket failure
cooldowns while retaining successful cache entries. Logs report new, cached,
unsuccessful and unattempted descriptions separately, plus token usage reported
by successful Groq requests. Successful cache entries older than 30 days and expired
failure cooldowns are pruned automatically.

For those channels, the generated EPG replaces the provider's placeholder
programme rows with listings based on the daily event label. LIVE EVENT channel
names are reduced to their stable `LIVE EVENT NN` names, and PPV EVENT names
retain their brackets and stable number. These transformations
are referred to as **Event Changes**. Times are interpreted
in `America/New_York` (including daylight saving time), converted to UTC/GMT,
and each generated listing ends at the next midnight UTC. A label such as
`5:25/8pm` creates two start-time entries, interpreting the first time as 5:25pm.
If a LIVE EVENT or PPV EVENT label says `No Event` (including a bare PPV EVENT
marker), it receives an all-day `No event today` listing. Event titles without
a parseable start time are cleaned and shown as an all-day listing.
Before the first scheduled start, an `Upcoming Event: [Event Name] at Hpm`
programme runs from midnight GMT until kickoff so UHF has guide information in
the pre-event period. Titles use a compact 12-hour GMT clock, for example
`2pm` or `2.30pm`. LIVE EVENT programme titles omit the `LIVE EVENT NN` prefix
and the source label's leading kickoff time.

For **Team Changes**, channels whose XMLTV IDs start with `EPL` or `EFL`, or
contain `spfl`, have fixture text changed from `Away @ Home` to `Home vs Away`, and `Game` in
programme titles becomes `Match` (for example, `No Match Today`). `Next Game` dates are rendered as
`DD/MM/YYYY HH:MM GMT`; Eastern timestamps are converted using their stated
EDT/EST offset, including daylight saving time.

These EPL/EFL/SPFL team-channel transformations are referred to as **Team
Changes**. They are separate from **League Changes**, which handle numbered
dynamic fixture channels such as EPL01–EPL10, EFL01–EFL35, and MLS 01–50.

Dynamic labels for `EPL01`–`EPL10`, `EFL01`–`EFL35`, `Bundesliga 01`–`10`, `La Liga 01`–`10`,
`Serie A 01`–`10`, `Ligue 1 01`–`10`, `MLS 01`–`50`, `Scottish Premiership 01`–`08`,
`Scottish Championship 01`–`09`, `Scottish Cup 1`–`10`, `FA Cup 01`–`20`,
`UEFA Champions League 01`–`20`, `UEFA Europa League 01`–`20`, and
`UEFA Europa Conf. League 01`–`20` are parsed as fixtures
with a kickoff time and date. Programme titles use `Home vs Away`; kickoff is
converted from the label's league-local time to UTC/GMT (MLS labels use Eastern
Time with daylight saving handled by the timezone database). For Ligue 1, the script also checks the
public ESPN scoreboard for that date and replaces the label time only when
both teams match an unambiguous fixture; if lookup or matching fails, it keeps
the provider time. The generated schedule uses `Upcoming Match` before kickoff,
the match listing for 2.5 hours, and `Match ended` until midnight GMT. Channels
without parseable fixture data receive an all-day `No match today` listing.
These transformations are referred to as **League Changes**.

The reviewed mirrored ID manifests classify additional channels as football
or general events. Football channels use `Upcoming Match`, a 2.5-hour match,
and `Match ended` through GMT midnight; unparsed or past fixtures show
`No match today`. General event channels use `Upcoming Event` before a parsed
kickoff and list the cleaned title through GMT midnight; unparsed titles are
listed all day, while explicit no-event and past-date labels show
`No event today`. These broad manifest targets use deterministic title cleanup and do
not request AI descriptions for every channel. Keep both text manifests beside
`clean_epg.py` in the GitHub repository; each contains one XMLTV channel ID per
line.

Dynamic `DAZN UK 01`–`DAZN UK 100` labels are parsed as event titles and London
kickoff dates/times. Channel names are normalized to their stable DAZN UK
number, event titles are cleaned with the same AI process as LIVE EVENT
channels, and events run from kickoff until midnight GMT. Active event titles
include the GMT kickoff time as `(Started Hpm)` or `(Started H.MMpm)`. Before
kickoff, an upcoming listing runs from today's midnight GMT to the event start.

Dynamic `Paramount+ 01`–`Paramount+ 100` labels use the event text and Eastern
date/time after the prefix. The programme title drops the channel number; the
upcoming entry precedes kickoff and the event listing continues until midnight
GMT on the kickoff date.

Dynamic `Disney+ Events 01`–`500` and `ESPN+ 01`–`400` use Eastern-time event
handling; `MAX UK 01`–`100` labels use London time. The provider prefix,
channel number, date, and time are removed from programme titles; channel names are reduced to
their stable numbered names. An upcoming listing runs until kickoff, then the
event listing continues until midnight GMT. AI-generated descriptions are
cached with the other event descriptions. If a label is empty or unparseable,
the provider's original rows are retained rather than removed.
If a parsed event or match date is earlier than the current GMT date, the
listing for that channel becomes an all-day `No event today` or `No match today`
entry, respectively; past events are skipped when collecting Gemini descriptions.

## One-time setup

1. Add `clean_epg.py`, `requirements.txt`, `football_mirror_channel_ids.txt`,
   `event_mirror_channel_ids.txt`, and `.github/workflows/update-epg.yml` to
   the root of your public GitHub repository. Keep `cleaned_epg.xml.gz` and the
   generated `ai_cache.json` in that same repository so the EPG URL stays
   stable and the AI cache persists between runs.
2. In the repository, open **Settings → Secrets and variables → Actions → New
   repository secret**. Add `GEMINI_API_KEY` and `GROQ_API_KEY` as separate
   secrets, using each provider's API key as its value. Do not put either key
   in the script or a committed file. Groq is used as a fallback when Gemini
   is unavailable or over quota.
3. Open **Settings → Actions → General → Workflow permissions**, choose **Read
   and write permissions**, and save. The workflow also requests write access in
   its YAML permissions.
4. Open the **Actions** tab and enable workflows if GitHub prompts you.
5. Select **Update cleaned EPG → Run workflow** to test it now. Check that the
   run succeeds and that it commits a new `cleaned_epg.xml.gz`.
6. Keep the existing raw GitHub URL in UHF. A daily commit replaces the file at
   that same path; raw-content caching may mean the new version takes a short
   time to appear. Refresh the EPG in UHF after the workflow completes.

The scheduled workflow uses GitHub-hosted runners, so your Mac does not need to
be on. Gemini and Groq API usage is subject to each provider's current quotas
and terms.
