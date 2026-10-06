"""
OptiNord Salesforce-dashboard - Pipeline & aktiviteter.
 
Sida 2 i samma app som Fortnox-dashboarden. Anslutningen till Salesforce
sköts helt i molnet - första gången du besöker sidan visas en "Anslut till
Salesforce"-knapp. Ingen lokal dator eller lokala filer behövs någonsin.
"""
 
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import streamlit as st
# Dashboardens nycklar ligger under [dashboard] i Secrets (faller tillbaka på rotnivån)
SEC = st.secrets["dashboard"] if "dashboard" in st.secrets else st.secrets
import base64
import datetime
import hashlib
import json
import secrets as pysecrets
import time
import urllib.parse
 
import pandas as pd
import requests
import streamlit as st
 
from dash_token_store import load_refresh_token, save_refresh_token
 
 
SF_TOKEN_FILE = "salesforce_refresh_token.txt"
LOGIN_BASE = "https://login.salesforce.com"
AUTH_URL = f"{LOGIN_BASE}/services/oauth2/authorize"
TOKEN_URL = f"{LOGIN_BASE}/services/oauth2/token"
API_VERSION = "v60.0"
CACHE_TTL_SECONDS = 15 * 60
 
# Måste EXAKT matcha Callback URL i din Salesforce External Client App
REDIRECT_URI = SEC.get("SALESFORCE_REDIRECT_URI", "https://optinord-kundprognos.streamlit.app/salesforce")
 
 
# ---------------------------------------------------------------------------
# Inloggningsflöde (körs helt i molnet)
# ---------------------------------------------------------------------------
 
@st.cache_resource
def _pkce_store():
    return {}
 
 
