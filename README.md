# PWS Omsetning Cloud

Skybackend for Playworld Sørlandet og Playworld Triaden.

## Arkitektur

```
Wallmob / NordPay
      ↓
lokal PWS collector (midlertidig)
      ↓ HTTPS
PWS Cloud API på Render
      ↓
PostgreSQL
      ↓
iPhone / Android / webdashboard
```

Denne første skyversjonen gjør mobilappen uavhengig av lokalt Wi‑Fi. Den eksisterende PC-løsningen kan foreløpig fortsette å hente data fra Wallmob/NordPay og sende ferdig JSON til skyen. Senere kan collector-delen flyttes helt til skyen hvis autentiseringen mot leverandørene fungerer stabilt der.

## Parker

- `sorlandet`
- `triaden`

## API

- `GET /health`
- `GET /api/parks`
- `GET /api/dashboard?park=sorlandet&date=2026-10-18`
- `GET /api/dates?park=sorlandet`
- `POST /api/ingest/sorlandet`
- `POST /api/ingest/triaden`

Lesing krever header `X-PWS-Key`.
Ingest krever header `X-PWS-Ingest-Key`.

## Render

Repoet inneholder `render.yaml`. I Render kan du velge **New → Blueprint** og koble dette repoet. Render oppretter webservice + PostgreSQL og ber om hemmelige nøkler.

Ikke legg passord, cookies, `PlayworldBrowserProfile` eller ekte API-nøkler i GitHub.

## Lokal collector

`collector/push_live.py` kan sende eksisterende:
- `pws_live_data.json` → Sørlandet
- `pws_triaden_live_data.json` → Triaden

Sett miljøvariablene:
- `PWS_CLOUD_URL`
- `PWS_INGEST_KEY`
- `PWS_SORLANDET_JSON`
- `PWS_TRIADEN_JSON`

Kjør:

```bash
python collector/push_live.py
```

Den sjekker filene hvert 30. sekund og laster bare opp når innholdet har endret seg.
