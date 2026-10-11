"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Everything lives in this one file, in this order: configuration, time helpers, business rules (R1-R5),
validation types and request models, serialisation, index creation, endpoints, analytics pipelines, explain.
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import AfterValidator, BaseModel, Field, StrictInt
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv()  # override=False: variables already in the environment take priority

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
MAX_SHIFT_SPAN = timedelta(hours=24)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(milliseconds=(ms // 1000) * 1000)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    """Seconds-truncated aware UTC view of a stored datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def ist_datetime(day: str, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def round_half_up(value, places: int):
    """Half-up rounding on the decimal value; round(.., 9) first removes float noise from $avg/$divide."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(float(value), 9))).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch belongs to the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, day: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - ist_datetime(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals, exact in Decimal."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only when at least 30. Overnight shifts end on the next day."""
    end = ist_datetime(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def closing_fields(punch_in: datetime, punch_out: Optional[datetime], day: str, emp: dict) -> dict:
    """Derived fields for a presence record, R2-R5."""
    out = {"late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"]),
           "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = Decimal(repr(wh)) < HALF_DAY_LIMIT
    return out


# --------------------------------------------------------------------------- #
# Validation types and request models
# --------------------------------------------------------------------------- #
def _valid_date(v: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError("must be YYYY-MM-DD")
    date.fromisoformat(v)
    return v


def _valid_month(v: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
        raise ValueError("must be YYYY-MM")
    date(int(v[:4]), int(v[5:7]), 1)
    return v


DateStr = Annotated[str, AfterValidator(_valid_date)]
MonthStr = Annotated[str, AfterValidator(_valid_month)]
Millis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PageParam = Annotated[int, Query(ge=1, le=1_000_000)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[Millis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[Millis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[Millis] = None
    punch_out: Optional[Millis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (BSON datetimes <-> epoch ms, legacy defaults, no _id)
# --------------------------------------------------------------------------- #
def _ms_or_value(v):
    return to_ms(v) if isinstance(v, datetime) else v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": to_ms(doc["created_at"]),
    }


def record_out(doc: dict) -> dict:
    history = [
        {"at": to_ms(h["at"]), "by": h.get("by"), "reason": h.get("reason"),
         "changes": {k: {"from": _ms_or_value(c.get("from")), "to": _ms_or_value(c.get("to"))}
                     for k, c in (h.get("changes") or {}).items()}}
        for h in (doc.get("history") or [])
    ]
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Indexes (idempotent, created at startup)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    # one record per employee and day; serves emp_code filters, punch-out lookup by day, $lookup joins, monthly summary
    try:
        logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    except OperationFailure:  # duplicates in pre-existing data must not keep the service down
        logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], name="emp_date")
    # global listing order (date desc, emp_code asc), date ranges, leaderboard and trend month scans
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # punch-out: most recent record of an employee with punch_in <= punched_at
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


# --------------------------------------------------------------------------- #
# Endpoints: system and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        employees.insert_one(doc)  # the unique index decides concurrent duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    q = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    if ts <= p_in or ts - p_in > MAX_SHIFT_SPAN:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    fields = closing_fields(p_in, ts, rec["date"], emp)
    fields["punch_out"] = ts
    # the punch_out: None guard makes the update atomic: only one of two simultaneous punch-outs matches
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cursor = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[DateStr, Path()], body: RegularizeIn):
    emp = get_employee(emp_code)
    for _ in range(5):  # optimistic concurrency: retry when another writer got in between read and write
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(rec, emp, body)
        history = rec.get("history") or []
        entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        guard = {
            "_id": rec["_id"], "status": rec["status"],
            "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history)]},
        }
        updated = logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "concurrent modification, please retry")


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate the correction, recompute derived fields (R2-R5) and describe what actually changed."""
    status = body.status or rec["status"]
    old_in = as_utc(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = as_utc(rec["punch_out"]) if rec.get("punch_out") else None
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"status {status} cannot carry punch times")
        new_in = new_out = None
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > MAX_SHIFT_SPAN):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours of it")
        derived = closing_fields(new_in, new_out, rec["date"], emp)
    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derived}
    if (status, new_in, new_out) == (rec["status"], old_in, old_out):
        raise HTTPException(422, "the request changes nothing")
    old = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    return new, changes


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
def _weekday(date_field: str = "$date") -> dict:
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]}


def _present_weight(weekday_only: bool) -> dict:
    """1 for a present record, 0.5 for a present half day, 0 otherwise (optionally only Mon-Fri)."""
    conds = [{"$in": ["$status", PRESENCE]}]
    if weekday_only:
        conds.append(_weekday())
    return {"$cond": [{"$and": conds}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}


def _flag(cond: dict) -> dict:
    return {"$cond": [cond, 1, 0]}


LATE_FLAG = _flag({"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]})


def _month_match(first: str, last: str) -> dict:
    return {"$gte": first, "$lte": last}


def working_days(first: str, last: str, joined_on: str) -> int:
    d, end, n = max(date.fromisoformat(first), date.fromisoformat(joined_on)), date.fromisoformat(last), 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": _month_match(first, last)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_weight(True)},
            "leave_days": {"$sum": _flag({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": LATE_FLAG},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    row = next(logs.aggregate(monthly_pipeline(emp_code, month)), {})
    wd = working_days(first, last, emp["joined_on"])
    present = row.get("present_days", 0)
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None,
    }


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so people without logs still count (R9); logs are joined per employee."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": _month_match(first, last)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_weight(True)},
            "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
            "hours_n": {"$sum": _flag(has_hours)},
            "late_count": {"$sum": LATE_FLAG},
            "late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": _flag({"$eq": ["$status", "LEAVE"]})},
            "on_duty_count": {"$sum": _flag({"$eq": ["$status", "ON_DUTY"]})},
        }},
    ]
    stat = lambda f: {"$sum": {"$ifNull": [f"$s.{f}", 0]}}  # noqa: E731
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {}]}}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": stat("present_days"), "hours_sum": stat("hours_sum"), "hours_n": stat("hours_n"),
            "late_count": stat("late_count"), "late_minutes": stat("late_minutes"),
            "leave_count": stat("leave_count"), "on_duty_count": stat("on_duty_count"),
        }},
        {"$sort": {"_id": 1}},
    ]


SUMMARY_HINT = {"department": 1, "joined_on": 1}


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = []
    for r in employees.aggregate(summary_pipeline(month, department), hint=SUMMARY_HINT):
        items.append({
            "department": r["_id"], "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None,
            "late_count": r["late_count"], "total_late_minutes": r["late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": _month_match(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_pipeline(department: str, start: str, end: str, codes: list[str]) -> list:
    lo = datetime.fromisoformat(start).replace(tzinfo=UTC)
    hi = datetime.fromisoformat(end).replace(tzinfo=UTC) + timedelta(days=1)  # densify upper bound is exclusive
    headcount = [
        {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
        {"$count": "n"},
    ]
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": _month_match(start, end)}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _present_weight(False)}, "late_count": {"$sum": LATE_FLAG}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
        }},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": headcount, "as": "h"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$h.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "moving": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving": 1}},
    ]


def trend_inputs(department: str, start: str, end: str) -> list[str]:
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    docs = list(employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    if not docs:
        raise HTTPException(404, "unknown department")
    return [d["emp_code"] for d in docs]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")],
                     to: Annotated[DateStr, Query()]):
    codes = trend_inputs(department, from_, to)
    items = []
    for r in logs.aggregate(trend_pipeline(department, from_, to, codes)):
        rate, moving = r["attendance_rate"], r["moving"]
        items.append({
            "date": r["date"], "is_working_day": r["is_working_day"], "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2), "late_count": r["late_count"],
            "attendance_rate": None if rate is None else round_half_up(rate, 4),
            "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


def _explain(command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    def aggregate(coll: str, pipeline: list, **extra) -> dict:
        return {"endpoint": endpoint, "collection": coll,
                "explain": _explain({"aggregate": coll, "pipeline": pipeline, "cursor": {}, **extra})}

    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        cmd = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
               "skip": (page - 1) * page_size, "limit": page_size}
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _explain(cmd)}
    if endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        return aggregate("attendance_logs", monthly_pipeline(emp_code, month))
    if endpoint == "department_summary":
        _require(month=month)
        return aggregate("employees", summary_pipeline(month, department), hint=SUMMARY_HINT)
    if endpoint == "late_leaderboard":
        _require(month=month)
        return aggregate("attendance_logs", leaderboard_pipeline(month, limit, department))
    _require(department=department, **{"from": from_}, to=to)
    codes = trend_inputs(department, from_, to)
    return aggregate("attendance_logs", trend_pipeline(department, from_, to, codes))
