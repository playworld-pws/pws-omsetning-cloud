from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright


OSLO = ZoneInfo("Europe/Oslo")

CLOUD_URL = os.getenv("PWS_CLOUD_URL", "https://pws-omsetning-api.onrender.com").rstrip("/")
APP_KEY = os.getenv("PWS_APP_KEY", "")
INGEST_KEY = os.getenv("PWS_INGEST_KEY", "")
INTERVAL_SECONDS = max(30, int(os.getenv("PWS_COLLECT_INTERVAL", "60")))

WALLMOB_LOGIN_URL = os.getenv(
    "WALLMOB_LOGIN_URL", "https://wbo-etail.wallmob.com/login"
)
WALLMOB_HOME_URL = os.getenv(
    "WALLMOB_HOME_URL", "https://wbo-etail.wallmob.com/home"
)

SORLANDET_WALLMOB_USER = os.getenv("WALLMOB_SORLANDET_USERNAME", "")
SORLANDET_WALLMOB_PASSWORD = os.getenv("WALLMOB_SORLANDET_PASSWORD", "")
SORLANDET_WALLMOB_TYPE = os.getenv("WALLMOB_SORLANDET_LOGIN_TYPE", "user")

TRIADEN_WALLMOB_USER = os.getenv("WALLMOB_TRIADEN_USERNAME", "")
TRIADEN_WALLMOB_PASSWORD = os.getenv("WALLMOB_TRIADEN_PASSWORD", "")
TRIADEN_WALLMOB_TYPE = os.getenv("WALLMOB_TRIADEN_LOGIN_TYPE", "user")

NORDPAY_USER = os.getenv("NORDPAY_USERNAME", "")
NORDPAY_PASSWORD = os.getenv("NORDPAY_PASSWORD", "")
NORDPAY_BASE_URL = os.getenv("NORDPAY_BASE_URL", "https://admin.nordpay.no").rstrip("/")

PARKS = {
    "sorlandet": {
        "name": "Playworld Sørlandet",
        "lat": 58.1467,
        "lon": 7.9956,
    },
    "triaden": {
        "name": "Playworld Triaden",
        "lat": 59.9280,
        "lon": 10.9530,
    },
}


def parse_number(value: Any) -> float | None:
    if value is None:
        return None

    original = str(value).strip()
    if not original:
        return None

    negative = original.startswith("-") or (
        original.startswith("(") and original.endswith(")")
    )

    s = re.sub(r"(?i)\bNOK\b|\bkr\b", "", original)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r",-?$", ",00", s)
    s = re.sub(r"\.-?$", ".00", s)
    s = s.replace("-", "")
    s = re.sub(r"[^0-9,.]", "", s)

    if not s:
        return None

    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")

    try:
        n = float(s)
    except ValueError:
        return None

    return -abs(n) if negative else abs(n)


