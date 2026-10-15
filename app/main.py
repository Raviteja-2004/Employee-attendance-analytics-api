"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import math
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Index creation at startup (idempotent)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    # Unique employee code — enforces concurrent-safe duplicate rejection
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # For headcount queries (analytics joins)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

    # Natural key for attendance: (emp_code, date) must be unique
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # list_attendance sort: date desc, emp_code asc
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # Analytics date-range queries
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    # Punch-out lookup: find open record by emp_code + punch_in
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)]
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def half_up(value: float, decimals: int) -> float:
    """Round half-up (not banker's rounding) to the given decimal places."""
    multiplier = 10 ** decimals
    return math.floor(value * multiplier + 0.5) / multiplier


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> Optional[int]:
    """datetime → epoch milliseconds. Treats naive datetimes as UTC (PyMongo default)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    return (h_e * 60 + m_e) <= (h_s * 60 + m_s)


def attendance_date_for_punch(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: IST calendar date for a punch-in instant.
    For overnight shifts a punch-in whose IST time is *before* shift_end
    belongs to the shift that started the previous IST day.
    """
    punch_ist = punch_utc.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        h_e, m_e = map(int, shift_end.split(":"))
        punch_minutes = punch_ist.hour * 60 + punch_ist.minute
        if punch_minutes < h_e * 60 + m_e:
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def calc_late_minutes(punch_utc: datetime, shift_start: str, att_date: str) -> int:
    """R2: Grace = 10 min. Late only when strictly > 10 min after shift_start."""
    h, m = map(int, shift_start.split(":"))
    d = date_type.fromisoformat(att_date)
    shift_start_dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    total_min = int((punch_utc - shift_start_dt).total_seconds() / 60)
    return total_min if total_min > 10 else 0


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    return half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def calc_overtime(punch_out_utc: datetime, shift_end: str, att_date: str, overnight: bool) -> int:
    """R3: floor minutes after shift_end, only when >= 30; overnight shift_end is next day."""
    h, m = map(int, shift_end.split(":"))
    d = date_type.fromisoformat(att_date)
    end_date = d + timedelta(days=1) if overnight else d
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, h, m, 0, tzinfo=IST)
    diff = int((punch_out_utc - shift_end_dt).total_seconds() / 60)
    return diff if diff >= 30 else 0


