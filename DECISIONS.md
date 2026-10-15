# DECISIONS.md

1. **Indexes.**
   I created six indexes at startup: a unique index on `employees.emp_code` (enforces the business-key uniqueness guarantee under concurrent inserts); a compound index on `employees(department, joined_on)` for headcount aggregations; a unique compound index on `attendance_logs(emp_code, date)` (the natural key — prevents double punch-in atomically); a compound index on `attendance_logs(date DESC, emp_code ASC)` for the list endpoint's required sort without a sort-in-memory; a compound index on `attendance_logs(date, status)` for the analytics date-range filters; and a compound index on `attendance_logs(emp_code, punch_in)` for the punch-out lookup. I considered a single-field index on `attendance_logs.emp_code` but dropped it because the compound `(emp_code, date)` already covers point-lookups by employee code.

2. **Punch-in race.**
   Both requests arrive, both fetch the employee record, both compute the attendance date, both try `insert_one`. MongoDB evaluates the unique index on `(emp_code, date)` at the storage layer — only one insert wins; the other raises `DuplicateKeyError`, which my code catches and turns into a 409. No application-level lock or pre-check is needed.

3. **Ties.**
   If two employees have identical `total_late_minutes` and both fall at the cutoff position, both share the same rank (standard competition ranking). The leaderboard includes every row whose `rank <= limit`, so when two employees tie at rank `limit`, both appear. The response can therefore have more than `limit` rows — as specified in the API contract.

4. **Headcount.**
   The department summary first aggregates headcount directly from the `employees` collection using `joined_on <= last_day_of_month`, producing a `{department: headcount}` map. Then it runs the log aggregation separately. When merging results, every department present in the headcount map is included in the output, even if it has zero matching log rows (it will simply show all zeros for the stats). This guarantees employees with no logs are still counted.

5. **One thing I'd change at 100× the data.**
   The `department_trend` endpoint computes per-day headcount by iterating over `dept_employees` in Python for each calendar day (O(employees × days)). At 100× scale that inner loop becomes expensive. I'd replace it with a MongoDB `$densify` + `$lookup` aggregation that computes headcount inside the database, keeping network traffic and Python work constant regardless of employee count.
