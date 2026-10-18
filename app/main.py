from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, SessionLocal, engine
from .models import Snapshot


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
