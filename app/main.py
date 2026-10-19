from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, SessionLocal, engine
from .models import DailyProduct, ProductHistoryDay, Snapshot


OSLO = ZoneInfo("Europe/Oslo")
ALLOWED_PARKS = {"sorlandet", "triaden"}

app = FastAPI(
    title="PWS Omsetning Cloud",
    version="1.0.0",
    description="Cloud API for Playworld Sørlandet og Playworld Triaden",
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def normalize_park(park: str) -> str:
    value = park.strip().lower()
    if value not in ALLOWED_PARKS:
        raise HTTPException(status_code=400, detail="Ukjent park")
    return value


def require_app_key(x_pws_key: str | None = Header(default=None)) -> None:
    if not x_pws_key or x_pws_key != settings.pws_app_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_ingest_key(
    x_pws_ingest_key: str | None = Header(default=None),
) -> None:
    if not x_pws_ingest_key or x_pws_ingest_key != settings.pws_ingest_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def payload_datetime(payload: dict[str, Any]) -> datetime:
    raw = payload.get("generated_at") or payload.get("generatedAt")
    if isinstance(raw, str):
        raw = raw.strip()
        formats = (
            "%d.%m.%Y %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S%z",
        )
        for fmt in formats:
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=OSLO) if dt.tzinfo is None else dt
            except ValueError:
                pass
    return datetime.now(OSLO)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "pws-omsetning-cloud",
        "time": datetime.now(OSLO).isoformat(),
    }


@app.get("/api/parks", dependencies=[Depends(require_app_key)])
def parks() -> dict[str, Any]:
    return {
        "parks": [
            {"id": "sorlandet", "name": "Playworld Sørlandet"},
            {"id": "triaden", "name": "Playworld Triaden"},
        ]
    }


