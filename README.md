# orse-prospects (dry-run tooling)

OrseBusiness prospect research for the Netherlands pilot. **Dry-run only:** writes local files in
`data/out/`. No database writes, no Gmail, no email, no contact with any business.

```
python -m venv .venv && .venv/Scripts/python -m pip install osmium==4.3.1
.venv/Scripts/python run.py dryrun --city Amersfoort --pbf data/raw/utrecht-latest.osm.pbf
.venv/Scripts/python run.py drafts --scored data/out/<file>-scored.json --env ../leadflow/.env.local --count 5
.venv/Scripts/python -m unittest discover -s tests -t .
```

Pipeline: collect (Overpass, fallback Geofabrik extract) -> dedupe + red list (`data/do_not_contact.csv`)
-> enrich (robots.txt respected, 1 request at a time, max 4 pages/site, role emails on own domain only)
-> deterministic score -> channel rule (email ONLY for B.V.; others manual_review / phone_or_contact_form)
-> Groq drafts (local files, validated, human review required).
