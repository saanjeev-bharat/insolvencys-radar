# Insolvency Radar (Australia)

Collects public notices of companies in liquidation, administration or receivership, plus
insolvency auctions and mortgagee sales, extracts the appointed practitioner's contact
details, and serves a searchable dashboard.

## Run
```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...          # optional: improves extraction on messy text
export RADAR_USER_AGENT="InsolvencyRadar/0.1 (you@example.com)"
python -m radar.pipeline              # ingest
uvicorn radar.api:app --reload        # http://localhost:8000
pytest                                # offline tests on sample notices
```
Schedule `python -m radar.pipeline` daily (cron or similar).

## Sources (edit `radar/sources.py`)
- ASIC Published Notices (appointments, winding-up) - the authoritative source.
- Auction houses that run insolvency / repossession sales (Pickles, Grays, Lawsons...).
- Mortgagee-in-possession property listings (realestate.com.au, Domain).
- Add `FileSource` or your own `Source` subclass for anything else (e.g. ASIC/AFSA bulk data on data.gov.au).

Selectors in the adapters are starting points and must be checked against the live sites.
Respect each site's terms of use and robots.txt; where a licensed feed or API exists, prefer it.

## Design choices
- Contact is the **appointed liquidator / administrator / receiver** named on the notice. That is
  the correct party for creditors, buyers and asset enquiries.
- Company/corporate matters only. Personal bankruptcies (individuals) are deliberately excluded.
- Every record keeps its source URL and a confidence score; verify details against the source
  notice before acting. Nothing here is legal or financial advice.