@app.post("/api/ingest/{park}", dependencies=[Depends(require_ingest_key)])
def ingest(
    park: str,
    payload: dict[str, Any],
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    park = normalize_park(park)
    generated_at = payload_datetime(payload)

    snapshot = Snapshot(
        park=park,
        business_date=generated_at.astimezone(OSLO).date(),
        generated_at=generated_at,
        payload=payload,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    return {
        "ok": True,
        "park": park,
        "snapshot_id": snapshot.id,
        "business_date": snapshot.business_date.isoformat(),
        "generated_at": snapshot.generated_at.isoformat(),
    }


@app.get("/api/dashboard", dependencies=[Depends(require_app_key)])
def dashboard(
    park: str = Query(...),
    date_value: date | None = Query(default=None, alias="date"),
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    park = normalize_park(park)
    target_date = date_value or datetime.now(OSLO).date()

    stmt = (
        select(Snapshot)
        .where(
            Snapshot.park == park,
            Snapshot.business_date == target_date,
        )
        .order_by(desc(Snapshot.generated_at), desc(Snapshot.id))
        .limit(1)
    )
    snapshot = db.execute(stmt).scalars().first()

    if snapshot is None:
        raise HTTPException(status_code=404, detail="Ingen data for valgt dato")

    return snapshot.payload


@app.get("/api/dates", dependencies=[Depends(require_app_key)])
def dates(
    park: str = Query(...),
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    park = normalize_park(park)

    stmt = (
        select(Snapshot.business_date)
        .where(Snapshot.park == park)
        .distinct()
        .order_by(desc(Snapshot.business_date))
    )
    values = db.execute(stmt).scalars().all()

    return {
        "park": park,
        "dates": [value.isoformat() for value in values],
    }


def _product_key(name: str) -> str:
    return " ".join(str(name or "").strip().casefold().split())[:255]


def _period_bounds(target: date, period: str) -> tuple[date, date]:
    value = period.strip().lower()
    if value == "day":
        return target, target
    if value == "week":
        start = target - timedelta(days=target.weekday())
        return start, start + timedelta(days=6)
    if value == "month":
        start = target.replace(day=1)
        last_day = monthrange(target.year, target.month)[1]
        return start, target.replace(day=last_day)
    raise HTTPException(status_code=400, detail="period må være day, week eller month")


@app.post("/api/products/ingest/{park}", dependencies=[Depends(require_ingest_key)])
def ingest_products(
    park: str,
    payload: dict[str, Any],
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    park = normalize_park(park)

    raw_date = payload.get("business_date") or payload.get("date")
    try:
        business_date = date.fromisoformat(str(raw_date))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Ugyldig business_date") from exc

    raw_products = payload.get("products")
    if not isinstance(raw_products, list):
        raise HTTPException(status_code=400, detail="products må være en liste")

    # Each upload is the complete current picture for that park/date.
    db.execute(
        delete(DailyProduct).where(
            DailyProduct.park == park,
            DailyProduct.business_date == business_date,
        )
    )

    inserted = 0
    for raw in raw_products:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue

        key = _product_key(name)
        try:
            revenue = float(raw.get("revenue") or 0)
        except (TypeError, ValueError):
            revenue = 0.0

        quantity_raw = raw.get("quantity")
        try:
            quantity = None if quantity_raw is None else float(quantity_raw)
        except (TypeError, ValueError):
            quantity = None

        db.add(
            DailyProduct(
                park=park,
                business_date=business_date,
                product_key=key,
                product_name=name[:255],
                quantity=quantity,
                revenue=round(revenue, 2),
            )
        )
        inserted += 1

    source = str(payload.get("source") or "live").strip().lower()[:32] or "live"
    history_day = db.execute(
        select(ProductHistoryDay).where(
            ProductHistoryDay.park == park,
            ProductHistoryDay.business_date == business_date,
        )
    ).scalars().first()
    if history_day is None:
        history_day = ProductHistoryDay(
            park=park,
            business_date=business_date,
            product_count=inserted,
            source=source,
        )
        db.add(history_day)
    else:
        history_day.product_count = inserted
        history_day.source = source

    db.commit()
    return {
        "ok": True,
        "park": park,
        "business_date": business_date.isoformat(),
        "products": inserted,
        "source": source,
    }


@app.get("/api/products", dependencies=[Depends(require_app_key)])
def products(
    park: str = Query(...),
    period: str = Query(default="day"),
    date_value: date | None = Query(default=None, alias="date"),
    sort: str = Query(default="revenue"),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    park = normalize_park(park)
    target = date_value or datetime.now(OSLO).date()
    start_date, end_date = _period_bounds(target, period)

    rows = db.execute(
        select(DailyProduct).where(
            DailyProduct.park == park,
            DailyProduct.business_date >= start_date,
            DailyProduct.business_date <= end_date,
        )
    ).scalars().all()

    grouped: dict[str, dict[str, Any]] = {}
    covered_dates: set[str] = set()

    for row in rows:
        covered_dates.add(row.business_date.isoformat())
        current = grouped.setdefault(
            row.product_key,
            {
                "name": row.product_name,
                "revenue": 0.0,
                "quantity": 0.0,
                "quantity_complete": True,
            },
        )
        current["revenue"] += float(row.revenue or 0)
        if row.quantity is None:
            current["quantity_complete"] = False
        else:
            current["quantity"] += float(row.quantity)

    result: list[dict[str, Any]] = []
    for item in grouped.values():
        result.append(
            {
                "name": item["name"],
                "revenue": round(item["revenue"], 2),
                "quantity": (
                    round(item["quantity"], 2)
                    if item["quantity_complete"]
                    else None
                ),
                "quantity_complete": bool(item["quantity_complete"]),
            }
        )

    sort_value = sort.strip().lower()
    if sort_value == "quantity":
        result.sort(
            key=lambda x: (
                x["quantity"] is not None,
                float(x["quantity"] or 0),
                float(x["revenue"] or 0),
            ),
            reverse=True,
        )
    elif sort_value == "revenue":
        result.sort(key=lambda x: float(x["revenue"] or 0), reverse=True)
    else:
        raise HTTPException(status_code=400, detail="sort må være revenue eller quantity")

    return {
        "ok": True,
        "park": park,
        "period": period.strip().lower(),
        "date": target.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "sort": sort_value,
        "limit": limit,
        "days_with_product_data": len(covered_dates),
        "covered_dates": sorted(covered_dates),
        "products": result[:limit],
    }


@app.get("/api/products/next-missing-date", dependencies=[Depends(require_app_key)])
def next_missing_product_date(
    park: str = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    db: Session = Depends(db_session),
) -> dict[str, Any]:
    park = normalize_park(park)
    if end < start:
        raise HTTPException(status_code=400, detail="end må være lik eller etter start")

    covered = set(
        db.execute(
            select(ProductHistoryDay.business_date).where(
                ProductHistoryDay.park == park,
                ProductHistoryDay.business_date >= start,
                ProductHistoryDay.business_date <= end,
            )
        ).scalars().all()
    )

    cursor = start
    while cursor <= end:
        if cursor not in covered:
            return {
                "ok": True,
                "park": park,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "next_missing_date": cursor.isoformat(),
                "complete": False,
                "covered_days": len(covered),
            }
        cursor += timedelta(days=1)

    return {
        "ok": True,
        "park": park,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "next_missing_date": None,
        "complete": True,
        "covered_days": len(covered),
    }
