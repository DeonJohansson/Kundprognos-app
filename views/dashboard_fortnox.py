"""
OptiNord Fortnox-dashboard.

Visar exakt det som specificerats:
- Ackumulerad försäljning i år (YTD) vs samma period förra året
- Månadens försäljning hittills (MTD) vs budget, inkl. GP
- Fakturerat per månad i år: budget, utfall, GP, jämfört med förra året
- Live idag: offerter/ordrar/fakturerat, plus samma för senaste arbetsdagen
- 5 största ordrar denna vecka

Alla belopp visas exklusive moms.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import streamlit as st
# Dashboardens nycklar ligger under [dashboard] i Secrets (faller tillbaka på rotnivån)
SEC = st.secrets["dashboard"] if "dashboard" in st.secrets else st.secrets
import datetime
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

from dash_token_store import load_refresh_token, save_refresh_token


FORTNOX_TOKEN_FILE = "fortnox_refresh_token.txt"
TOKEN_URL = "https://apps.fortnox.se/oauth-v1/token"
API_BASE = "https://api.fortnox.se/3"

# ---------------------------------------------------------------------------
# Delad hastighetsbegränsare - alla Fortnox-anrop (sekventiella och parallella)
# går igenom denna, så vi aldrig sprängar Fortnox gräns på 300 anrop/minut,
# oavsett hur många trådar som körs samtidigt.
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_last_request_time = [0.0]
MIN_REQUEST_INTERVAL = 0.25  # max ~4 anrop/sek = 240/min, god marginal


def rate_limited_get(url, headers=None, params=None, timeout=30):
    for attempt in range(6):
        with _rate_lock:
            wait = _last_request_time[0] + MIN_REQUEST_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_request_time[0] = time.time()

        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
            time.sleep(retry_after)
            continue
        return resp
    return resp
CACHE_TTL_SECONDS = 15 * 60          # aktuell månad / snabba vyer
CLOSED_MONTH_TTL_SECONDS = 24 * 3600  # avslutade månader ändras inte

# Antagande: standard svensk moms 25 %. Fakturans "Totalt" är inkl. moms;
# vi räknar bort moms med denna sats för de snabba vyerna (Live idag, YTD,
# veckans ordrar). Om ni har blandade momssatser kan detta behöva justeras.
VAT_RATE = 0.25

MONTH_NAMES = ["Januari", "Februari", "Mars", "April", "Maj", "Juni",
               "Juli", "Augusti", "September", "Oktober", "November", "December"]

# Budget 2026, beräknat från Mål_2026.xlsx (dagsbudget x antal arbetsdagar/månad)
from dash_config import load_config  # noqa: E402

MONTHLY_BUDGET = {int(k): v for k, v in load_config()["monthly_budget"].items()}


# ---------------------------------------------------------------------------
# Svenska röda dagar (beräknas algoritmiskt, fungerar för alla år)
# ---------------------------------------------------------------------------

def easter_sunday(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def swedish_public_holidays(year):
    easter = easter_sunday(year)
    holidays = {
        datetime.date(year, 1, 1),
        datetime.date(year, 1, 6),
        easter - datetime.timedelta(days=2),
        easter,
        easter + datetime.timedelta(days=1),
        datetime.date(year, 5, 1),
        easter + datetime.timedelta(days=39),
        easter + datetime.timedelta(days=49),
        datetime.date(year, 6, 6),
        datetime.date(year, 12, 25),
        datetime.date(year, 12, 26),
    }
    for d in range(20, 27):  # Midsommardagen: lördag 20-26 juni
        dd = datetime.date(year, 6, d)
        if dd.weekday() == 5:
            holidays.add(dd)
            break
    for month, day_range in ((10, range(31, 32)), (11, range(1, 7))):  # Alla helgons dag
        for d in day_range:
            dd = datetime.date(year, month, d)
            if dd.weekday() == 5:
                holidays.add(dd)
                break
    return holidays


def is_business_day(d):
    return d.weekday() < 5 and d not in swedish_public_holidays(d.year)


def last_business_day(before_date):
    d = before_date - datetime.timedelta(days=1)
    while not is_business_day(d):
        d -= datetime.timedelta(days=1)
    return d


def business_days_elapsed(year, month, up_to_date):
    d = datetime.date(year, month, 1)
    count = 0
    while d <= up_to_date:
        if is_business_day(d):
            count += 1
        d += datetime.timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Fortnox-anslutning
# ---------------------------------------------------------------------------

@st.cache_resource
def _token_state():
    return {"access_token": None, "expires_at": 0}


def get_access_token():
    state = _token_state()
    if state["access_token"] and time.time() < state["expires_at"]:
        return state["access_token"]

    refresh_token = load_refresh_token(FORTNOX_TOKEN_FILE, "FORTNOX_REFRESH_TOKEN")
    resp = requests.post(
        TOKEN_URL,
        auth=(SEC["FORTNOX_CLIENT_ID"], SEC["FORTNOX_CLIENT_SECRET"]),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Kunde inte ansluta till Fortnox ({resp.status_code}): {resp.text}")
    tokens = resp.json()
    save_refresh_token(tokens["refresh_token"], FORTNOX_TOKEN_FILE)

    state["access_token"] = tokens["access_token"]
    state["expires_at"] = time.time() + tokens.get("expires_in", 3600) - 300  # 5 min marginal
    return state["access_token"]


def fetch_all_pages(access_token, endpoint, params=None, limit=500):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = dict(params or {})
    params["limit"] = limit
    page = 1
    all_items = []
    list_key = None
    while True:
        params["page"] = page
        resp = rate_limited_get(f"{API_BASE}/{endpoint}", headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Fortnox-fel vid {endpoint} ({resp.status_code}): {resp.text}")
        data = resp.json()
        if list_key is None:
            candidates = [k for k in data.keys() if isinstance(data[k], list)]
            list_key = candidates[0] if candidates else None
        if list_key is None:
            return data
        items = data[list_key]
        all_items.extend(items)
        meta = data.get("MetaInformation", {})
        if page >= meta.get("@TotalPages", 1) or not items:
            break
        page += 1
    return all_items


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_list(endpoint, params, _access_token):
    return fetch_all_pages(_access_token, endpoint, params)


def net_amount(total_incl_vat):
    """Snabb uppskattning av belopp exkl. moms (25%) från inkl.-belopp."""
    return (total_incl_vat or 0) / (1 + VAT_RATE)


# ---------------------------------------------------------------------------
# Full fakturadata (för GP) - dyrare, cachas smart per månad
# ---------------------------------------------------------------------------

@st.cache_resource
def _full_invoice_cache():
    return {}


def get_full_invoices_for_month(year, month, access_token):
    cache = _full_invoice_cache()
    key = (year, month)
    now = time.time()
    today = datetime.date.today()
    is_current_month = (year, month) == (today.year, today.month)
    ttl = CACHE_TTL_SECONDS if is_current_month else CLOSED_MONTH_TTL_SECONDS

    if key in cache and (now - cache[key][0]) < ttl:
        return cache[key][1]

    start = datetime.date(year, month, 1)
    end = (datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)) - datetime.timedelta(days=1)
    short_list = fetch_all_pages(access_token, "invoices", {"fromdate": start.isoformat(), "todate": end.isoformat()})

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    docs = [inv["DocumentNumber"] for inv in short_list if not inv.get("Cancelled")]

    def fetch_one(doc):
        resp = rate_limited_get(f"{API_BASE}/invoices/{doc}", headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json()["Invoice"]
        return None

    full_list = []
    # Fortnox tillåter 300 anrop/min (~5/sek) - 8 parallella trådar håller oss
    # gott och väl under gränsen men är ändå ~8x snabbare än ett i taget.
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_one, doc) for doc in docs]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                full_list.append(result)

    cache[key] = (now, full_list)
    return full_list


def _to_float(val):
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def invoice_gp(invoice):
    """Summerar bruttovinst (Fortnox 'ContributionValue' per fakturarad)."""
    rows = invoice.get("InvoiceRows") or []
    return sum(_to_float(r.get("ContributionValue")) for r in rows)


# ---------------------------------------------------------------------------
# Sida
# ---------------------------------------------------------------------------

col_a, col_b = st.columns([5, 1])
with col_a:
    st.title("Fortnox - Försäljningsöversikt")
with col_b:
    if st.button("🔄 Uppdatera nu"):
        fetch_list.clear()
        _full_invoice_cache().clear()
        st.rerun()

try:
    access_token = get_access_token()
except Exception as e:
    st.error(f"Kunde inte ansluta till Fortnox: {e}")
    st.stop()

try:
    today = datetime.date.today()
    year = today.year

    # ===========================================================================
    # Datainsamling (all logik oförändrad, bara rendering-delen är ny)
    # ===========================================================================

    last_biz_day = last_business_day(today)


    def day_summary(date_str):
        params = {"fromdate": date_str, "todate": date_str}
        offers = fetch_list("offers", params, access_token)
        orders = fetch_list("orders", params, access_token)
        invoices = fetch_list("invoices", params, access_token)

        def summarize(items):
            active = [i for i in items if not i.get("Cancelled") and i.get("Currency") == "SEK"]
            return len(active), net_amount(sum(_to_float(i.get("Total")) for i in active))

        return {
            "offers": summarize(offers),
            "orders": summarize(orders),
            "invoices": summarize(invoices),
        }


    with st.spinner("Hämtar Live idag..."):
        today_data = day_summary(today.isoformat())
        last_biz_data = day_summary(last_biz_day.isoformat())

    # ---- YTD ----
    ytd_start = datetime.date(year, 1, 1)
    ytd_invoices = fetch_list("invoices", {"fromdate": ytd_start.isoformat(), "todate": today.isoformat()}, access_token)
    ytd_sales = net_amount(sum(_to_float(i.get("Total")) for i in ytd_invoices if not i.get("Cancelled") and i.get("Currency") == "SEK"))

    prior_year = year - 1
    prior_end = today.replace(year=prior_year) if not (today.month == 2 and today.day == 29) else datetime.date(prior_year, 2, 28)
    prior_start = datetime.date(prior_year, 1, 1)
    ytd_prior_invoices = fetch_list("invoices", {"fromdate": prior_start.isoformat(), "todate": prior_end.isoformat()}, access_token)
    ytd_prior_sales = net_amount(sum(_to_float(i.get("Total")) for i in ytd_prior_invoices if not i.get("Cancelled") and i.get("Currency") == "SEK"))

    diff_kr = ytd_sales - ytd_prior_sales
    diff_pct = (diff_kr / ytd_prior_sales * 100) if ytd_prior_sales else 0

    # ---- MTD ----
    month_start = datetime.date(year, today.month, 1)
    days_elapsed = business_days_elapsed(year, today.month, today)
    budget_info = MONTHLY_BUDGET[today.month]
    budget_so_far = budget_info["sales"] / budget_info["days"] * days_elapsed
    gp_budget_so_far = budget_info["gp"] / budget_info["days"] * days_elapsed

    with st.spinner("Räknar ut GP för innevarande månad (kan ta en stund första gången)..."):
        mtd_full_invoices = get_full_invoices_for_month(year, today.month, access_token)

    mtd_actual = sum(_to_float(inv.get("Net")) for inv in mtd_full_invoices if inv.get("Currency") == "SEK")
    mtd_gp = sum(invoice_gp(inv) for inv in mtd_full_invoices if inv.get("Currency") == "SEK")
    mtd_gp_pct = (mtd_gp / mtd_actual * 100) if mtd_actual else 0
    mtd_budget_pct = (mtd_actual / budget_so_far * 100) if budget_so_far else 0
    mtd_gp_of_budget_pct = (mtd_gp / gp_budget_so_far * 100) if gp_budget_so_far else 0

    mtd_diff_kr = mtd_actual - budget_so_far
    mtd_diff_pct = (mtd_diff_kr / budget_so_far * 100) if budget_so_far else 0

    # ---- Fakturerat per månad i år ----
    monthly_rows = []
    with st.spinner("Bygger månadsdata (GP för varje avslutad månad hämtas och cachas)..."):
        for m in range(1, today.month + 1):
            m_start = datetime.date(year, m, 1)
            m_end = (datetime.date(year, m + 1, 1) if m < 12 else datetime.date(year + 1, 1, 1)) - datetime.timedelta(days=1)
            if m_end > today:
                m_end = today

            m_invoices = fetch_list("invoices", {"fromdate": m_start.isoformat(), "todate": m_end.isoformat()}, access_token)
            utfall = net_amount(sum(_to_float(i.get("Total")) for i in m_invoices if not i.get("Cancelled") and i.get("Currency") == "SEK"))

            py_start = datetime.date(prior_year, m, 1)
            py_end = (datetime.date(prior_year, m + 1, 1) if m < 12 else datetime.date(prior_year + 1, 1, 1)) - datetime.timedelta(days=1)
            py_invoices = fetch_list("invoices", {"fromdate": py_start.isoformat(), "todate": py_end.isoformat()}, access_token)
            utfall_forra_aret = net_amount(sum(_to_float(i.get("Total")) for i in py_invoices if not i.get("Cancelled") and i.get("Currency") == "SEK"))

            full_invoices_m = get_full_invoices_for_month(year, m, access_token)
            gp_m = sum(invoice_gp(inv) for inv in full_invoices_m if inv.get("Currency") == "SEK")

            budget_m = MONTHLY_BUDGET[m]["sales"]

            monthly_rows.append({
                "month": MONTH_NAMES[m - 1][:3],
                "budget": round(budget_m),
                "utfall": round(utfall),
                "gp": round(gp_m),
                "utfall_forra_aret": round(utfall_forra_aret),
            })

    # ---- 5 största ordrar denna vecka ----
    week_start = today - datetime.timedelta(days=today.weekday())
    week_orders = fetch_list("orders", {"fromdate": week_start.isoformat(), "todate": today.isoformat()}, access_token)
    week_orders = [o for o in week_orders if not o.get("Cancelled") and o.get("Currency") == "SEK"]
    top5 = sorted(week_orders, key=lambda o: _to_float(o.get("Total")), reverse=True)[:5]

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    top5_rows = []
    for o in top5:
        doc = o["DocumentNumber"]
        resp = rate_limited_get(f"{API_BASE}/orders/{doc}", headers=headers, timeout=30)
        net = net_amount(_to_float(o.get("Total")))
        if resp.status_code == 200:
            net = _to_float(resp.json()["Order"].get("Net")) or net
        top5_rows.append({"customer": o.get("CustomerName", ""), "amount": round(net)})

    # ===========================================================================
    # Kortbaserad HTML-dashboard (en sida, ingen scroll)
    # ===========================================================================

    def fmt_kr(n):
        return f"{n:,.0f} kr".replace(",", " ")


    def fmt_kr_short(n):
        a = abs(n)
        if a >= 1_000_000:
            return f"{n/1_000_000:.1f} Mkr".replace(".0 ", " ")
        if a >= 1_000:
            return f"{round(n/1000)} tkr"
        return f"{n:.0f} kr"


    months_json = json.dumps([r["month"] for r in monthly_rows])
    budget_json = json.dumps([r["budget"] for r in monthly_rows])
    utfall_json = json.dumps([r["utfall"] for r in monthly_rows])
    gp_json = json.dumps([r["gp"] for r in monthly_rows])
    last_year_json = json.dumps([r["utfall_forra_aret"] for r in monthly_rows])

    top5_html = "".join(
        f'<div class="order-row"><span class="order-cust">{r["customer"]}</span>'
        f'<span class="order-amt">{fmt_kr_short(r["amount"])}</span></div>'
        for r in top5_rows
    ) or '<div class="empty-note">Inga ordrar denna vecka ännu.</div>'

    mtd_budget_pct_clamped = max(0, min(mtd_budget_pct, 100))
    mtd_gp_pct_clamped = max(0, min(mtd_gp_of_budget_pct, 100))

    html = f"""
    <!DOCTYPE html>
    <html lang="sv">
    <head>
    <meta charset="UTF-8">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700;800&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
    <style>
      :root{{
        --paper:#F5F7FA; --card:#FFFFFF; --ink:#182F58; --ink-soft:#5A6B85;
        --rule:#E1E6ED; --green:#8AC249; --green-deep:#5E9130; --navy-mid:#2C4874; --alert:#D64550;
      }}
      *{{box-sizing:border-box; margin:0; padding:0;}}
      body{{background:var(--paper); font-family:'Inter',sans-serif; color:var(--ink);}}
      .grid{{display:grid; grid-template-columns:repeat(12,1fr); grid-template-rows:auto auto auto; gap:14px; padding:4px;}}
      .card{{background:var(--card); border:1px solid var(--rule); border-radius:12px; padding:16px 18px; display:flex; flex-direction:column;}}
      .label{{font-family:'IBM Plex Mono',monospace; font-size:10.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--ink-soft); margin-bottom:6px;}}
      .big{{font-family:'Manrope',sans-serif; font-weight:800; font-size:26px; line-height:1.1;}}
      .sub{{font-family:'IBM Plex Mono',monospace; font-size:12px; margin-top:4px;}}
      .sub.up{{color:var(--green-deep);}} .sub.down{{color:var(--alert);}}
      .kpi-row{{grid-column:1/13; display:grid; grid-template-columns:repeat(4,1fr); gap:14px;}}
      .mid-row{{grid-column:1/13; display:grid; grid-template-columns:2fr 1fr 1fr; gap:14px;}}
      .bottom-row{{grid-column:1/13; display:grid; grid-template-columns:1fr 1fr; gap:14px;}}
      .chart-card{{height:230px;}}
      .chart-card canvas{{max-height:190px;}}
      .progress-track{{background:var(--rule); border-radius:6px; height:10px; overflow:hidden; margin-top:10px;}}
      .progress-fill{{height:100%; border-radius:6px; background:var(--green);}}
      .progress-fill.over{{background:var(--navy-mid);}}
      .today-row{{display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--rule); font-size:13px;}}
      .today-row:last-child{{border-bottom:none;}}
      .today-row .n{{font-family:'IBM Plex Mono',monospace; font-weight:600;}}
      .order-row{{display:flex; justify-content:space-between; padding:7px 0; border-bottom:1px solid var(--rule); font-size:13px;}}
      .order-row:last-child{{border-bottom:none;}}
      .order-amt{{font-family:'IBM Plex Mono',monospace; font-weight:600;}}
      .empty-note{{color:var(--ink-soft); font-size:13px; padding:8px 0;}}
      h3.card-title{{font-family:'Manrope',sans-serif; font-weight:700; font-size:14px; margin-bottom:8px;}}
    </style>
    </head>
    <body>
    <div class="grid">

      <div class="kpi-row">
        <div class="card">
          <div class="label">Fakturerat {year} (YTD)</div>
          <div class="big">{fmt_kr_short(ytd_sales)}</div>
          <div class="sub {'up' if diff_pct>=0 else 'down'}">{diff_pct:+.1f}% vs {prior_year} ({fmt_kr_short(diff_kr)})</div>
        </div>
        <div class="card">
          <div class="label">MTD utfall vs budget</div>
          <div class="big">{mtd_budget_pct:.0f}%</div>
          <div class="progress-track"><div class="progress-fill {'over' if mtd_budget_pct>100 else ''}" style="width:{mtd_budget_pct_clamped}%"></div></div>
          <div class="sub">{fmt_kr_short(mtd_actual)} av {fmt_kr_short(budget_so_far)}</div>
        </div>
        <div class="card">
          <div class="label">MTD GP</div>
          <div class="big">{mtd_gp_pct:.1f}%</div>
          <div class="progress-track"><div class="progress-fill {'over' if mtd_gp_of_budget_pct>100 else ''}" style="width:{mtd_gp_pct_clamped}%"></div></div>
          <div class="sub">{fmt_kr_short(mtd_gp)} av budget {fmt_kr_short(gp_budget_so_far)}</div>
        </div>
        <div class="card">
          <div class="label">Live idag</div>
          <div class="today-row"><span>Offerter</span><span class="n">{today_data['offers'][0]} · {fmt_kr_short(today_data['offers'][1])}</span></div>
          <div class="today-row"><span>Ordrar</span><span class="n">{today_data['orders'][0]} · {fmt_kr_short(today_data['orders'][1])}</span></div>
          <div class="today-row"><span>Fakturerat</span><span class="n">{today_data['invoices'][0]} · {fmt_kr_short(today_data['invoices'][1])}</span></div>
        </div>
      </div>

      <div class="mid-row">
        <div class="card chart-card">
          <h3 class="card-title">Budget vs Utfall per månad</h3>
          <canvas id="budgetChart"></canvas>
        </div>
        <div class="card chart-card">
          <h3 class="card-title">GP kr per månad</h3>
          <canvas id="gpChart"></canvas>
        </div>
        <div class="card chart-card">
          <h3 class="card-title">vs {prior_year}</h3>
          <canvas id="yoyChart"></canvas>
        </div>
      </div>

      <div class="bottom-row">
        <div class="card">
          <h3 class="card-title">Senaste arbetsdagen ({last_biz_day.strftime('%-d/%-m')})</h3>
          <div class="today-row"><span>Offerter</span><span class="n">{last_biz_data['offers'][0]} · {fmt_kr_short(last_biz_data['offers'][1])}</span></div>
          <div class="today-row"><span>Ordrar</span><span class="n">{last_biz_data['orders'][0]} · {fmt_kr_short(last_biz_data['orders'][1])}</span></div>
          <div class="today-row"><span>Fakturerat</span><span class="n">{last_biz_data['invoices'][0]} · {fmt_kr_short(last_biz_data['invoices'][1])}</span></div>
        </div>
        <div class="card">
          <h3 class="card-title">5 största ordrar denna vecka</h3>
          {top5_html}
        </div>
      </div>

    </div>

    <script>
    const months = {months_json};
    const budget = {budget_json};
    const utfall = {utfall_json};
    const gp = {gp_json};
    const lastYear = {last_year_json};

    const commonOpts = {{
      responsive:true, maintainAspectRatio:false,
      plugins:{{ legend:{{display:false}} }},
      scales:{{
        x:{{ grid:{{display:false}}, ticks:{{font:{{family:"IBM Plex Mono",size:9}}, color:"#5A6B85"}} }},
        y:{{ grid:{{color:"#E1E6ED"}}, ticks:{{font:{{family:"IBM Plex Mono",size:9}}, color:"#5A6B85"}} }}
      }}
    }};

    new Chart(document.getElementById('budgetChart'), {{
      type:'bar',
      data:{{ labels:months, datasets:[
        {{label:'Budget', data:budget, backgroundColor:'#2C4874', borderRadius:2}},
        {{label:'Utfall', data:utfall, backgroundColor:'#8AC249', borderRadius:2}}
      ]}},
      options:{{...commonOpts, plugins:{{legend:{{display:true, position:'top', labels:{{boxWidth:10, font:{{size:10}}}}}}}}}}
    }});

    new Chart(document.getElementById('gpChart'), {{
      type:'bar',
      data:{{ labels:months, datasets:[{{data:gp, backgroundColor:'#5E9130', borderRadius:2}}] }},
      options:commonOpts
    }});

    new Chart(document.getElementById('yoyChart'), {{
      type:'bar',
      data:{{ labels:months, datasets:[
        {{label:'{year}', data:utfall, backgroundColor:'#8AC249', borderRadius:2}},
        {{label:'{prior_year}', data:lastYear, backgroundColor:'#D6D0BE', borderRadius:2}}
      ]}},
      options:{{...commonOpts, plugins:{{legend:{{display:true, position:'top', labels:{{boxWidth:10, font:{{size:10}}}}}}}}}}
    }});
    </script>
    </body>
    </html>
    """

    st.components.v1.html(html, height=760, scrolling=False)

    st.caption(
        f"Data hämtad {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}. Alla belopp exkl. moms (25% schablon för snabba vyer). "
        "GP baseras på Fortnox 'ContributionValue' per fakturarad."
    )
except Exception as e:
    st.error(f"Ett fel uppstod när dashboarden byggdes: {e}")
    st.stop()
