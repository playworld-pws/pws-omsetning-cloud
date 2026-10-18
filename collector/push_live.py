from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


CLOUD_URL = os.environ.get("PWS_CLOUD_URL", "").rstrip("/")
INGEST_KEY = os.environ.get("PWS_INGEST_KEY", "")
INTERVAL = int(os.environ.get("PWS_PUSH_INTERVAL", "30"))

FILES = {
    "sorlandet": os.environ.get("PWS_SORLANDET_JSON", ""),
    "triaden": os.environ.get("PWS_TRIADEN_JSON", ""),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upload(park: str, raw: bytes) -> None:
    # Verify valid JSON before upload.
    json.loads(raw.decode("utf-8-sig"))

    req = urllib.request.Request(
        f"{CLOUD_URL}/api/ingest/{park}",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-PWS-Ingest-Key": INGEST_KEY,
        },
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"HTTP {response.status}")


def main() -> None:
    if not CLOUD_URL:
        raise SystemExit("Mangler PWS_CLOUD_URL")
    if not INGEST_KEY:
        raise SystemExit("Mangler PWS_INGEST_KEY")

    last_hash: dict[str, str] = {}

    print("PWS Cloud collector startet.")
    print(f"Sky: {CLOUD_URL}")
    print(f"Intervall: {INTERVAL} sek")

    while True:
        for park, filename in FILES.items():
            if not filename:
                continue

            path = Path(filename)
            if not path.exists():
                print(f"[{park}] Fant ikke {path}")
                continue

            try:
                raw = path.read_bytes()
                current = digest(raw)

                if last_hash.get(park) != current:
                    upload(park, raw)
                    last_hash[park] = current
                    print(f"[{park}] Lastet opp {path.name}")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                print(f"[{park}] Feil: {exc}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
