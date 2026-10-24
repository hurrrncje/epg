# Daily cleaned EPG

This repository downloads the provider XMLTV feed, cleans programme titles for
channels whose display-name alias contains `LIVE EVENT 01` through `LIVE EVENT
36`, and publishes `cleaned_epg.xml.gz`. GitHub Actions runs daily at 05:15 UTC
and can also be started manually from the Actions tab.

## One-time setup

1. Add `clean_epg.py`, `requirements.txt`, and `.github/workflows/update-epg.yml`
   to the root of your public GitHub repository. Keep `cleaned_epg.xml.gz` in
   that same repository so its raw URL remains stable.
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
