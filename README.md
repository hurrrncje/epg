# Daily cleaned EPG

This repository downloads the provider XMLTV feed, cleans programme titles for
channels whose display-name alias contains `LIVE EVENT 01` through `LIVE EVENT
36`, and publishes `cleaned_epg.xml.gz`. GitHub Actions runs daily at 05:15,
07:15, and 09:15 UK time (Europe/London) and can also be started manually from
the Actions tab. GitHub may delay scheduled runs during busy periods.
The workflow also saves Gemini results in `ai_cache.json` and commits that file
with the EPG. Unchanged event inputs reuse cached titles and descriptions;
changed or new events may require new Gemini requests. Failed API requests are
also held for six hours so the three morning runs do not repeatedly hit the
same exhausted quota.

For those channels, the generated EPG replaces the provider's placeholder
programme rows with listings based on the daily event label. Every channel
display-name is reduced to its stable `LIVE EVENT NN` name. Times are interpreted
in `America/New_York` (including daylight saving time), converted to UTC/GMT,
and each generated listing ends at the next midnight UTC. A label such as
`5:25/8pm` creates two start-time entries, interpreting the first time as 5:25pm.
Channels marked `No Event` or with no explicit AM/PM time receive no programme
entry that day.
Before the first scheduled start, an `Upcoming Event: [Event Name] at Hpm`
programme runs from midnight GMT until kickoff so UHF has guide information in
the pre-event period. Titles use a compact 12-hour GMT clock, for example
`2pm` or `2.30pm`. LIVE EVENT programme titles omit the `LIVE EVENT NN` prefix
and the source label's leading kickoff time.

For channels whose XMLTV IDs start with `EPL` or `EFL`, fixture text is changed
from `Away @ Home` to `Home vs Away`. `Next Game` dates are rendered as
`DD/MM/YYYY HH:MM GMT`; Eastern timestamps are converted using their stated
EDT/EST offset, including daylight saving time.

Dynamic labels for `EPL01`–`EPL10` are parsed as home team, UK kickoff time,
away team, and date. The programme title becomes `Home vs Away`; kickoff is
converted from Europe/London time to UTC/GMT, with an upcoming listing before
kickoff and the match listing continuing to midnight GMT.

Dynamic `Paramount+ 01`–`Paramount+ 100` labels use the event text and Eastern
date/time after the prefix. The programme title drops the channel number; the
upcoming entry precedes kickoff and the event listing continues until midnight
GMT on the kickoff date.

## One-time setup

1. Add `clean_epg.py`, `requirements.txt`, and `.github/workflows/update-epg.yml`
   to the root of your public GitHub repository. Keep `cleaned_epg.xml.gz` and
   the generated `ai_cache.json` in that same repository so the EPG URL stays
   stable and the AI cache persists between runs.
2. In the repository, open **Settings → Secrets and variables → Actions → New
   repository secret**. Name it `GEMINI_API_KEY` and paste your Gemini API key
   as the secret value. Do not put the key in the script or a committed file.
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
be on. Gemini API usage may incur charges or quota consumption according to
your Google AI account.
