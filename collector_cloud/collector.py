from __future__ import annotations

import asyncio
import os
import re
import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright


OSLO = ZoneInfo("Europe/Oslo")

CLOUD_URL = os.getenv("PWS_CLOUD_URL", "https://pws-omsetning-api.onrender.com").rstrip("/")
APP_KEY = os.getenv("PWS_APP_KEY", "")
INGEST_KEY = os.getenv("PWS_INGEST_KEY", "")
INTERVAL_SECONDS = max(30, int(os.getenv("PWS_COLLECT_INTERVAL", "60")))
COLLECTOR_BUILD = "2026-10-19-wallmob-capture-v4"

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
    performance_payloads: list[dict[str, Any]] = []

    async def on_response(response):
        try:
            if (
                "bapi-etail.wallmob.com/reports/performance" in response.url
                and "top_products" in response.url
                and response.ok
            ):
                payload = await response.json()
                if isinstance(payload, dict):
                    performance_payloads.append(payload)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(WALLMOB_HOME_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2200)
        text = await page.locator("body").inner_text()
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

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

    product_sales: list[dict[str, Any]] = []
    if performance_payloads:
        report = performance_payloads[-1]
        for item in report.get("top_products") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("product_name") or "").strip()
            raw_turnover = parse_number(item.get("turnover"))
            if not name or raw_turnover is None:
                continue
            # Wallmob performance API returns monetary values in øre.
            revenue = round(raw_turnover / 100.0, 2)
            product_sales.append(
                {
                    "name": name,
                    "quantity": None,
                    "revenue": revenue,
                    "source": "wallmob",
                    "source_id": item.get("product_id"),
                }
            )

    product_sales.sort(key=lambda x: float(x.get("revenue") or 0), reverse=True)

    return {
        "ok": True,
        "turnover": turnover,
        "products": products,
        "orders": orders,
        "gross_margin": margin,
        "product_sales": product_sales,
        "_product_sales_complete": bool(performance_payloads),
        "message": "OK – hentet direkte i PWS Cloud",
    }