def build_authorize_url():
    # PKCE: generera en hemlig verifier och en hashad "challenge" av den.
    # Salesforce kräver detta numera för Authorization Code-flödet.
    # Sparas i en processgemensam cache (nyckel = state) istället för
    # st.session_state, eftersom Streamlit Cloud kan starta en ny session
    # när webbläsaren skickas iväg till Salesforce och tillbaka.
    code_verifier = pysecrets.token_urlsafe(64)
    state = pysecrets.token_urlsafe(16)
    _pkce_store()[state] = code_verifier
 
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("utf-8")).digest()
    ).rstrip(b"=").decode("utf-8")
 
    params = {
        "client_id": SEC["SALESFORCE_CONSUMER_KEY"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "api refresh_token offline_access",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
 
 
def exchange_code_for_tokens(code, state):
    code_verifier = _pkce_store().pop(state, None)
    if not code_verifier:
        raise RuntimeError(
            "Inloggningssessionen gick förlorad (kod-verifiering saknas). "
            "Klicka 'Anslut till Salesforce' igen och slutför inloggningen utan att öppna nya flikar under tiden."
        )
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": SEC["SALESFORCE_CONSUMER_KEY"],
            "client_secret": SEC["SALESFORCE_CONSUMER_SECRET"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Kunde inte slutföra inloggningen ({resp.status_code}): {resp.text}")
    return resp.json()
 
 
# Om Salesforce precis skickat tillbaka en auktoriseringskod, byt in den nu
if "code" in st.query_params:
    try:
        tokens = exchange_code_for_tokens(st.query_params["code"], st.query_params.get("state", ""))
        save_refresh_token(tokens["refresh_token"], SF_TOKEN_FILE)
        st.query_params.clear()
        st.success("Ansluten till Salesforce! Laddar dashboarden...")
        time.sleep(1)
        st.rerun()
    except Exception as e:
        st.error(f"Inloggningen misslyckades: {e}")
        st.stop()
 
existing_refresh_token = load_refresh_token(SF_TOKEN_FILE, "SALESFORCE_REFRESH_TOKEN")
 
if not existing_refresh_token:
    st.title("Salesforce - Pipeline & aktiviteter")
    st.info("Den här sidan är inte ansluten till Salesforce ännu.")
    try:
        st.link_button("🔗 Anslut till Salesforce", build_authorize_url())
    except Exception:
        st.error(
            "SALESFORCE_CONSUMER_KEY/SECRET saknas i Streamlit secrets. "
            "Lägg till dem under Manage app -> Settings -> Secrets, ladda om sidan, och försök igen."
        )
    st.stop()
 
 
# ---------------------------------------------------------------------------
# Salesforce-anslutning (efter att vi har en refresh-token)
# ---------------------------------------------------------------------------
 
@st.cache_resource
def _sf_token_state():
    return {"access_token": None, "instance_url": None, "expires_at": 0}
 
 
def get_session():
    state = _sf_token_state()
    if state["access_token"] and time.time() < state["expires_at"]:
        return state["access_token"], state["instance_url"]
 
    refresh_token = load_refresh_token(SF_TOKEN_FILE, "SALESFORCE_REFRESH_TOKEN")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": SEC["SALESFORCE_CONSUMER_KEY"],
            "client_secret": SEC["SALESFORCE_CONSUMER_SECRET"],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Kunde inte ansluta till Salesforce ({resp.status_code}): {resp.text}\n"
            "Anslutningen kan ha återkallats - klicka 'Anslut till Salesforce' igen nedan."
        )
    data = resp.json()
    if data.get("refresh_token"):
        save_refresh_token(data["refresh_token"], SF_TOKEN_FILE)
 
    state["access_token"] = data["access_token"]
    state["instance_url"] = data["instance_url"]
    state["expires_at"] = time.time() + 3300  # ~55 min marginal (Salesforce anger ej alltid expires_in)
    return data["access_token"], data["instance_url"]
 
 
def soql_query(access_token, instance_url, soql):
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{instance_url}/services/data/{API_VERSION}/query"
    records = []
    resp = requests.get(url, headers=headers, params={"q": soql}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Salesforce-fel vid query ({resp.status_code}): {resp.text}")
    data = resp.json()
    records.extend(data["records"])
    while not data.get("done", True):
        next_url = f"{instance_url}{data['nextRecordsUrl']}"
        resp = requests.get(next_url, headers=headers, timeout=30)
        data = resp.json()
        records.extend(data["records"])
    return records
 
 
from dash_config import load_config  # noqa: E402

TARGET_SALESPEOPLE = load_config()["target_salespeople"]
WEEKLY_VISIT_TARGET = load_config()["weekly_visit_target"]
MONTH_NAMES_SF = ["Januari", "Februari", "Mars", "April", "Maj", "Juni",
                  "Juli", "Augusti", "September", "Oktober", "November", "December"]
 
 
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
        datetime.date(year, 1, 1), datetime.date(year, 1, 6),
        easter - datetime.timedelta(days=2), easter, easter + datetime.timedelta(days=1),
        datetime.date(year, 5, 1),
        easter + datetime.timedelta(days=39), easter + datetime.timedelta(days=49),
        datetime.date(year, 6, 6),
        datetime.date(year, 12, 25), datetime.date(year, 12, 26),
    }
    for d in range(20, 27):
        dd = datetime.date(year, 6, d)
        if dd.weekday() == 5:
            holidays.add(dd)
            break
    for month, day_range in ((10, range(31, 32)), (11, range(1, 7))):
        for d in day_range:
            dd = datetime.date(year, month, d)
            if dd.weekday() == 5:
                holidays.add(dd)
                break
    return holidays
 
 
def is_business_day(d):
    return d.weekday() < 5 and d not in swedish_public_holidays(d.year)
 
 
def business_days_elapsed(year, month, up_to_date):
    d = datetime.date(year, month, 1)
    count = 0
    while d <= up_to_date:
        if is_business_day(d):
            count += 1
        d += datetime.timedelta(days=1)
    return count
 
 
def business_days_between(start_date, end_date):
    d = start_date
    count = 0
    while d <= end_date:
        if is_business_day(d):
            count += 1
        d += datetime.timedelta(days=1)
    return count
 
 
@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Hämtar färsk data från Salesforce...")
def fetch_salesforce_data():
    access_token, instance_url = get_session()
 
    opportunities = soql_query(
        access_token, instance_url,
        "SELECT Id, Name, Amount, StageName, Probability, CloseDate, IsClosed, IsWon, "
        "Owner.Name FROM Opportunity WHERE NOT (IsClosed = true AND IsWon = false)"
    )
    events = soql_query(
        access_token, instance_url,
        "SELECT Id, Date__c, Name, Type__c, Who_did_the_visit_Select_All__c "
        "FROM Visit_Report__c WHERE Date__c = THIS_YEAR AND Date__c != null "
        "AND Type__c != 'Teams Meeting'"
    )
    tasks = soql_query(
        access_token, instance_url,
        "SELECT Id, OwnerId, Owner.Name, ActivityDate, Subject "
        "FROM Task WHERE ActivityDate = LAST_N_DAYS:14 AND ActivityDate != null "
        "AND Subject LIKE 'Samtal%'"
    )
    return {
        "opportunities": opportunities,
        "events": events,
        "tasks": tasks,
        "fetched_at": time.time(),
    }
 
 
# ---------------------------------------------------------------------------
# Sida
# ---------------------------------------------------------------------------
 
col1, col2 = st.columns([5, 1])
with col1:
    st.title("Salesforce - Pipeline & aktiviteter")
with col2:
    if st.button("🔄 Uppdatera nu"):
        fetch_salesforce_data.clear()
        st.rerun()
 
try:
    raw = fetch_salesforce_data()
except Exception as e:
    st.error(f"Kunde inte hämta data från Salesforce: {e}")
    st.link_button("🔗 Anslut till Salesforce igen", build_authorize_url())
    st.stop()
 
fetched_ago_min = round((time.time() - raw["fetched_at"]) / 60)
 
opps = pd.DataFrame(raw["opportunities"])
events = pd.DataFrame(raw["events"])
 
if not opps.empty:
    opps["OwnerName"] = opps["Owner"].apply(lambda o: o["Name"] if isinstance(o, dict) else "Okänd")
    opps["Amount"] = opps["Amount"].fillna(0)
 
if not events.empty:
    events["Attendees"] = events.get("Who_did_the_visit_Select_All__c", "").fillna("").apply(
        lambda s: [x.strip() for x in s.split(";") if x.strip()]
    )
    events["Date__c_parsed"] = pd.to_datetime(events["Date__c"]).dt.date
    events["Month"] = events["Date__c_parsed"].apply(lambda d: d.month)
 
today = datetime.date.today()
year = today.year
ytd_start = datetime.date(year, 1, 1)
 
if not opps.empty:
    fas_num = opps["StageName"].astype(str).str.extract(r"FAS\s?([0-4])", expand=False)
    is_fas_pipeline = fas_num.isin(["0", "1", "2", "3"])
    pipeline_opps = opps[is_fas_pipeline]
    won_ytd_opps = opps[(opps["IsWon"] == True) & (pd.to_datetime(opps["CloseDate"]).dt.date >= ytd_start)]
else:
    pipeline_opps = opps
    won_ytd_opps = opps
 
pipeline_total = pipeline_opps["Amount"].sum() if not pipeline_opps.empty else 0
pipeline_count = len(pipeline_opps) if not pipeline_opps.empty else 0
won_ytd_total = won_ytd_opps["Amount"].sum() if not won_ytd_opps.empty else 0
 
# ---- Pipeline per stadium ----
if not pipeline_opps.empty:
    by_stage = pipeline_opps.groupby("StageName")["Amount"].sum().sort_index()
    stage_labels = [s[:12] for s in by_stage.index.tolist()]
    stage_values = [round(v) for v in by_stage.values.tolist()]
else:
    stage_labels, stage_values = [], []
 
# ---- Pipeline per säljare ----
if not pipeline_opps.empty:
    filtered = pipeline_opps[pipeline_opps["OwnerName"].isin(TARGET_SALESPEOPLE)]
    by_owner = filtered.groupby("OwnerName")["Amount"].sum().reindex(TARGET_SALESPEOPLE).fillna(0)
else:
    by_owner = pd.Series([0] * len(TARGET_SALESPEOPLE), index=TARGET_SALESPEOPLE)
 
# ---- Besök per säljare (augusti-december, denna månad och framåt) ----
TRACK_START_MONTH = 8
TRACK_END_MONTH = 12
 
this_week_start = today - datetime.timedelta(days=today.weekday())
 
person_data = {}
for person in TARGET_SALESPEOPLE:
    weekly_target = WEEKLY_VISIT_TARGET[person]
    daily_target = weekly_target / 5
    first_name = person.split(" ")[0]
 
    month_actual, month_target = [], []
    for m in range(TRACK_START_MONTH, TRACK_END_MONTH + 1):
        if m == today.month:
            # Räkna bara från måndag denna vecka och framåt, inte från 1:a i månaden
            effective_start = this_week_start if this_week_start.month == m else datetime.date(year, m, 1)
            target_m = daily_target * business_days_between(effective_start, today)
        elif m < today.month:
            last_day = (datetime.date(year, m + 1, 1) - datetime.timedelta(days=1)) if m < 12 else datetime.date(year, 12, 31)
            target_m = daily_target * business_days_elapsed(year, m, last_day)
        else:
            target_m = 0  # framtida månad - inget mål förrän den börjar
 
        actual_m = 0
        if not events.empty:
            if m == today.month:
                effective_start = this_week_start if this_week_start.month == m else datetime.date(year, m, 1)
                month_events = events[(events["Date__c_parsed"] >= effective_start) & (events["Date__c_parsed"] <= today)]
            else:
                month_events = events[events["Month"] == m]
            if not month_events.empty:
                counts = month_events.explode("Attendees")["Attendees"].value_counts()
                actual_m = int(counts.get(person, 0)) or int(counts.get(first_name, 0))
 
        month_actual.append(actual_m)
        month_target.append(round(target_m, 1))
 
    person_data[person] = {
        "actual": month_actual,
        "target": month_target,
        "ytd_actual": sum(month_actual),
        "ytd_target": round(sum(month_target), 1),
    }
 
# ---- Samtal denna vecka vs förra veckan ----
tasks = pd.DataFrame(raw.get("tasks", []))
if not tasks.empty:
    tasks["OwnerName"] = tasks["Owner"].apply(lambda o: o["Name"] if isinstance(o, dict) else "Okänd")
    tasks["ActivityDate_parsed"] = pd.to_datetime(tasks["ActivityDate"]).dt.date
 
last_week_start = this_week_start - datetime.timedelta(days=7)
last_week_end = this_week_start - datetime.timedelta(days=1)
 
call_counts = {}
for person in TARGET_SALESPEOPLE:
    if tasks.empty:
        call_counts[person] = {"this_week": 0, "last_week": 0}
        continue
    this_week_count = len(tasks[(tasks["OwnerName"] == person) & (tasks["ActivityDate_parsed"] >= this_week_start) & (tasks["ActivityDate_parsed"] <= today)])
    last_week_count = len(tasks[(tasks["OwnerName"] == person) & (tasks["ActivityDate_parsed"] >= last_week_start) & (tasks["ActivityDate_parsed"] <= last_week_end)])
    call_counts[person] = {"this_week": this_week_count, "last_week": last_week_count}
 
# ===========================================================================
# Kortbaserad HTML-dashboard (en sida, ingen scroll)
# ===========================================================================
 
def fmt_kr_short(n):
    a = abs(n)
    if a >= 1_000_000:
        return f"{n/1_000_000:.1f} Mkr".replace(".0 ", " ")
    if a >= 1_000:
        return f"{round(n/1000)} tkr"
    return f"{n:.0f} kr"
 
 
month_short_labels = json.dumps([MONTH_NAMES_SF[m - 1][:3] for m in range(TRACK_START_MONTH, TRACK_END_MONTH + 1)])
stage_labels_json = json.dumps(stage_labels)
stage_values_json = json.dumps(stage_values)
owner_labels_json = json.dumps(TARGET_SALESPEOPLE)
owner_values_json = json.dumps([round(v) for v in by_owner.values.tolist()])
 
 
call_cards_html = ""
for person in TARGET_SALESPEOPLE:
    cc = call_counts[person]
    call_diff = cc["this_week"] - cc["last_week"]
    call_color = "#8AC249" if call_diff >= 0 else "#D64550"
    call_cards_html += f"""
    <div class="card">
      <div class="label">{person} - Samtal</div>
      <div class="big" style="font-size:22px;">{cc['this_week']} <span style="font-size:12px;color:var(--ink-soft);font-weight:500;">denna vecka</span></div>
      <div class="sub" style="color:{call_color};">{call_diff:+d} vs förra veckan ({cc['last_week']})</div>
    </div>
    """
 
person_cards_html = ""
person_charts_js = ""
colors = ["#8AC249", "#2C4874", "#5E9130", "#D64550"]
for idx, person in enumerate(TARGET_SALESPEOPLE):
    pd_ = person_data[person]
    diff = pd_["ytd_actual"] - pd_["ytd_target"]
    pct = (pd_["ytd_actual"] / pd_["ytd_target"] * 100) if pd_["ytd_target"] else 100
    pct_clamped = max(0, min(pct, 100))
    color = "#8AC249" if diff >= 0 else "#D64550"
    person_cards_html += f"""
    <div class="card person-card">
      <div class="label">{person}</div>
      <div class="big" style="font-size:20px;">{pd_['ytd_actual']} <span style="font-size:12px;color:var(--ink-soft);font-weight:500;">/ {pd_['ytd_target']:.0f} mål</span></div>
      <div class="progress-track"><div class="progress-fill" style="width:{pct_clamped}%; background:{color};"></div></div>
      <div class="sub" style="color:{color};">{diff:+.1f} mot mål hittills</div>
      <canvas id="personChart{idx}" style="max-height:70px; margin-top:6px;"></canvas>
    </div>
    """
    actual_json = json.dumps(pd_["actual"])
    target_json = json.dumps(pd_["target"])
    person_charts_js += f"""
    new Chart(document.getElementById('personChart{idx}'), {{
      type:'bar',
      data:{{ labels:{month_short_labels}, datasets:[
        {{label:'Faktiskt', data:{actual_json}, backgroundColor:'{colors[idx]}', borderRadius:2}},
        {{label:'Mål', data:{target_json}, type:'line', borderColor:'#5A6B85', borderWidth:1.5, pointRadius:0, fill:false}}
      ]}},
      options:{{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}},
        scales:{{ x:{{display:false}}, y:{{display:false}} }} }}
    }});
    """
 
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
  .grid{{display:grid; grid-template-columns:repeat(12,1fr); gap:14px; padding:4px;}}
  .card{{background:var(--card); border:1px solid var(--rule); border-radius:12px; padding:16px 18px; display:flex; flex-direction:column;}}
  .label{{font-family:'IBM Plex Mono',monospace; font-size:10.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--ink-soft); margin-bottom:6px;}}
  .big{{font-family:'Manrope',sans-serif; font-weight:800; font-size:26px; line-height:1.1;}}
  .sub{{font-family:'IBM Plex Mono',monospace; font-size:11.5px; margin-top:4px;}}
  .kpi-row{{grid-column:1/13; display:grid; grid-template-columns:repeat(3,1fr); gap:14px;}}
  .mid-row{{grid-column:1/13; display:grid; grid-template-columns:1fr 1fr; gap:14px;}}
  .bottom-row{{grid-column:1/13; display:grid; grid-template-columns:repeat(3,1fr); gap:14px;}}
  .chart-card{{height:250px;}}
  .chart-card canvas{{max-height:200px;}}
  .progress-track{{background:var(--rule); border-radius:6px; height:9px; overflow:hidden; margin-top:8px;}}
  .progress-fill{{height:100%; border-radius:6px;}}
  .person-card{{height:190px;}}
  h3.card-title{{font-family:'Manrope',sans-serif; font-weight:700; font-size:14px; margin-bottom:8px;}}
</style>
</head>
<body>
<div class="grid">
 
  <div class="kpi-row">
    <div class="card">
      <div class="label">Total pipeline (FAS 0-3)</div>
      <div class="big">{fmt_kr_short(pipeline_total)}</div>
    </div>
    <div class="card">
      <div class="label">Antal öppna säljprojekt</div>
      <div class="big">{pipeline_count}</div>
    </div>
    <div class="card">
      <div class="label">Closed Won {year} (YTD)</div>
      <div class="big">{fmt_kr_short(won_ytd_total)}</div>
    </div>
  </div>
 
  <div class="mid-row">
    <div class="card chart-card">
      <h3 class="card-title">Pipeline per stadium</h3>
      <canvas id="stageChart"></canvas>
    </div>
    <div class="card chart-card">
      <h3 class="card-title">Pipeline per säljare</h3>
      <canvas id="ownerChart"></canvas>
    </div>
  </div>
 
  <div class="bottom-row">
    {person_cards_html}
  </div>
 
  <div class="bottom-row">
    {call_cards_html}
  </div>
 
</div>
 
<script>
new Chart(document.getElementById('stageChart'), {{
  type:'doughnut',
  data:{{ labels:{stage_labels_json}, datasets:[{{
    data:{stage_values_json},
    backgroundColor:['#2C4874','#8AC249','#5E9130','#D6D0BE']
  }}] }},
  options:{{ responsive:true, maintainAspectRatio:false,
    plugins:{{ legend:{{position:'right', labels:{{boxWidth:10, font:{{size:10}}}}}} }} }}
}});
 
new Chart(document.getElementById('ownerChart'), {{
  type:'bar',
  data:{{ labels:{owner_labels_json}, datasets:[{{data:{owner_values_json}, backgroundColor:'#8AC249', borderRadius:4}}] }},
  options:{{ responsive:true, maintainAspectRatio:false, indexAxis:'y',
    plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:{{color:'#E1E6ED'}}, ticks:{{font:{{family:"IBM Plex Mono",size:9}}}}}}, y:{{grid:{{display:false}}, ticks:{{font:{{size:10}}}}}} }} }}
}});
 
{person_charts_js}
</script>
</body>
</html>
"""
 
st.components.v1.html(html, height=810, scrolling=False)
 
st.caption(
    f"Data hämtad för {fetched_ago_min} min sedan · uppdateras automatiskt var 15:e minut. "
    "Pipeline: FAS 0-FAS 3 (FAS 4 och avslutade/förlorade räknas inte in). "
    "Besöksmål: veckomål/5 x arbetsdagar (röda dagar borträknade). Innevarande månad räknas från måndag denna vecka, inte från månadens start."
)
 