def _ensure_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """Make a naive datetime timezone-aware (UTC). Pass-through if already aware or None."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def serialize_log(doc: dict) -> dict:
    """Strip _id and convert datetimes → epoch ms throughout a log document."""
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    for entry in doc["history"]:
        entry["at"] = dt_to_ms(entry["at"])
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                entry["changes"][field]["from"] = dt_to_ms(entry["changes"][field]["from"])
                entry["changes"][field]["to"] = dt_to_ms(entry["changes"][field]["to"])
    return doc


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


def month_range(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM string."""
    year, mon = map(int, month.split("-"))
    first = date_type(year, mon, 1)
    last = date_type(year + 1, 1, 1) - timedelta(days=1) if mon == 12 \
        else date_type(year, mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_working_days(start_str: str, end_str: str) -> int:
    """Count Mon–Fri days between start_str and end_str inclusive."""
    start = date_type.fromisoformat(start_str)
    end = date_type.fromisoformat(end_str)
    count, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def _validate_ms(v):
    if v is not None and (v < 100_000_000_000 or v > 4_102_444_800_000):
        raise ValueError("must be epoch milliseconds (>= 100000000000)")
    return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_ms(cls, v):
        return _validate_ms(v)


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_ms(cls, v):
        return _validate_ms(v)


class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_ms(cls, v):
        return _validate_ms(v)

    @model_validator(mode="after")
    def absent_leave_rules(self):
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "Cannot supply punch_in or punch_out alongside ABSENT/LEAVE status"
                )
        return self


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for emp in items:
        emp["created_at"] = dt_to_ms(emp["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {body.emp_code} not found")

    ts_utc = ms_to_dt(body.punched_at) if body.punched_at else \
        datetime.now(tz=timezone.utc).replace(microsecond=0)

    att_date = attendance_date_for_punch(ts_utc, emp["shift_start"], emp["shift_end"])
    late = calc_late_minutes(ts_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return serialize_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {body.emp_code} not found")

    ts_utc = ms_to_dt(body.punched_at) if body.punched_at else \
        datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Most recent open record whose punch_in <= ts_utc (handles overnight)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_utc},
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "No open punch-in found for this employee")

    pi = _ensure_tz(record["punch_in"])
    if ts_utc <= pi:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts_utc - pi).total_seconds() > 86400:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    overnight = is_overnight(emp["shift_start"], emp["shift_end"])
    wh = calc_work_hours(pi, ts_utc)
    ot = calc_overtime(ts_utc, emp["shift_end"], record["date"], overnight)
    hd = wh < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},   # guard against concurrent punch-out
        {"$set": {"punch_out": ts_utc, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, "Record was already punched out by a concurrent request")
    return serialize_log(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {"items": [serialize_log(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code} not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, f"No attendance record for {emp_code} on {date}")

    # Resolve intended final state
    new_status = body.status if body.status is not None else record["status"]
    old_pi = _ensure_tz(record.get("punch_in"))
    old_po = _ensure_tz(record.get("punch_out"))

    new_pi = ms_to_dt(body.punch_in) if body.punch_in is not None else old_pi
    new_po = ms_to_dt(body.punch_out) if body.punch_out is not None else old_po

    if new_status in ("ABSENT", "LEAVE"):
        new_pi = None
        new_po = None
    else:
        if new_pi is None:
            raise HTTPException(422, "A presence status requires punch_in")
        # punch_in must stay on the record's attendance date (R1)
        if attendance_date_for_punch(new_pi, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must remain on the record's attendance date")
        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields from final state
    overnight = is_overnight(emp["shift_start"], emp["shift_end"])
    if new_status in ("ABSENT", "LEAVE"):
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    elif new_po is not None:
        new_late = calc_late_minutes(new_pi, emp["shift_start"], date)
        new_wh = calc_work_hours(new_pi, new_po)
        new_ot = calc_overtime(new_po, emp["shift_end"], date, overnight)
        new_hd = new_wh < 4.50
    else:
        new_late = calc_late_minutes(new_pi, emp["shift_start"], date)
        new_wh, new_ot, new_hd = None, 0, False

    old_status = record["status"]
    old_late = record.get("late_minutes", 0)
    old_wh = record.get("work_hours")
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    # Build changes — only fields that actually changed
    changes: dict = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if dt_to_ms(new_pi) != dt_to_ms(old_pi):
        changes["punch_in"] = {"from": old_pi, "to": new_pi}
    if dt_to_ms(new_po) != dt_to_ms(old_po):
        changes["punch_out"] = {"from": old_po, "to": new_po}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: only update if history length hasn't changed
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(record.get("history", []))},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, "Concurrent edit detected — please retry")
    return serialize_log(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee {emp_code} not found")

    first_day, last_day = month_range(month)

    # R7: working days start from max(first_day, joined_on)
    effective_start = max(first_day, emp["joined_on"])
    working_days = count_working_days(effective_start, last_day) if effective_start <= last_day else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        # dayOfWeek: 1=Sun, 2=Mon … 6=Fri, 7=Sat  →  Mon-Fri = [2,3,4,5,6]
        {"$addFields": {
            "is_weekday": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0.0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    r = rows[0] if rows else {}

    present_days = half_up(r.get("present_days", 0), 2)
    att_pct = half_up(present_days / working_days * 100, 2) if working_days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = month_range(month)

    # Headcount per department: employees with joined_on <= last_day (R9)
    emp_filter: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_filter["department"] = department

    hc_rows = list(db.employees.aggregate([
        {"$match": emp_filter},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
    ]))
    headcounts = {r["_id"]: r["headcount"] for r in hc_rows}
    if not headcounts:
        return {"month": month, "items": []}

    # Log aggregation grouped by department (via $lookup)
    log_pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        *([{"$match": {"emp.department": department}}] if department else []),
        {"$addFields": {
            "dept": "$emp.department",
            "is_weekday": {"$in": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                [2, 3, 4, 5, 6],
            ]},
        }},
        {"$group": {
            "_id": "$dept",
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0.0,
            ]}},
            "wh_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                ]},
                "$work_hours", 0.0,
            ]}},
            "wh_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                ]},
                1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    log_stats = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept in sorted(headcounts):
        s = log_stats.get(dept, {})
        wh_count = s.get("wh_count", 0)
        items.append({
            "department": dept,
            "headcount": headcounts[dept],
            "present_days": half_up(s.get("present_days", 0), 2),
            "avg_work_hours": half_up(s["wh_sum"] / wh_count, 2) if wh_count else None,
            "late_count": s.get("late_count", 0),
            "total_late_minutes": s.get("total_late_minutes", 0),
            "leave_count": s.get("leave_count", 0),
            "on_duty_count": s.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = month_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        *([{"$match": {"emp.department": department}}] if department else []),
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking — ties share a rank, next rank is skipped
    items = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        row["rank"] = rank
        if row["rank"] <= limit:
            items.append(row)
        else:
            break   # rows are sorted desc, so once rank > limit we're done

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from"),
    to: str = Query(),
):
    if to < from_:
        raise HTTPException(422, "'to' must not be before 'from'")
    from_date = date_type.fromisoformat(from_)
    to_date = date_type.fromisoformat(to)
    if (to_date - from_date).days > 92:
        raise HTTPException(422, "Range must not exceed 92 days")

    dept_employees = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0},
    ))
    if not dept_employees:
        raise HTTPException(404, f"Department '{department}' not found")

    # Aggregate per-day stats from logs
    log_pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0.0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
    ]
    log_by_date = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    # Gap-fill every calendar day in the range (R: use $densify in aggregation —
    # here we fill in Python as the range is bounded to 92 days max)
    items = []
    cur = from_date
    while cur <= to_date:
        cur_str = cur.isoformat()
        is_wd = cur.weekday() < 5
        hc = sum(1 for e in dept_employees if e["joined_on"] <= cur_str)
        log = log_by_date.get(cur_str, {})
        pc = half_up(log.get("present_count", 0), 2)
        lc = log.get("late_count", 0)
        att_rate = half_up(pc / hc, 4) if (is_wd and hc > 0) else None
        items.append({
            "date": cur_str,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": pc,
            "late_count": lc,
            "attendance_rate": att_rate,
            "moving_avg_7d": None,
        })
        cur += timedelta(days=1)

    # 7-day moving average of non-null attendance_rate (within the requested range only)
    for i, row in enumerate(items):
        window = [
            items[j]["attendance_rate"]
            for j in range(max(0, i - 6), i + 1)
            if items[j]["attendance_rate"] is not None
        ]
        row["moving_avg_7d"] = half_up(sum(window) / len(window), 4) if window else None

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin / Explain
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    def run_explain(collection_name: str, cmd: dict) -> dict:
        return db.command("explain", cmd, verbosity="executionStats")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain = run_explain("attendance_logs", {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first_day, last_day = month_range(month)
        explain = run_explain("attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}}],
            "cursor": {},
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first_day, last_day = month_range(month)
        explain = run_explain("attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": {"date": {"$gte": first_day, "$lte": last_day}}}],
            "cursor": {},
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first_day, last_day = month_range(month)
        explain = run_explain("attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}}],
            "cursor": {},
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required")
        explain = run_explain("attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": {"date": {"$gte": from_, "$lte": to}}}],
            "cursor": {},
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
