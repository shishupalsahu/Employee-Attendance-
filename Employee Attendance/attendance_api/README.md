# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+, tested on 7.0). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env
python sample_seed.py                                  # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```
`MONGO_URI` and `MONGO_DB` come from the environment (a `.env` file is read only for values not already set).
Indexes are created automatically at startup.

## Notes
* Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.
* Analytics are MongoDB aggregation pipelines (`$lookup`, `$setWindowFields`, `$densify`); Python only formats numbers (half-up rounding).
* Review findings are in `REVIEW.md`, design answers in `DECISIONS.md`.
* Nothing is known to be missing. Tested locally with my own data for ties, weekend records, a mid-month joiner, an overnight shift, employees without logs, and about 100k records.
