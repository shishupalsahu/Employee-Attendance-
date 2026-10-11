# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicate guard, lookups, sorting), and `(department, joined_on)` for headcount and department filters. `attendance_logs`: unique `(emp_code, date)` (one record per day, employee filters, joins), `(date desc, emp_code)` (the listing order, month scans), `(status, date desc, emp_code)` for status filters, and `(emp_code, punch_in desc)` for punch-out. I rejected a covering index on `late_minutes` for the leaderboard: it adds write cost for one monthly report.

2. **Punch-in race.** Both requests find the employee, build the same record and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.

3. **Ties.** Ranking uses `$rank` in `$setWindowFields`, so ties share a rank and the next is skipped. `limit` is applied to the rank, so everyone tied at or below `limit` is returned, even if that is more than `limit` rows.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and `$lookup`s each person's logs. A person with no logs gets an empty result and still counts as 1.

5. **Change for 100x data.** Pre-aggregate monthly numbers per employee when records are written or in a nightly job, so reports stop scanning raw days, and page the list by cursor (last date and emp_code) instead of `skip`.
