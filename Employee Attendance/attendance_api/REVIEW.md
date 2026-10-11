# REVIEW.md

Defects found in the starter `app/main.py`. Every one is fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Created without `tz_aware=True`, so every datetime read back is naive. | `compute_overtime` subtracts a naive stored value from an IST-aware one and raises `TypeError` (500). | `tz_aware=True`. All instants are handled as aware UTC and converted to IST only for rules. |
| 2 | whole app | No indexes and no unique constraints. | Duplicate employees or days are possible, and `/attendance` is a collection scan on 100k rows. | `ensure_indexes()` runs at startup (see DECISIONS.md). |
| 3 | `health` | Always returns ok, never touches MongoDB. | Stop MongoDB and `/health` still says 200. The contract wants 503. | Pings MongoDB, 503 on failure. |
| 4 | `compute_late_minutes` | `punch_in.replace(hour, minute)` uses the datetime's own zone (UTC or server-local), not IST, so the shift start lands on the wrong clock time. | Punch-in 09:28 IST (03:58 UTC) is judged against 09:30 UTC and comes out hours late. | Shift start is built as an IST datetime on the attendance date. |
| 5 | `compute_late_minutes` | Grace is tested on floored minutes: `minutes > 10`. | 09:40:01 gives 10 minutes, so it is not > 10 and returns 0. R2 says it is late with 10. | Compare whole seconds: late if `seconds > 600`, then `seconds // 60`. |
| 6 | `compute_late_minutes` | Ignores overnight shifts and uses the punch-in's own date. | Shift 22:00-06:00, punch-in 02:00 gets measured from 22:00 of the same day (negative, so 0). | Uses the R1 attendance date (previous day for an early-morning overnight punch). |
| 7 | `compute_work_hours` | Python `round()` on a float: banker's rounding plus binary float error. No second truncation. | 3618 s = 1.005 h gives 1.0, expected 1.01. | `Decimal(seconds)/3600` quantized `ROUND_HALF_UP`. |
| 8 | `compute_overtime` | No 30-minute minimum. | Punch-out 15 minutes after shift end returns 15, expected 0. | Floor minutes, returned only if >= 30. |
| 9 | `compute_overtime` | Shift end is always on the record's date and the zone is only attached with `replace`. Overnight shifts are wrong. | 22:00-06:00 shift, out 06:40 next day gives about -1000 minutes, clamped to 0. Expected 40. | Overnight shifts end on the next day. |
| 10 | `create_employee` | Check-then-insert (`find_one`, then `insert_one`) with no unique index. | Parallel creates of the same `emp_code` both return 201. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 11 | `create_employee`, `EmployeeIn` | No validation at all (`emp_code` format, e-mail, `HH:MM`, `joined_on` date, `shift_start == shift_end`, lengths). | `joined_on: "2026-02-30"` or `shift_start: "9am"` is stored. | Pydantic patterns and length limits, a date validator, and an explicit 422 for equal shifts. |
| 12 | `create_employee` | `created_at` is `datetime.now()` (naive, server-local) and is returned as a datetime. | The response has an ISO string. The contract requires epoch milliseconds. | UTC time, returned as integer ms. |
| 13 | `list_employees` | `skip = page * page_size` skips the first page. | `page=1` never returns the first employees. | `(page - 1) * page_size`. |
| 14 | `list_employees` | `total` counts the whole collection, not the filtered set. | `?department=Ops` reports the total of all employees. | `count_documents(q)`. |
| 15 | `list_employees` | No sort, and `page`/`page_size` are unvalidated. | Order is arbitrary. `page=0` gives a negative skip (500). `page_size=100000` is accepted. | Sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100`, else 422. |
| 16 | `list_employees` | `if department:` treats `department=""` as no filter. | `?department=` returns everyone. | Filter whenever the parameter is given. |
| 17 | `punch_in` | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises `TypeError`. | POST with an unknown `emp_code` returns 500, expected 404. | Explicit 404 first. |
| 18 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` is server-local naive time. `ts.date()` is the local date, not the IST date. | On a UTC server a 01:00 IST punch-in is filed under the previous day. | Epoch ms to aware UTC; the date is computed in IST by R1, including the overnight rule. |
| 19 | `punch_in` | `if body.punched_at` treats 0 as missing, and nothing checks the unit, type or range. | `punched_at: 1783312500` (seconds) is accepted and lands in 1970. | `StrictInt` between 1e11 and 4102444800000, so seconds, floats and strings return 422. |
| 20 | `punch_in` | `status` is a free string. | `ABSENT`, `LEAVE` or `"banana"` can be punched in. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 21 | `punch_in` | Instants are not truncated to whole seconds. | 09:40:00.900 is stored with milliseconds. | `from_ms` floors to the second before anything else. |
| 22 | `punch_in` | Check-then-insert race. | Two simultaneous punch-ins both get 201 and create two records. | Unique (`emp_code`, `date`) index; the loser's `DuplicateKeyError` is 409. |
| 23 | `punch_in` | Response leaks the ObjectId as `id`. Instants are datetimes, not ms. History and fields come back as stored. | A client sees `id` and ISO strings. The contract says `_id` never appears. | `record_out()` is the single serialiser: no id, ms, legacy defaults. |
| 24 | `list_attendance` | `list(find(q))` loads the whole matching set and sorts and paginates in Python. | 100k records means a full scan, big memory and slow responses. | `find().sort().skip().limit()` backed by indexes; `count_documents` for the total. |
| 25 | `list_attendance` | Sorts by `date` only, so ties are in arbitrary order. The sort is not stable across pages. | Same-date rows can repeat or disappear between pages. | Sort `date` desc, `emp_code` asc (the contract's order). |
| 26 | `list_attendance` | No validation of dates, `status`, `page` or `page_size`. `date_from > date_to` is not rejected. | `page=0` gives a negative slice. `date_from=abc` is silently compared as a string. | Strict date type, enum `status`, bounded pagination, explicit 422 for an inverted range. |
| 27 | `list_attendance` | Returns `id` and raw datetimes. Legacy documents lacking `history`, `half_day`, etc. are returned as-is. | Missing keys, and the serialisation error is a 500. | `record_out()` as above. |

## Examined and judged not a defect

* `load_dotenv()` is fine. It does not override variables that already exist, so real environment variables win as required.
* The `IST` constant (`timezone(timedelta(hours=5, minutes=30))`) is correct. India has no daylight-saving time, so a fixed offset is safe.
* Popping `_id` from `doc` after `insert_one` works, because `insert_one` mutates the dict. I still removed the pattern: the serialisers build the response field by field so no internal field can leak.
* The 24-hour handling, `EMP\d{4,6}` and the other limits come from the contract, not from the starter.
* The helpers were not reusable as written: they are wrong in timezone, rounding, grace and overnight handling (4-9). I rewrote them rather than patching call sites.
