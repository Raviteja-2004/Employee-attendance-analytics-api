# Employee Attendance & Analytics API

A robust, production-grade FastAPI and MongoDB service designed for tracking employee attendance, managing shift-based punch lifecycles, recording regularization audit trails, and generating high-performance department and organizational analytics.

The service strictly adheres to the OpenAPI 3.0.3 specification and business rules (R1–R10).

---

## Features

### 1. Attendance & Shift Lifecycle
- **Punch-In (`POST /attendance/punch-in`)**: Records punch-in timestamps in epoch milliseconds. Automatically resolves overnight shifts (R1) across midnight. Enforces atomic uniqueness on `(emp_code, date)` to prevent duplicate punch-ins under concurrent load without race conditions.
- **Punch-Out (`POST /attendance/punch-out`)**: Calculates actual worked hours with half-up rounding (R4), late arrival minutes with a 10-minute grace window (R2), overtime duration with a 30-minute minimum threshold (R3), and half-day status (R5).
- **Regularization & Audit Trail (`PATCH /attendance/{emp_code}/{date}`)**: Enables authorized attendance corrections with optimistic concurrency control and an immutable audit log (`history`) tracking all field modifications (`from` / `to`), timestamps, author, and reasons.
- **Attendance Records (`GET /attendance`)**: Filter by employee, date range, or status with database-level pagination and stable compound index ordering (`date DESC, emp_code ASC`).

### 2. Analytics & Reporting
- **Monthly Employee Summary (`GET /analytics/employees/{emp_code}/monthly`)**: Comprehensive aggregation reporting expected working days, actual present days (PRESENT, WFH, ON_DUTY), leaves, absences, half-days, total work hours, late minutes, and overtime.
- **Department Summary (`GET /analytics/departments/summary`)**: Headcount-safe two-pass aggregation computing attendance percentages, total overtime, and late occurrences across departments, accurately accounting for employees with zero logs.
- **Late Leaderboard (`GET /analytics/leaderboard/late`)**: Standard competition ranking (1224) based on cumulative late arrival minutes, handling ties cleanly and respecting pagination limits.
- **Department 7-Day Trend (`GET /analytics/departments/{department}/trend`)**: Daily moving average with calendar gap-filling to ensure consecutive date series even on weekends or zero-activity dates.

### 3. Reliability & Index Optimization
- **Idempotent Startup Indexing**: Six indexes created automatically on application startup to enforce data integrity and guarantee index-backed queries (`IXSCAN`).
- **Health & Diagnostics**: Active MongoDB ping healthcheck (`GET /health`) and execution stats verification via `GET /admin/explain/{endpoint}`.

---

## Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/) (Python 3.10+)
- **Server**: [Uvicorn](https://www.uvicorn.org/)
- **Database**: [MongoDB](https://www.mongodb.com/) (6.0+ supported, 7.0 compatible) via [PyMongo](https://pymongo.readthedocs.io/)
- **Validation**: [Pydantic v2](https://docs.pydantic.dev/)

---

## Project Structure

```text
.
├── app/
│   ├── __init__.py        # Package initializer
│   └── main.py            # Complete application implementation (all routes & logic)
├── sample_data/           # Sample MongoDB Extended JSON documents
├── DATA_MODEL.md          # Database schema and document design specification
├── DECISIONS.md           # Architectural decisions, index rationale, and race condition design
├── openapi.yaml           # OpenAPI 3.0.3 specification contract and business rules (R1-R10)
├── README.md              # Project documentation and setup guide
├── requirements.txt       # Project dependencies
├── REVIEW.md              # Defect review and bug fix register (17 defects resolved)
└── sample_seed.py         # Sample database seeding script
```

---

## Getting Started

### 1. Prerequisites
- Python 3.10 or higher
- MongoDB instance (local or MongoDB Atlas)

### 2. Environment Configuration
Create a `.env` file in the root directory (or set environment variables):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Installation & Run
```bash
# Create and activate virtual environment
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate
# On Windows:
# .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# (Optional) Seed sample data
python sample_seed.py

# Start the API server
uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation will be available at `http://localhost:8000/docs` (Swagger UI) and `http://localhost:8000/redoc`.

---

## Architectural & Review Documentation

- **[DECISIONS.md](file:///c:/Users/manoj/Downloads/SDT_candidate_kit/candidate_kit/DECISIONS.md)**: Details indexing strategy, atomic race condition handling on punch-in, standard competition tie-breaker rules, and headcount aggregation guarantees.
- **[REVIEW.md](file:///c:/Users/manoj/Downloads/SDT_candidate_kit/candidate_kit/REVIEW.md)**: Line-by-line audit documenting all 17 defects diagnosed and resolved from the initial starter code.