def _safe_shape(value: Any, depth: int = 0) -> Any:
    """Return a compact, secret-safe view of a JSON payload for Render logs."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:30]:
            low = str(key).lower()
            if any(secret in low for secret in ("token", "password", "secret", "authorization", "cookie")):
                out[key] = "<redacted>"
            else:
                out[key] = _safe_shape(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_safe_shape(item, depth + 1) for item in value[:12]]
    if isinstance(value, str) and len(value) > 180:
        return value[:180] + "..."
    return value


async def probe_wallmob_top_products(page: Page, park: str, verbose: bool = True, target_date: date | None = None) -> dict[str, Any] | None:
    """Discover Wallmob's top_products response shape without logging credentials."""
    captured_headers: dict[str, str] = {}
    captured_performance_url = ""
    captured_top_products: list[Any] = []

    async def on_request(request):
        nonlocal captured_headers, captured_performance_url
        try:
            # Capture the exact performance request made by Wallmob itself.
            # This preserves any shop/account filters and proprietary auth headers.
            if (
                "bapi-etail.wallmob.com/reports/performance" not in request.url
                or "top_products" not in request.url
            ):
                return
            headers = await request.all_headers()
            if headers:
                captured_headers = headers
            captured_performance_url = request.url
        except Exception:
            pass

    async def on_response(response):
        try:
            url = response.url
            if (
                "bapi-etail.wallmob.com/reports/performance" in url
                and "top_products" in url
                and response.ok
            ):
                captured_top_products.append(await response.json())
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        # Reload once so we can observe the same API headers the Wallmob frontend uses.
        await page.reload(wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)

        requested_date = target_date or datetime.now(OSLO).date()
        today = datetime.now(OSLO).date()

        if captured_top_products and requested_date == today:
            payload = captured_top_products[-1]
            if verbose:
                sample = _safe_shape(payload)
                print(
                    f"[{park}] TOP_PRODUCTS_PROBE captured="
                    + json.dumps(sample, ensure_ascii=False)[:4000],
                    flush=True,
                )
            return payload

        # Reuse all safe headers from Wallmob's own performance request.
        # A proprietary token/header may otherwise be lost if we hard-code names.
        blocked_headers = {
            "host",
            "content-length",
            "connection",
            "accept-encoding",
        }
        api_headers = {
            key: value
            for key, value in captured_headers.items()
            if key.lower() not in blocked_headers and not key.startswith(":")
        }

        start = datetime(
            requested_date.year,
            requested_date.month,
            requested_date.day,
            0,
            0,
            0,
            tzinfo=OSLO,
        )
        if requested_date == today:
            end = datetime.now(OSLO)
        else:
            end = start + timedelta(days=1) - timedelta(seconds=1)

        if captured_performance_url:
            parts = urlsplit(captured_performance_url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["starttime"] = str(int(start.timestamp()))
            query["endtime"] = str(int(end.timestamp()))
            query["include_vat"] = "1"
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
        else:
            params = {
                "starttime": int(start.timestamp()),
                "endtime": int(end.timestamp()),
                "interval_grouping": 999999999,
                "include_vat": 1,
                "reports": json.dumps(
                    [
                        "turnover",
                        "top_products",
                        "top_users",
                        "top_registers",
                        "top_shops",
                        "all_shops",
                    ],
                    separators=(",", ":"),
                ),
            }
            url = "https://bapi-etail.wallmob.com/reports/performance?" + urlencode(params)

        response = await page.context.request.get(url, headers=api_headers, timeout=30000)
        status = response.status
        body = await response.text()

        if status >= 400:
            print(
                f"[{park}] WALLMOB_HISTORY HTTP {status} "
                f"date={requested_date.isoformat()}; "
                f"captured_header_names={sorted(api_headers.keys())}; "
                f"captured_url={bool(captured_performance_url)}",
                flush=True,
            )
            return None

        try:
            payload = json.loads(body)
        except Exception:
            print(
                f"[{park}] TOP_PRODUCTS_PROBE non-json="
                + re.sub(r"\s+", " ", body)[:1000],
                flush=True,
            )
            return

        if verbose:
            sample = _safe_shape(payload)
            print(
                f"[{park}] TOP_PRODUCTS_PROBE fetched="
                + json.dumps(sample, ensure_ascii=False)[:4000],
                flush=True,
            )
        return payload
    except Exception as exc:
        print(f"[{park}] TOP_PRODUCTS_PROBE FEIL: {exc}", flush=True)
        return None
    finally:
        try:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
        except Exception:
            pass


def wallmob_products_from_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return result
    for item in payload.get("top_products") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        raw_turnover = parse_number(item.get("turnover"))
        if not name or raw_turnover is None:
            continue
        result.append(
            {
                "name": name,
                "quantity": None,
                "revenue": round(raw_turnover / 100.0, 2),
                "source": "wallmob",
                "source_id": item.get("product_id"),
            }
        )
    result.sort(key=lambda x: float(x.get("revenue") or 0), reverse=True)
    return result


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


def _safe_url_pattern(url: str) -> str:
    # Keep route structure, but avoid dumping transaction/order identifiers.
    url = re.sub(r"([?&][^=]+=)[^&]+", r"\1<value>", url)
    url = re.sub(r"/[0-9a-fA-F-]{8,}(?=/|$)", "/<id>", url)
    url = re.sub(r"/\d{3,}(?=/|$)", "/<id>", url)
    return url[:500]


async def probe_nordpay_products(page: Page) -> None:
    """One-time, read-only discovery of where NordPay exposes ticket/product lines."""
    try:
        today = datetime.now(OSLO).strftime("%Y-%m-%d")
        sales_tables = await nordpay_report_tables(
            page, f"/manage/reports/sales?starts={today}&ends={today}"
        )
        compact_tables = []
        for table in sales_tables[:4]:
            compact_tables.append([row[:12] for row in table[:8]])
        print(
            "[nordpay] NORDPAY_PRODUCTS_PROBE sales_tables="
            + json.dumps(compact_tables, ensure_ascii=False)[:3500],
            flush=True,
        )

        seen_urls: set[str] = set()

        async def on_response(response):
            try:
                u = response.url
                low = u.lower()
                if NORDPAY_BASE_URL in u and any(
                    word in low
                    for word in (
                        "transaction",
                        "order",
                        "sale",
                        "product",
                        "item",
                        "report",
                    )
                ):
                    seen_urls.add(_safe_url_pattern(u))
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(
                f"{NORDPAY_BASE_URL}/manage/transactions",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_timeout(2500)

            info = await page.evaluate(
                """() => {
                    const tables = Array.from(document.querySelectorAll('table')).map(t => ({
                        headers: Array.from(t.querySelectorAll('thead th')).map(x => x.innerText.trim()),
                        firstRows: Array.from(t.querySelectorAll('tbody tr')).slice(0,3).map(tr =>
                            Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()).slice(0,10)
                        )
                    }));
                    const links = Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.getAttribute('href'))
                        .filter(Boolean)
                        .filter(h => /transaction|order|sale|product|item|report/i.test(h))
                        .slice(0,30);
                    const scripts = Array.from(document.querySelectorAll('script[src]'))
                        .map(s => s.getAttribute('src'))
                        .filter(Boolean)
                        .filter(s => /transaction|order|sale|product|item|report/i.test(s))
                        .slice(0,20);
                    return {tables, links, scripts};
                }"""
            )

            # Headers and route patterns are useful; row values may contain payment/customer
            # data, so only log table headers, not transaction rows.
            safe_info = {
                "table_headers": [
                    t.get("headers", [])[:15] for t in (info.get("tables") or [])[:6]
                ],
                "links": [_safe_url_pattern(str(x)) for x in (info.get("links") or [])],
                "scripts": [str(x)[:300] for x in (info.get("scripts") or [])],
                "network_urls": sorted(seen_urls)[:40],
            }
            print(
                "[nordpay] NORDPAY_PRODUCTS_PROBE transactions="
                + json.dumps(safe_info, ensure_ascii=False)[:5000],
                flush=True,
            )


            # Deeper, still read-only discovery: inspect products/orders pages and
            # one order-detail page, but log structure only (no customer/payment values).
            first_order_edit = None
            for raw_link in (info.get("links") or []):
                if re.search(r"/manage/orders/edit/\d+", str(raw_link)):
                    first_order_edit = str(raw_link)
                    break

            async def page_structure(relative_url: str) -> dict[str, Any]:
                await page.goto(
                    f"{NORDPAY_BASE_URL}{relative_url}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await page.wait_for_timeout(1800)
                return await page.evaluate(
                    """() => ({
                        title: document.title,
                        headings: Array.from(document.querySelectorAll('h1,h2,h3,h4'))
                            .map(x => x.innerText.trim())
                            .filter(Boolean)
                            .slice(0,20),
                        table_headers: Array.from(document.querySelectorAll('table')).map(t =>
                            Array.from(t.querySelectorAll('thead th')).map(x => x.innerText.trim())
                        ).slice(0,10),
                        form_labels: Array.from(document.querySelectorAll('label'))
                            .map(x => x.innerText.trim())
                            .filter(Boolean)
                            .slice(0,40),
                        link_patterns: Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.getAttribute('href'))
                            .filter(Boolean)
                            .filter(h => /product|item|order|ticket|sale/i.test(h))
                            .slice(0,40)
                    })"""
                )

            for label, rel in (
                ("products", "/manage/products"),
                ("orders", "/manage/orders"),
            ):
                try:
                    structure = await page_structure(rel)
                    structure["link_patterns"] = [
                        _safe_url_pattern(str(x))
                        for x in (structure.get("link_patterns") or [])
                    ]
                    print(
                        f"[nordpay] NORDPAY_PRODUCTS_PROBE {label}_structure="
                        + json.dumps(structure, ensure_ascii=False)[:4500],
                        flush=True,
                    )

                    if label == "orders":
                        history_info = await page.evaluate(
                            """() => {
                                const interesting = (s) => /date|start|end|page|offset|limit|force|period/i.test(s || '');
                                const inputs = Array.from(document.querySelectorAll('input,select')).map(el => ({
                                    tag: el.tagName,
                                    name: el.getAttribute('name') || '',
                                    id: el.id || '',
                                    type: el.getAttribute('type') || '',
                                    value: el.value || ''
                                })).filter(x => interesting(x.name) || interesting(x.id) || /date/i.test(x.type));

                                const links = Array.from(document.querySelectorAll('a[href]'))
                                    .map(a => ({
                                        text: (a.innerText || '').trim(),
                                        href: a.getAttribute('href') || ''
                                    }))
                                    .filter(x => /page|date|start|end|force|orders/i.test(x.href) || /next|prev|older|newer|forrige|neste/i.test(x.text));

                                const buttons = Array.from(document.querySelectorAll('button'))
                                    .map(b => ({
                                        text: (b.innerText || '').trim(),
                                        name: b.getAttribute('name') || '',
                                        value: b.getAttribute('value') || '',
                                        data: Array.from(b.attributes)
                                            .filter(a => a.name.startsWith('data-'))
                                            .reduce((o,a) => { o[a.name]=a.value; return o; }, {})
                                    }))
                                    .filter(x => /next|prev|older|newer|forrige|neste|date|day|week|month/i.test(x.text + ' ' + x.name));

                                const forms = Array.from(document.querySelectorAll('form')).map(f => ({
                                    action: f.getAttribute('action') || '',
                                    method: f.getAttribute('method') || ''
                                })).filter(x => /order|date|report|setting/i.test(x.action));

                                return {inputs, links: links.slice(0,50), buttons: buttons.slice(0,30), forms: forms.slice(0,20)};
                            }"""
                        )
                        history_info["links"] = [
                            {
                                "text": str(x.get("text") or "")[:80],
                                "href": _safe_url_pattern(str(x.get("href") or "")),
                            }
                            for x in (history_info.get("links") or [])
                        ]
                        print(
                            "[nordpay] NORDPAY_HISTORY_PROBE="
                            + json.dumps(history_info, ensure_ascii=False)[:5000],
                            flush=True,
                        )


                        # Try common read-only history/pagination query patterns and
                        # compare the returned order dates/IDs. NordPay may ignore
                        # unsupported parameters; the log makes that visible.
                        candidate_urls = [
                            "/manage/orders?page=2",
                            "/manage/orders?page=3",
                            "/manage/orders?offset=50",
                            "/manage/orders?offset=100",
                            "/manage/orders?limit=100",
                            "/manage/orders?starts=2026-01-01&ends=2026-01-31",
                            "/manage/orders?start=2026-01-01&end=2026-01-31",
                            "/manage/orders?date=2026-01-15",
                        ]

                        query_results = []
                        for candidate in candidate_urls:
                            try:
                                result = await page.evaluate(
                                    """async (relativeUrl) => {
                                        const r = await fetch(relativeUrl, {
                                            credentials: 'include',
                                            cache: 'no-store',
                                            headers: {'X-Requested-With':'XMLHttpRequest'}
                                        });
                                        const txt = await r.text();
                                        if (!r.ok) return {ok:false, status:r.status, rows:[]};
                                        const doc = new DOMParser().parseFromString(txt, 'text/html');
                                        const t = Array.from(doc.querySelectorAll('table')).find(tbl => {
                                            const h = Array.from(tbl.querySelectorAll('thead th'))
                                                .map(x => x.innerText.trim());
                                            return h.includes('ID') && h.includes('Created At') && h.includes('Items');
                                        });
                                        if (!t) return {ok:true, status:r.status, rows:[]};
                                        const headers = Array.from(t.querySelectorAll('thead th'))
                                            .map(x => x.innerText.trim());
                                        const idIdx = headers.indexOf('ID');
                                        const dateIdx = headers.indexOf('Created At');
                                        const rows = Array.from(t.querySelectorAll('tbody tr')).slice(0,5).map(tr => {
                                            const cells = Array.from(tr.querySelectorAll('td'));
                                            return {
                                                id: idIdx >= 0 ? (cells[idIdx]?.innerText || '').trim() : '',
                                                created_at: dateIdx >= 0 ? (cells[dateIdx]?.innerText || '').trim() : ''
                                            };
                                        });
                                        return {ok:true, status:r.status, rows};
                                    }""",
                                    candidate,
                                )
                                query_results.append({
                                    "url": _safe_url_pattern(candidate),
                                    "ok": bool(result.get("ok")),
                                    "status": result.get("status"),
                                    "rows": result.get("rows") or [],
                                })
                            except Exception as exc:
                                query_results.append({
                                    "url": _safe_url_pattern(candidate),
                                    "ok": False,
                                    "error": str(exc)[:160],
                                })

                        print(
                            "[nordpay] NORDPAY_HISTORY_QUERY_PROBE="
                            + json.dumps(query_results, ensure_ascii=False)[:7000],
                            flush=True,
                        )

                    if label == "orders":
                        order_sample = await page.evaluate(
                            """() => {
                                const t = Array.from(document.querySelectorAll('table')).find(tbl => {
                                    const h = Array.from(tbl.querySelectorAll('thead th'))
                                        .map(x => x.innerText.trim().toLowerCase());
                                    return h.includes('id') && h.includes('created at') && h.includes('items');
                                });
                                if (!t) return [];
                                const headers = Array.from(t.querySelectorAll('thead th'))
                                    .map(x => x.innerText.trim());
                                const allowed = new Set(['ID','Order','Order Nr','Queue Nr','Status','Created At','Items','Totals']);
                                return Array.from(t.querySelectorAll('tbody tr')).slice(0,8).map(tr => {
                                    const cells = Array.from(tr.querySelectorAll('td'));
                                    const out = {};
                                    headers.forEach((h, i) => {
                                        if (allowed.has(h)) out[h] = (cells[i]?.innerText || '').trim();
                                    });
                                    const edit = tr.querySelector('a[href*="/manage/orders/edit/"]');
                                    if (edit) out.edit = edit.getAttribute('href');
                                    return out;
                                });
                            }"""
                        )
                        for row in order_sample:
                            if row.get("edit"):
                                row["edit"] = _safe_url_pattern(str(row["edit"]))
                        print(
                            "[nordpay] NORDPAY_PRODUCTS_PROBE order_list_sample="
                            + json.dumps(order_sample, ensure_ascii=False)[:4500],
                            flush=True,
                        )
                except Exception as exc:
                    print(
                        f"[nordpay] NORDPAY_PRODUCTS_PROBE {label}_structure FEIL: {exc}",
                        flush=True,
                    )

            if first_order_edit:
                try:
                    rel = first_order_edit
                    if rel.startswith(NORDPAY_BASE_URL):
                        rel = rel[len(NORDPAY_BASE_URL):]
                    structure = await page_structure(rel)
                    structure["link_patterns"] = [
                        _safe_url_pattern(str(x))
                        for x in (structure.get("link_patterns") or [])
                    ]
                    print(
                        "[nordpay] NORDPAY_PRODUCTS_PROBE order_detail_structure="
                        + json.dumps(structure, ensure_ascii=False)[:4500],
                        flush=True,
                    )


                    # Log only the sold item rows from the first order. These
                    # contain product/price/quantity business data and exclude
                    # customer, receipt and payment tables.
                    item_rows = await page.evaluate(
                        """() => {
                            const tables = Array.from(document.querySelectorAll('table'));
                            for (const t of tables) {
                                const headers = Array.from(t.querySelectorAll('thead th'))
                                    .map(x => x.innerText.trim());
                                const norm = headers.map(x => x.toLowerCase());
                                if (
                                    norm.includes('product') &&
                                    norm.includes('unit price') &&
                                    norm.includes('qty') &&
                                    norm.includes('price')
                                ) {
                                    return Array.from(t.querySelectorAll('tbody tr'))
                                        .slice(0,20)
                                        .map(tr => {
                                            const cells = Array.from(tr.querySelectorAll('td'))
                                                .map(td => td.innerText.trim());
                                            const row = {};
                                            headers.forEach((h, i) => row[h || ('col_' + i)] = cells[i] ?? '');
                                            return row;
                                        });
                                }
                            }
                            return [];
                        }"""
                    )
                    print(
                        "[nordpay] NORDPAY_PRODUCTS_PROBE order_item_sample="
                        + json.dumps(item_rows, ensure_ascii=False)[:4500],
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[nordpay] NORDPAY_PRODUCTS_PROBE order_detail_structure FEIL: {exc}",
                        flush=True,
                    )
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

        await page.goto(
            f"{NORDPAY_BASE_URL}/dashboard",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await page.wait_for_timeout(700)
    except Exception as exc:
        print(f"[nordpay] NORDPAY_PRODUCTS_PROBE FEIL: {exc}", flush=True)


def parse_nordpay_money(value: Any) -> float | None:
    """Parse NordPay money strings such as 169,-kr and 150.89,-kr."""
    if value is None:
        return None

    s = str(value).strip()
    if not s:
        return None

    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = re.sub(r"(?i)\bNOK\b|\bkr\b", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("−", "-").replace("(", "").replace(")", "")

    # NordPay may append ',-' after a decimal amount, e.g. 150.89,-kr.
    s = re.sub(r",-$", "", s)
    s = s.lstrip("-")
    s = re.sub(r"[^0-9,.]", "", s)
    if not s:
        return None

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[-1]
        if len(tail) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        tail = s.rsplit(".", 1)[-1]
        if len(tail) != 2:
            s = s.replace(".", "")

    try:
        number = float(s)
    except ValueError:
        return None

    return -abs(number) if negative else abs(number)


def nordpay_product_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Product innerText contains product name on first line and metadata below.
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def canonical_product_name(name: str) -> str:
    clean = re.sub(r"\s+", " ", str(name or "")).strip()
    low = clean.casefold()

    if "minigolf" in low:
        return "Minigolf"
    if "lek" in low:
        if re.search(r"\b30\s*(?:min|minutt)", low):
            return "Lek - 30 min"
        if re.search(r"\b1\s*(?:time|timer|t)\b", low):
            return "Lek - 1 time"
        if re.search(r"\b2\s*(?:time|timer|t)\b", low):
            return "Lek - 2 timer"
        if "heldag" in low or "hele dagen" in low:
            return "Lek - heldag"
    return clean


def combine_product_sales(*lists: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for items in lists:
        for item in items or []:
            raw_name = str(item.get("name") or "").strip()
            if not raw_name:
                continue
            display = canonical_product_name(raw_name)
            key = display.casefold()
            current = grouped.setdefault(
                key,
                {
                    "name": display,
                    "quantity": 0.0,
                    "quantity_complete": True,
                    "revenue": 0.0,
                    "sources": [],
                },
            )

            qty = item.get("quantity")
            if qty is None:
                current["quantity_complete"] = False
            else:
                current["quantity"] += float(qty)

            current["revenue"] += float(item.get("revenue") or 0)
            source = str(item.get("source") or "")
            if source and source not in current["sources"]:
                current["sources"].append(source)

    result = []
    for item in grouped.values():
        item["quantity"] = (
            round(item["quantity"], 2) if item["quantity_complete"] else None
        )
        item["revenue"] = round(item["revenue"], 2)
        result.append(item)

    result.sort(key=lambda x: float(x.get("revenue") or 0), reverse=True)
    return result


async def nordpay_order_rows(page: Page) -> list[dict[str, Any]]:
    await page.goto(
        f"{NORDPAY_BASE_URL}/manage/orders",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    await page.wait_for_timeout(1600)
    rows = await page.evaluate(
        """() => {
            const tables = Array.from(document.querySelectorAll('table'));
            const t = tables.find(tbl => {
                const h = Array.from(tbl.querySelectorAll('thead th'))
                    .map(x => x.innerText.trim());
                return h.includes('Created At') && h.includes('Items') && h.includes('Status');
            });
            if (!t) return [];
            const headers = Array.from(t.querySelectorAll('thead th'))
                .map(x => x.innerText.trim());
            return Array.from(t.querySelectorAll('tbody tr')).map(tr => {
                const cells = Array.from(tr.querySelectorAll('td'));
                const row = {};
                headers.forEach((h, i) => row[h || ('col_' + i)] = (cells[i]?.innerText || '').trim());
                const edit = tr.querySelector('a[href*="/manage/orders/edit/"]');
                if (edit) row.edit = edit.getAttribute('href');
                return row;
            });
        }"""
    )
    return rows if isinstance(rows, list) else []


async def nordpay_order_items(page: Page, relative_url: str) -> list[dict[str, Any]]:
    return await page.evaluate(
        """async (relativeUrl) => {
            const r = await fetch(relativeUrl, {
                credentials: 'include',
                cache: 'no-store',
                headers: {'X-Requested-With':'XMLHttpRequest'}
            });
            const txt = await r.text();
            if (!r.ok) return [];
            const doc = new DOMParser().parseFromString(txt, 'text/html');
            const tables = Array.from(doc.querySelectorAll('table'));
            const t = tables.find(tbl => {
                const h = Array.from(tbl.querySelectorAll('thead th'))
                    .map(x => x.innerText.trim());
                return h.includes('Product') && h.includes('Unit Price') &&
                       h.includes('Tax') && h.includes('Qty') && h.includes('Price');
            });
            if (!t) return [];
            const headers = Array.from(t.querySelectorAll('thead th'))
                .map(x => x.innerText.trim());
            return Array.from(t.querySelectorAll('tbody tr')).map(tr => {
                const cells = Array.from(tr.querySelectorAll('td'));
                const row = {};
                headers.forEach((h, i) => row[h || ('col_' + i)] = (cells[i]?.innerText || '').trim());
                return row;
            });
        }""",
        relative_url,
    )


async def read_nordpay_daily_products(page: Page) -> list[dict[str, Any]]:
    today = datetime.now(OSLO).strftime("%Y-%m-%d")
    orders = await nordpay_order_rows(page)

    completed = []
    for order in orders:
        created = str(order.get("Created At") or "").strip()
        status = str(order.get("Status") or "").casefold()
        edit = str(order.get("edit") or "").strip()
        if created.startswith(today) and "completed" in status and edit:
            completed.append(edit)

    grouped: dict[str, dict[str, Any]] = {}
    for edit in completed[:300]:
        try:
            rows = await nordpay_order_items(page, edit)
        except Exception as exc:
            print(f"[nordpay] Kunne ikke lese ordrelinjer {edit}: {exc}", flush=True)
            continue

        for row in rows:
            name = nordpay_product_label(row.get("Product"))
            if not name:
                continue
            qty = parse_number(row.get("Qty"))
            net = parse_nordpay_money(row.get("Price"))

            tax_text = str(row.get("Tax") or "")
            tax_amount = None

            # Example from NordPay: "12%\n18.11,-kr".
            # Capture the monetary tax amount after the percent value.
            m_tax = re.search(
                r"%\s*([0-9]+(?:[.,][0-9]{2})?)\s*(?:,-)?\s*kr",
                tax_text,
                flags=re.IGNORECASE,
            )
            if m_tax:
                tax_amount = parse_nordpay_money(m_tax.group(1))

            revenue = None
            if net is not None:
                revenue = net + (tax_amount or 0.0)

            key = canonical_product_name(name).casefold()
            item = grouped.setdefault(
                key,
                {
                    "name": canonical_product_name(name),
                    "quantity": 0.0,
                    "revenue": 0.0,
                    "source": "nordpay",
                },
            )
            if qty is not None:
                item["quantity"] += float(qty)
            if revenue is not None:
                item["revenue"] += float(revenue)

    result = []
    for item in grouped.values():
        item["quantity"] = round(item["quantity"], 2)
        item["revenue"] = round(item["revenue"], 2)
        result.append(item)
    result.sort(key=lambda x: float(x.get("revenue") or 0), reverse=True)
    return result


async def nordpay_order_rows_for_date(
    page: Page,
    target_date: date,
) -> list[dict[str, Any]]:
    """Read all visible order pages for one NordPay business date."""
    iso = target_date.isoformat()
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for page_no in range(1, 21):
        relative_url = (
            f"/manage/orders?starts={iso}&ends={iso}&page={page_no}"
        )
        result = await page.evaluate(
            """async (relativeUrl) => {
                const r = await fetch(relativeUrl, {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {'X-Requested-With':'XMLHttpRequest'}
                });
                const txt = await r.text();
                if (!r.ok) return {ok:false, status:r.status, rows:[]};
                const doc = new DOMParser().parseFromString(txt, 'text/html');
                const t = Array.from(doc.querySelectorAll('table')).find(tbl => {
                    const h = Array.from(tbl.querySelectorAll('thead th'))
                        .map(x => x.innerText.trim());
                    return h.includes('ID') && h.includes('Created At') &&
                           h.includes('Items') && h.includes('Status');
                });
                if (!t) return {ok:true, status:r.status, rows:[]};
                const headers = Array.from(t.querySelectorAll('thead th'))
                    .map(x => x.innerText.trim());
                return {
                    ok:true,
                    status:r.status,
                    rows:Array.from(t.querySelectorAll('tbody tr')).map(tr => {
                        const cells = Array.from(tr.querySelectorAll('td'));
                        const row = {};
                        headers.forEach((h, i) => {
                            row[h || ('col_' + i)] = (cells[i]?.innerText || '').trim();
                        });
                        const edit = tr.querySelector('a[href*="/manage/orders/edit/"]');
                        if (edit) row.edit = edit.getAttribute('href');
                        return row;
                    })
                };
            }""",
            relative_url,
        )

        if not result or not result.get("ok"):
            raise RuntimeError(
                f"NordPay ordrearkiv svarte HTTP {result.get('status') if result else '?'}"
            )

        rows = result.get("rows") or []
        if not rows:
            break

        added = 0
        for row in rows:
            row_id = str(row.get("ID") or "").strip()
            if row_id and row_id in seen_ids:
                continue
            if row_id:
                seen_ids.add(row_id)
            all_rows.append(row)
            added += 1

        # If page=N is ignored, page 2 will repeat page 1. Stop safely.
        if added == 0:
            break

    return all_rows


async def nordpay_products_for_date(
    page: Page,
    target_date: date,
) -> list[dict[str, Any]]:
    iso = target_date.isoformat()
    orders = await nordpay_order_rows_for_date(page, target_date)

    edits: list[str] = []
    for order in orders:
        created = str(order.get("Created At") or "").strip()
        status = str(order.get("Status") or "").casefold()
        edit = str(order.get("edit") or "").strip()
        if created.startswith(iso) and "completed" in status and edit:
            edits.append(edit)

    grouped: dict[str, dict[str, Any]] = {}
    for edit in edits[:500]:
        rows = await nordpay_order_items(page, edit)
        for row in rows:
            name = nordpay_product_label(row.get("Product"))
            if not name:
                continue

            qty = parse_number(row.get("Qty"))
            net = parse_nordpay_money(row.get("Price"))
            tax_text = str(row.get("Tax") or "")
            tax_amount = None
            m_tax = re.search(
                r"%\s*([0-9]+(?:[.,][0-9]{2})?)\s*(?:,-)?\s*kr",
                tax_text,
                flags=re.IGNORECASE,
            )
            if m_tax:
                tax_amount = parse_nordpay_money(m_tax.group(1))

            revenue = None if net is None else net + (tax_amount or 0.0)
            display = canonical_product_name(name)
            key = display.casefold()
            item = grouped.setdefault(
                key,
                {
                    "name": display,
                    "quantity": 0.0,
                    "revenue": 0.0,
                    "source": "nordpay",
                },
            )
            if qty is not None:
                item["quantity"] += float(qty)
            if revenue is not None:
                item["revenue"] += float(revenue)

    result = []
    for item in grouped.values():
        item["quantity"] = round(item["quantity"], 2)
        item["revenue"] = round(item["revenue"], 2)
        result.append(item)
    result.sort(key=lambda x: float(x.get("revenue") or 0), reverse=True)
    return result


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
    product_sales = payload.get("product_sales") or []
    if product_sales:
        summary = ", ".join(
            f"{str(p.get('name') or '').splitlines()[0]}={p.get('revenue')}kr"
            for p in product_sales[:5]
        )
        print(f"[{park}] PRODUKTER: {summary}", flush=True)


async def ingest_product_history(
    client: httpx.AsyncClient,
    park: str,
    business_date: str,
    products: list[dict[str, Any]],
    source: str = "live",
) -> None:
    try:
        r = await client.post(
            f"{CLOUD_URL}/api/products/ingest/{park}",
            headers={"X-PWS-Ingest-Key": INGEST_KEY},
            json={
                "business_date": business_date,
                "products": products,
                "source": source,
            },
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        print(
            f"[{park}] PRODUKTHISTORIKK {business_date}: "
            f"{result.get('products', 0)} produkter lagret",
            flush=True,
        )
    except Exception as exc:
        # API and worker can deploy a few seconds apart on Render. Keep live
        # collection running and retry automatically on the next cycle.
        print(
            f"[{park}] PRODUKTHISTORIKK kunne ikke lagres ennå: {exc}",
            flush=True,
        )


async def capture_wallmob_performance_request(
    page: Page,
    park: str,
) -> dict[str, Any] | None:
    """Reload Wallmob and capture the exact top_products request + response."""
    def wanted(url: str) -> bool:
        return (
            "bapi-etail.wallmob.com/reports/performance" in url
            and "top_products" in url
        )

    try:
        async with page.expect_request(
            lambda request: wanted(request.url) and request.method.upper() == "GET",
            timeout=20000,
        ) as request_info:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=45000,
            )

        request = await request_info.value
        response = await asyncio.wait_for(request.response(), timeout=30000)
        if response is None:
            print(
                f"[{park}] WALLMOB_CAPTURE FEIL: request fikk ingen response",
                flush=True,
            )
            return None

        headers = await request.all_headers()

        blocked = {
            "host",
            "content-length",
            "connection",
            "accept-encoding",
            "cookie",
        }
        safe_request_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in blocked and not k.startswith(":")
        }

        payload = await response.json()
        if not isinstance(payload, dict):
            return None

        print(
            f"[{park}] WALLMOB_CAPTURE ok "
            f"products={len(payload.get('top_products') or [])}; "
            f"header_names={sorted(safe_request_headers.keys())}",
            flush=True,
        )
        return {
            "url": request.url,
            "headers": safe_request_headers,
            "payload": payload,
        }
    except Exception as exc:
        print(
            f"[{park}] WALLMOB_CAPTURE FEIL: {exc}",
            flush=True,
        )
        return None


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
        self.wall_product_probe_done: set[str] = set()
        self.wall_report_templates: dict[str, dict[str, Any]] = {}
        self.nord_product_probe_done = False
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

    def _install_wallmob_capture(
        self,
        context: BrowserContext,
        park: str,
    ) -> dict[str, Any]:
        capture: dict[str, Any] = {
            "url": None,
            "headers": None,
            "payload": None,
        }

        async def on_request(request):
            try:
                url = request.url
                if (
                    "bapi-etail.wallmob.com/reports/performance" not in url
                    or "top_products" not in url
                ):
                    return
                headers = await request.all_headers()
                blocked = {
                    "host",
                    "content-length",
                    "connection",
                    "accept-encoding",
                    "cookie",
                }
                capture["url"] = url
                capture["headers"] = {
                    k: v
                    for k, v in headers.items()
                    if k.lower() not in blocked and not k.startswith(":")
                }
            except Exception:
                pass

        async def on_response(response):
            try:
                url = response.url
                if (
                    "bapi-etail.wallmob.com/reports/performance" in url
                    and "top_products" in url
                    and response.ok
                ):
                    payload = await response.json()
                    if isinstance(payload, dict):
                        capture["payload"] = payload
            except Exception:
                pass

        context.on("request", on_request)
        context.on("response", on_response)
        return capture

    def _save_wallmob_capture(
        self,
        park: str,
        capture: dict[str, Any],
        data: dict[str, Any] | None = None,
    ) -> None:
        url = capture.get("url")
        headers = capture.get("headers")
        if url and isinstance(headers, dict) and headers:
            self.wall_report_templates[park] = {
                "url": url,
                "headers": headers,
            }

        payload = capture.get("payload")
        if data is not None and isinstance(payload, dict):
            products = wallmob_products_from_payload(payload)
            data["product_sales"] = products
            data["_product_sales_complete"] = True

    async def _wallmob_history_payload(
        self,
        context: BrowserContext,
        park: str,
        target_date: date,
    ) -> dict[str, Any] | None:
        template = self.wall_report_templates.get(park)
        if not template:
            print(
                f"[{park}] WALLMOB_HISTORY mangler fanget performance-request",
                flush=True,
            )
            return None

        original_url = str(template.get("url") or "")
        headers = template.get("headers") or {}
        parts = urlsplit(original_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))

        start = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            0,
            0,
            0,
            tzinfo=OSLO,
        )
        end = start + timedelta(days=1) - timedelta(seconds=1)
        query["starttime"] = str(int(start.timestamp()))
        query["endtime"] = str(int(end.timestamp()))
        query["include_vat"] = "1"

        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
        response = await context.request.get(
            url,
            headers=headers,
            timeout=30000,
        )
        if response.status >= 400:
            print(
                f"[{park}] WALLMOB_HISTORY HTTP {response.status} "
                f"date={target_date.isoformat()}; template=True; "
                f"header_names={sorted(headers.keys())}",
                flush=True,
            )
            return None

        try:
            payload = await response.json()
        except Exception:
            body = await response.text()
            print(
                f"[{park}] WALLMOB_HISTORY non-json "
                f"date={target_date.isoformat()}: "
                + re.sub(r"\s+", " ", body)[:300],
                flush=True,
            )
            return None

        return payload if isinstance(payload, dict) else None

    async def fetch_wallmob(
        self, park: str, username: str, password: str, login_type: str
    ) -> dict[str, Any]:
        async def read_live(
            context: BrowserContext,
            page: Page,
        ) -> dict[str, Any]:
            data = await read_wallmob(page)

            captured = await capture_wallmob_performance_request(page, park)
            if captured is not None:
                self.wall_report_templates[park] = {
                    "url": captured["url"],
                    "headers": captured["headers"],
                }
                products = wallmob_products_from_payload(captured["payload"])
                data["product_sales"] = products
                live_turnover = float(data.get("turnover") or 0)
                data["_product_sales_complete"] = bool(products) or live_turnover == 0
            else:
                data["_product_sales_complete"] = False

            self.wall_states[park] = await context.storage_state()
            return data

        state = self.wall_states.get(park)
        if state is not None:
            context = await self._new_context(state)
            try:
                page = await context.new_page()
                await page.goto(
                    WALLMOB_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(900)
                body = await page.locator("body").inner_text()
                if "Dagens omsetning" in body:
                    return await read_live(context, page)
            except Exception as exc:
                print(
                    f"[{park}] Wallmob lagret sesjon feilet: {exc}",
                    flush=True,
                )
            finally:
                await context.close()

        context = await self._new_context()
        try:
            page = await login_wallmob(context, username, password, login_type)
            return await read_live(context, page)
        finally:
            await context.close()

    async def fetch_wallmob_products_for_date(
        self,
        park: str,
        username: str,
        password: str,
        login_type: str,
        target_date: date,
    ) -> list[dict[str, Any]] | None:
        # Ensure we have both an authenticated browser state and the exact
        # performance request template generated by Wallmob itself.
        if self.wall_states.get(park) is None or park not in self.wall_report_templates:
            await self.fetch_wallmob(park, username, password, login_type)

        for attempt in range(2):
            state = self.wall_states.get(park)
            if state is None:
                return None

            context = await self._new_context(state)
            try:
                payload = await self._wallmob_history_payload(
                    context,
                    park,
                    target_date,
                )
                if payload is not None:
                    self.wall_states[park] = await context.storage_state()
                    return wallmob_products_from_payload(payload)
            finally:
                await context.close()

            if attempt == 0:
                print(
                    f"[{park}] Wallmob historikk prøver ny innlogging "
                    f"for {target_date.isoformat()}",
                    flush=True,
                )
                self.wall_report_templates.pop(park, None)
                self.wall_states[park] = None
                await self.fetch_wallmob(park, username, password, login_type)

        print(
            f"[{park}] Wallmob historikk ga ingen data for "
            f"{target_date.isoformat()} etter ny innlogging",
            flush=True,
        )
        return None

    async def fetch_nordpay_products_for_date(
        self,
        target_date: date,
    ) -> list[dict[str, Any]] | None:
        if self.nord_state is None:
            await self.fetch_nordpay()

        context = await self._new_context(self.nord_state)
        try:
            page = await context.new_page()
            await page.goto(
                f"{NORDPAY_BASE_URL}/dashboard",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(700)
            body = await page.locator("body").inner_text()
            if "DAGENS SALG" not in body.upper():
                return None

            products = await nordpay_products_for_date(page, target_date)
            self.nord_state = await context.storage_state()
            return products
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
                        if not self.nord_product_probe_done:
                            await probe_nordpay_products(page)
                            self.nord_product_probe_done = True
                        data["product_sales"] = await read_nordpay_daily_products(page)
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
            if not self.nord_product_probe_done:
                await probe_nordpay_products(page)
                self.nord_product_probe_done = True
            data["product_sales"] = await read_nordpay_daily_products(page)
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
            combined_products = combine_product_sales(
                wall.get("product_sales"), nord.get("product_sales")
            )
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
                "product_sales": combined_products,
                "hourly_revenue": hourly,
                "nordpay_time": hourly,
                "hourly_status": "Skybasert timegraf – differanse mellom live snapshots",
            }
            await ingest(client, "sorlandet", payload)
            if wall.get("_product_sales_complete"):
                await ingest_product_history(
                    client,
                    "sorlandet",
                    now.date().isoformat(),
                    combined_products,
                )
            else:
                print(
                    "[sorlandet] PRODUKTHISTORIKK hoppet over – "
                    "Wallmob produktlisten var ikke komplett",
                    flush=True,
                )
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
                "product_sales": combine_product_sales(wall.get("product_sales")),
                "hourly_revenue": hourly,
                "nordpay_time": hourly,
                "hourly_status": "Skybasert timegraf – differanse mellom live snapshots",
            }
            await ingest(client, "triaden", payload)
            if wall.get("_product_sales_complete"):
                await ingest_product_history(
                    client,
                    "triaden",
                    now.date().isoformat(),
                    payload.get("product_sales") or [],
                )
            else:
                print(
                    "[triaden] PRODUKTHISTORIKK hoppet over – "
                    "Wallmob produktlisten var ikke komplett",
                    flush=True,
                )
        except Exception as exc:
            print(f"[triaden] FEIL: {exc}", flush=True)
    else:
        print(
            "[triaden] Hopper over – mangler WALLMOB_TRIADEN_USERNAME/PASSWORD.",
            flush=True,
        )


async def next_missing_product_date(
    client: httpx.AsyncClient,
    park: str,
    start_date: date,
    end_date: date,
) -> date | None:
    try:
        r = await client.get(
            f"{CLOUD_URL}/api/products/next-missing-date",
            params={
                "park": park,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            headers={"X-PWS-Key": APP_KEY},
            timeout=20,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        raw = (r.json() or {}).get("next_missing_date")
        return date.fromisoformat(raw) if raw else None
    except Exception as exc:
        print(f"[backfill] Kunne ikke lese fremdrift for {park}: {exc}", flush=True)
        return None


async def backfill_product_history_once(
    collector: CloudCollector,
    client: httpx.AsyncClient,
) -> None:
    start_date = date(2026, 1, 1)
    end_date = datetime.now(OSLO).date() - timedelta(days=1)
    if end_date < start_date:
        return

    # One missing date for each park per cycle. Everything is sequential so the
    # 512 MB Render worker never keeps multiple browser contexts alive.
    for park in ("sorlandet", "triaden"):
        target = await next_missing_product_date(client, park, start_date, end_date)
        if target is None:
            continue

        try:
            if park == "sorlandet":
                wall = await collector.fetch_wallmob_products_for_date(
                    park,
                    SORLANDET_WALLMOB_USER,
                    SORLANDET_WALLMOB_PASSWORD,
                    SORLANDET_WALLMOB_TYPE,
                    target,
                )
                if wall is None:
                    raise RuntimeError("Wallmob historikk kunne ikke leses")

                nord = await collector.fetch_nordpay_products_for_date(target)
                if nord is None:
                    raise RuntimeError("NordPay historikk kunne ikke leses")

                products = combine_product_sales(wall, nord)
            else:
                wall = await collector.fetch_wallmob_products_for_date(
                    park,
                    TRIADEN_WALLMOB_USER,
                    TRIADEN_WALLMOB_PASSWORD,
                    TRIADEN_WALLMOB_TYPE,
                    target,
                )
                if wall is None:
                    raise RuntimeError("Wallmob historikk kunne ikke leses")
                products = combine_product_sales(wall)

            await ingest_product_history(
                client,
                park,
                target.isoformat(),
                products,
                source="backfill",
            )
            print(
                f"[backfill] {park} {target.isoformat()} ferdig – "
                f"{len(products)} produkter",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[backfill] {park} {target.isoformat()} FEIL: {exc}",
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
    print(f"Build: {COLLECTOR_BUILD}", flush=True)
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
                    await backfill_product_history_once(collector, client)
                except Exception as exc:
                    print(f"[collector] FEIL: {exc}", flush=True)

                elapsed = (datetime.now(OSLO) - started).total_seconds()
                await asyncio.sleep(max(5, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