def after_label(text: str, labels: list[str]) -> float | None:
    for label in labels:
        m = re.search(
            rf"{re.escape(label)}[\s\r\n:]{{0,30}}([0-9][0-9\s\.,-]*)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            value = parse_number(m.group(1))
            if value is not None:
                return value
    return None


async def fill_first(page: Page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.fill(value)
                return True
        except Exception:
            pass
    return False


async def click_text_first(page: Page, names: list[str]) -> bool:
    for name in names:
        try:
            loc = page.get_by_role(
                "button", name=re.compile(re.escape(name), re.IGNORECASE)
            ).first
            if await loc.count() and await loc.is_visible():
                await loc.click()
                return True
        except Exception:
            pass
    return False


async def login_wallmob(
    context: BrowserContext,
    username: str,
    password: str,
    login_type: str,
) -> Page:
    page = await context.new_page()
    await page.goto(WALLMOB_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(1200)

    if login_type.lower().startswith("master"):
        try:
            await page.get_by_text("MASTER USER", exact=False).first.click()
        except Exception:
            pass
    else:
        try:
            await page.get_by_text("USER", exact=False).first.click()
        except Exception:
            pass

    ok_user = await fill_first(
        page,
        [
            'input[placeholder*="username" i]',
            'input[name*="username" i]',
            'input[type="email"]',
            'input[placeholder*="e-mail" i]',
            'input[placeholder*="email" i]',
        ],
        username,
    )
    ok_password = await fill_first(
        page,
        [
            'input[type="password"]',
            'input[name*="password" i]',
            'input[placeholder*="password" i]',
        ],
        password,
    )

    if not ok_user or not ok_password:
        raise RuntimeError("Wallmob: fant ikke brukernavn/passord-feltene.")

    if not await click_text_first(page, ["LOGIN", "Log in", "Logg inn"]):
        try:
            await page.locator('button[type="submit"]').first.click()
        except Exception as exc:
            raise RuntimeError("Wallmob: fant ikke login-knappen.") from exc

    try:
        await page.wait_for_url(
            re.compile(r"wbo-etail\.wallmob\.com/(?!login)"),
            timeout=30000,
        )
    except Exception:
        pass

    await page.goto(WALLMOB_HOME_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2200)
    text = await page.locator("body").inner_text()

    if "Dagens omsetning" not in text:
        raise RuntimeError(
            "Wallmob-login lyktes ikke eller dashboardet mangler 'Dagens omsetning'."
        )

    return page


async def read_wallmob(page: Page) -> dict[str, Any]:
    await page.goto(WALLMOB_HOME_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(1800)
    text = await page.locator("body").inner_text()

    if "Dagens omsetning" not in text:
        raise RuntimeError("Wallmob-sesjonen er ikke innlogget.")

    turnover = after_label(text, ["Dagens omsetning"])
    products = after_label(
        text, ["Totalt solgte produkter i dag", "Dagens produktsalg"]
    )
    orders = after_label(text, ["Antall kunder i dag"])
    margin = after_label(text, ["Totalt bruttomargin i dag", "Bruttomargin"])

    if turnover is None:
        raise RuntimeError("Wallmob: fant ikke dagens omsetning.")

    return {
        "ok": True,
        "turnover": turnover,
        "products": products,
        "orders": orders,
        "gross_margin": margin,
        "message": "OK – hentet direkte i PWS Cloud",
    }


async def login_nordpay(context: BrowserContext) -> Page:
    page = await context.new_page()
    await page.goto(
        f"{NORDPAY_BASE_URL}/dashboard",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    await page.wait_for_timeout(1200)

    body = await page.locator("body").inner_text()
    if "DAGENS SALG" in body.upper():
        return page

    ok_user = await fill_first(
        page,
        [
            'input[type="email"]',
            'input[name*="email" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="e-post" i]',
        ],
        NORDPAY_USER,
    )
    ok_password = await fill_first(
        page,
        [
            'input[type="password"]',
            'input[name*="password" i]',
            'input[placeholder*="passord" i]',
        ],
        NORDPAY_PASSWORD,
    )

    if not ok_user or not ok_password:
        raise RuntimeError("NordPay: fant ikke e-post/passord-feltene.")

    if not await click_text_first(page, ["Sign in", "Logg inn", "Login"]):
        try:
            await page.locator('button[type="submit"]').first.click()
        except Exception as exc:
            raise RuntimeError("NordPay: fant ikke login-knappen.") from exc

    try:
        await page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass

    await page.goto(
        f"{NORDPAY_BASE_URL}/dashboard",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    await page.wait_for_timeout(1800)
    body = await page.locator("body").inner_text()

    if "DAGENS SALG" not in body.upper():
        raise RuntimeError("NordPay-login lyktes ikke eller dashboardet er endret.")

    return page


async def nordpay_report_tables(page: Page, relative_url: str) -> list[list[list[str]]]:
    result = await page.evaluate(
        """async (relativeUrl) => {
            const r = await fetch(relativeUrl, {
                credentials: 'include',
                cache: 'no-store',
                headers: {'X-Requested-With': 'XMLHttpRequest'}
            });
            const txt = await r.text();
            if (!r.ok) return {ok:false, status:r.status, tables:[]};
            const doc = new DOMParser().parseFromString(txt, 'text/html');
            const tables = Array.from(doc.querySelectorAll('table')).map(t =>
                Array.from(t.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim())
                )
            );
            return {ok:true, status:r.status, tables};
        }""",
        relative_url,
    )
    if not result or not result.get("ok"):
        return []
    return result.get("tables") or []


def header_index(header: list[str], names: list[str]) -> int:
    wanted = {n.strip().lower() for n in names}
    for i, cell in enumerate(header):
        if str(cell).strip().lower() in wanted:
            return i
    return -1


async def read_nordpay(page: Page) -> dict[str, Any]:
    await page.goto(
        f"{NORDPAY_BASE_URL}/dashboard",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    await page.wait_for_timeout(1500)
    text = await page.locator("body").inner_text()

    m_sales = re.search(
        r"DAGENS\s+SALG.{0,160}?([0-9][0-9\s\.,-]*)\s*(?:kr|NOK)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    m_orders = re.search(
        r"([0-9]+)\s+Totale\s+bestillinger",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    turnover = parse_number(m_sales.group(1)) if m_sales else None
    orders = parse_number(m_orders.group(1)) if m_orders else None

    if turnover is None:
        raise RuntimeError("NordPay: fant ikke 'Dagens salg'.")

    products = None
    turnover_ex_vat = None
    today = datetime.now(OSLO).strftime("%Y-%m-%d")

    try:
        tables = await nordpay_report_tables(
            page, f"/manage/reports/sales?starts={today}&ends={today}"
        )
        for table in tables:
            for r, header in enumerate(table):
                i_period = header_index(header, ["Period"])
                i_items = header_index(header, ["Number of Items"])
                i_ex = header_index(header, ["Sales Ex. VAT", "Sales Ex VAT"])
                if i_period < 0:
                    continue
                for row in table[r + 1 :]:
                    if len(row) <= i_period or str(row[i_period]).strip() != today:
                        continue
                    if 0 <= i_items < len(row):
                        products = parse_number(row[i_items])
                    if 0 <= i_ex < len(row):
                        turnover_ex_vat = parse_number(row[i_ex])
                    break
    except Exception as exc:
        print(f"[nordpay] Sales Report kunne ikke leses: {exc}", flush=True)

    return {
        "ok": True,
        "turnover": turnover,
        "turnover_ex_vat": turnover_ex_vat,
        "orders": orders,
        "products": products,
        "message": "OK – hentet direkte i PWS Cloud",
    }


WEATHER_CODES = {
    0: "Klart",
    1: "For det meste klart",
    2: "Delvis skyet",
    3: "Overskyet",
    45: "Tåke",
    48: "Rimtåke",
    51: "Lett yr",
    53: "Yr",
    55: "Kraftig yr",
    61: "Lett regn",
    63: "Regn",
    65: "Kraftig regn",
    71: "Lett snø",
    73: "Snø",
    75: "Kraftig snø",
    80: "Lette regnbyger",
    81: "Regnbyger",
    82: "Kraftige regnbyger",
    95: "Tordenvær",
    96: "Tordenvær med hagl",
    99: "Kraftig tordenvær med hagl",
}


async def weather(client: httpx.AsyncClient, lat: float, lon: float) -> dict[str, Any]:
    try:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
                "wind_speed_unit": "ms",
                "timezone": "Europe/Oslo",
            },
            timeout=15,
        )
        r.raise_for_status()
        current = r.json()["current"]
        code = int(current.get("weather_code", -1))
        return {
            "ok": True,
            "temperature": float(current.get("temperature_2m", 0)),
            "precipitation": float(current.get("precipitation", 0)),
            "wind": float(current.get("wind_speed_10m", 0)),
            "description": WEATHER_CODES.get(code, "Værdata"),
            "message": "OK",
        }
    except Exception as exc:
        return {
            "ok": False,
            "temperature": None,
            "precipitation": None,
            "wind": None,
            "description": "Vær utilgjengelig",
            "message": str(exc),
        }


def empty_hourly() -> list[dict[str, Any]]:
    return [
        {"time": f"{h:02d}:00", "value": 0.0, "wallmob": 0.0, "nordpay": 0.0}
        for h in range(10, 22)
    ]


def parse_generated_date(payload: dict[str, Any] | None):
    if not payload:
        return None
    raw = payload.get("generated_at") or payload.get("generatedAt")
    if not raw:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(raw), fmt).date()
        except ValueError:
            pass
    return None


def build_hourly(
    previous: dict[str, Any] | None,
    wallmob_total: float | None,
    nordpay_total: float | None,
    now: datetime,
) -> list[dict[str, Any]]:
    points = empty_hourly()

    if previous and parse_generated_date(previous) == now.date():
        old_points = previous.get("hourly_revenue") or previous.get("nordpay_time") or []
        by_time = {str(p.get("time")): p for p in old_points if isinstance(p, dict)}
        for point in points:
            old = by_time.get(point["time"])
            if old:
                point["wallmob"] = float(old.get("wallmob") or 0)
                point["nordpay"] = float(old.get("nordpay") or 0)
                point["value"] = round(point["wallmob"] + point["nordpay"], 2)

        old_wall = parse_number((previous.get("wallmob") or {}).get("turnover"))
        old_nord = parse_number((previous.get("nordpay") or {}).get("turnover"))

        if 10 <= now.hour <= 21:
            key = f"{now.hour:02d}:00"
            current = next(p for p in points if p["time"] == key)

            if wallmob_total is not None and old_wall is not None:
                delta = wallmob_total - old_wall
                if 0 < delta < 1_000_000:
                    current["wallmob"] = round(current["wallmob"] + delta, 2)

            if nordpay_total is not None and old_nord is not None:
                delta = nordpay_total - old_nord
                if 0 < delta < 1_000_000:
                    current["nordpay"] = round(current["nordpay"] + delta, 2)

            current["value"] = round(current["wallmob"] + current["nordpay"], 2)

    return points


async def latest_payload(client: httpx.AsyncClient, park: str) -> dict[str, Any] | None:
    if not APP_KEY:
        return None
    try:
        r = await client.get(
            f"{CLOUD_URL}/api/dashboard",
            params={"park": park},
            headers={"X-PWS-Key": APP_KEY},
            timeout=20,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"[{park}] Kunne ikke hente forrige snapshot: {exc}", flush=True)
        return None


async def ingest(client: httpx.AsyncClient, park: str, payload: dict[str, Any]) -> None:
    r = await client.post(
        f"{CLOUD_URL}/api/ingest/{park}",
        headers={"X-PWS-Ingest-Key": INGEST_KEY},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()
    print(
        f"[{park}] snapshot {result.get('snapshot_id')} – "
        f"Wallmob {payload['wallmob'].get('turnover')} – "
        f"NordPay {payload['nordpay'].get('turnover')}",
        flush=True,
    )


class CloudCollector:
    """Memory-light collector.

    Only one browser context/page is kept open at a time. Login state is
    preserved as small Playwright storage-state dictionaries between polls.
    This is important on Render's 512 MB worker plan.
    """

    def __init__(self, browser: Browser):
        self.browser = browser
        self.wall_states: dict[str, dict[str, Any] | None] = {
            "sorlandet": None,
            "triaden": None,
        }
        self.nord_state: dict[str, Any] | None = None

    async def _new_context(
        self, storage_state: dict[str, Any] | None = None
    ) -> BrowserContext:
        context = await self.browser.new_context(
            locale="nb-NO",
            timezone_id="Europe/Oslo",
            ignore_https_errors=True,
            storage_state=storage_state,
        )

        async def block_heavy(route):
            if route.request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", block_heavy)
        return context

    async def fetch_wallmob(
        self, park: str, username: str, password: str, login_type: str
    ) -> dict[str, Any]:
        state = self.wall_states.get(park)
        context = await self._new_context(state)
        try:
            page = await context.new_page()

            # Reuse the previous login session when possible.
            if state is not None:
                try:
                    await page.goto(
                        WALLMOB_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(900)
                    body = await page.locator("body").inner_text()
                    if "Dagens omsetning" in body:
                        data = await read_wallmob(page)
                        self.wall_states[park] = await context.storage_state()
                        return data
                except Exception:
                    pass

            try:
                await page.close()
            except Exception:
                pass
        finally:
            await context.close()

        # Session expired or first run: log in in a fresh lightweight context.
        context = await self._new_context()
        try:
            page = await login_wallmob(context, username, password, login_type)
            data = await read_wallmob(page)
            self.wall_states[park] = await context.storage_state()
            return data
        finally:
            await context.close()

    async def fetch_nordpay(self) -> dict[str, Any]:
        context = await self._new_context(self.nord_state)
        try:
            page = await context.new_page()

            if self.nord_state is not None:
                try:
                    await page.goto(
                        f"{NORDPAY_BASE_URL}/dashboard",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(900)
                    body = await page.locator("body").inner_text()
                    if "DAGENS SALG" in body.upper():
                        data = await read_nordpay(page)
                        self.nord_state = await context.storage_state()
                        return data
                except Exception:
                    pass

            try:
                await page.close()
            except Exception:
                pass
        finally:
            await context.close()

        context = await self._new_context()
        try:
            page = await login_nordpay(context)
            data = await read_nordpay(page)
            self.nord_state = await context.storage_state()
            return data
        finally:
            await context.close()


async def collect_once(
    collector: CloudCollector,
    client: httpx.AsyncClient,
) -> None:
    now = datetime.now(OSLO)

    if SORLANDET_WALLMOB_USER and SORLANDET_WALLMOB_PASSWORD:
        try:
            # Fetch each source sequentially. The browser context is closed
            # immediately after each source so Chromium cannot accumulate
            # three dashboards in RAM at the same time.
            wall = await collector.fetch_wallmob(
                "sorlandet",
                SORLANDET_WALLMOB_USER,
                SORLANDET_WALLMOB_PASSWORD,
                SORLANDET_WALLMOB_TYPE,
            )

            if not NORDPAY_USER or not NORDPAY_PASSWORD:
                raise RuntimeError("Mangler NORDPAY_USERNAME/NORDPAY_PASSWORD i Render.")

            nord = await collector.fetch_nordpay()
            prev = await latest_payload(client, "sorlandet")
            hourly = build_hourly(prev, wall.get("turnover"), nord.get("turnover"), now)
            w = await weather(
                client, PARKS["sorlandet"]["lat"], PARKS["sorlandet"]["lon"]
            )

            payload = {
                "park": "sorlandet",
                "park_name": PARKS["sorlandet"]["name"],
                "generated_at": now.strftime("%d.%m.%Y %H:%M:%S"),
                "weather": w,
                "wallmob": wall,
                "nordpay": nord,
                "hourly_revenue": hourly,
                "nordpay_time": hourly,
                "hourly_status": "Skybasert timegraf – differanse mellom live snapshots",
            }
            await ingest(client, "sorlandet", payload)
        except Exception as exc:
            print(f"[sorlandet] FEIL: {exc}", flush=True)
    else:
        print(
            "[sorlandet] Hopper over – mangler WALLMOB_SORLANDET_USERNAME/PASSWORD.",
            flush=True,
        )

    if TRIADEN_WALLMOB_USER and TRIADEN_WALLMOB_PASSWORD:
        try:
            wall = await collector.fetch_wallmob(
                "triaden",
                TRIADEN_WALLMOB_USER,
                TRIADEN_WALLMOB_PASSWORD,
                TRIADEN_WALLMOB_TYPE,
            )
            nord = {
                "ok": True,
                "turnover": 0.0,
                "orders": 0,
                "products": 0,
                "message": "Ikke brukt på Playworld Triaden",
            }
            prev = await latest_payload(client, "triaden")
            hourly = build_hourly(prev, wall.get("turnover"), 0.0, now)
            w = await weather(
                client, PARKS["triaden"]["lat"], PARKS["triaden"]["lon"]
            )

            payload = {
                "park": "triaden",
                "park_name": PARKS["triaden"]["name"],
                "generated_at": now.strftime("%d.%m.%Y %H:%M:%S"),
                "weather": w,
                "wallmob": wall,
                "nordpay": nord,
                "hourly_revenue": hourly,
                "nordpay_time": hourly,
                "hourly_status": "Skybasert timegraf – differanse mellom live snapshots",
            }
            await ingest(client, "triaden", payload)
        except Exception as exc:
            print(f"[triaden] FEIL: {exc}", flush=True)
    else:
        print(
            "[triaden] Hopper over – mangler WALLMOB_TRIADEN_USERNAME/PASSWORD.",
            flush=True,
        )


def validate_base_secrets() -> None:
    missing = []
    if not INGEST_KEY:
        missing.append("PWS_INGEST_KEY")
    if not APP_KEY:
        missing.append("PWS_APP_KEY")
    if missing:
        raise RuntimeError("Mangler Render secrets: " + ", ".join(missing))


async def main() -> None:
    validate_base_secrets()
    print("PWS Cloud Collector starter.", flush=True)
    print(f"API: {CLOUD_URL}", flush=True)
    print(f"Intervall: {INTERVAL_SECONDS} sek", flush=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        collector = CloudCollector(browser)

        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "PWS-Cloud-Collector/1.0"},
        ) as client:
            while True:
                started = datetime.now(OSLO)
                try:
                    await collect_once(collector, client)
                except Exception as exc:
                    print(f"[collector] FEIL: {exc}", flush=True)

                elapsed = (datetime.now(OSLO) - started).total_seconds()
                await asyncio.sleep(max(5, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
