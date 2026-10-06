#!/usr/bin/env python3
"""
Fortnox GAP-analys – kärnan
===========================
Synkar kunder, artiklar (med EAN) och kundfakturor från Fortnox till en databas
(Postgres i molnet, eller SQLite lokalt) och bygger en färdig rapport som sparas
i databasen. Streamlit-appen läser bara den färdiga rapporten, så sidan laddar direkt.

Kommandon:
    python fortnox_core.py setup     # engångs, på din egen dator: koppla mot Fortnox
    python fortnox_core.py sync      # synka nu (det här kör GitHub Actions varje timme)
    python fortnox_core.py fields    # visa artikelfälten, för att välja kategorifält
    python fortnox_core.py demo      # fyll databasen med påhittad data för test

Hemligheter läses från miljövariabler (Streamlit Secrets / GitHub Secrets):
    FORTNOX_CLIENT_ID, FORTNOX_CLIENT_SECRET, FORTNOX_TENANT_ID, DATABASE_URL
Övriga inställningar ligger i installningar.json.
"""
import base64
import datetime as dt
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlencode, urlparse, parse_qs, quote

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "installningar.json")
TEMPLATE_PATH = os.path.join(HERE, "rapport_mall.html")
LOCAL_DB = os.path.join(HERE, "lokal.sqlite")

_OAUTH = os.environ.get("FORTNOX_OAUTH_BASE", "https://apps.fortnox.se/oauth-v1")  # kan pekas om vid test
AUTH_URL = _OAUTH + "/auth"
TOKEN_URL = _OAUTH + "/token"
API = os.environ.get("FORTNOX_API_BASE", "https://api.fortnox.se/3")
SCOPES = "invoice article customer companyinformation"
STOCKHOLM = dt.timezone(dt.timedelta(hours=1))  # ersätts av zoneinfo nedan om möjligt
try:
    from zoneinfo import ZoneInfo
    STOCKHOLM = ZoneInfo("Europe/Stockholm")
except Exception:
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def now_local():
    return dt.datetime.now(STOCKHOLM)


# --------------------------------------------------------------------------- #
# Inställningar
# --------------------------------------------------------------------------- #
def load_settings(db=None):
    """Standardvärden från installningar.json, sedan det som valts i appen (sparat i databasen)."""
    s = {}
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s = json.load(f)
    stored = {}
    if db is not None:
        try:
            stored = json.loads(db.meta_get("settings") or "{}")
        except Exception:
            stored = {}
    for key, val in stored.items():
        if key == "kategori" and isinstance(val, dict):
            s.setdefault("kategori", {}).update(val)
        else:
            s[key] = val
    s.setdefault("ar_bakat", 3)
    s.setdefault("foretagsnamn", "")
    s.setdefault("exkludera_artiklar", [])
    k = s.setdefault("kategori", {})
    k.setdefault("falt", "Manufacturer")
    k.setdefault("regex", None)
    k.setdefault("mappning", {})
    k.setdefault("saknas", "Okategoriserad")
    s["client_id"] = os.environ.get("FORTNOX_CLIENT_ID", "")
    s["client_secret"] = os.environ.get("FORTNOX_CLIENT_SECRET", "")
    s["tenant_id"] = os.environ.get("FORTNOX_TENANT_ID", "") or \
        ((db.meta_get("tenant_id") or "") if db is not None else "")
    return s


def save_settings(db, changes):
    cur = json.loads(db.meta_get("settings") or "{}")
    cur.update(changes)
    db.meta_set("settings", json.dumps(cur, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Databas – samma kod för Postgres (moln) och SQLite (lokalt test)
# --------------------------------------------------------------------------- #
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS customers(nr TEXT PRIMARY KEY, json TEXT)",
    "CREATE TABLE IF NOT EXISTS articles(nr TEXT PRIMARY KEY, json TEXT, detailed INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS invoices(docnr TEXT PRIMARY KEY, date TEXT, customer TEXT, cancelled INTEGER, json TEXT)",
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS report(id INTEGER PRIMARY KEY, generated TEXT, json TEXT)",
]


class DB:
    def __init__(self, url=None, autocommit=False):
        url = url if url is not None else os.environ.get("DATABASE_URL", "")
        self.pg = url.startswith(("postgres://", "postgresql://"))
        if self.pg:
            import psycopg
            self.con = psycopg.connect(url, connect_timeout=20, autocommit=autocommit)
        else:
            self.con = sqlite3.connect(url or LOCAL_DB, check_same_thread=False,
                                       isolation_level=None if autocommit else "")
        for sql in SCHEMA:
            self.execute(sql)
        self.commit()

    def _q(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql, params=()):
        cur = self.con.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def executemany(self, sql, rows):
        if rows:
            self.con.cursor().executemany(self._q(sql), rows)

    def all(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def commit(self):
        self.con.commit()

    def close(self):
        self.con.close()

    # meta
    def meta_get(self, k):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r[0] if r else None

    def meta_set(self, k, v):
        self.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.commit()


UPSERT_CUSTOMER = "INSERT INTO customers(nr,json) VALUES(?,?) ON CONFLICT(nr) DO UPDATE SET json=excluded.json"
UPSERT_ARTICLE = ("INSERT INTO articles(nr,json,detailed) VALUES(?,?,?) "
                  "ON CONFLICT(nr) DO UPDATE SET json=excluded.json, detailed=excluded.detailed")
UPSERT_INVOICE = ("INSERT INTO invoices(docnr,date,customer,cancelled,json) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(docnr) DO UPDATE SET date=excluded.date, customer=excluded.customer, "
                  "cancelled=excluded.cancelled, json=excluded.json")


# --------------------------------------------------------------------------- #
# Fortnox-klient
# --------------------------------------------------------------------------- #
class Fortnox:
    MIN_INTERVAL = 5.0 / 23  # Fortnox tillåter 25 anrop / 5 s – lite marginal

    def __init__(self, s):
        import requests
        if not (s["client_id"] and s["client_secret"]):
            raise RuntimeError("FORTNOX_CLIENT_ID / FORTNOX_CLIENT_SECRET saknas.")
        self.requests = requests
        self.s = s
        self.session = requests.Session()
        self._last = 0.0
        self._token = None
        self._exp = 0

    def _basic(self):
        raw = f"{self.s['client_id']}:{self.s['client_secret']}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def token_request(self, data, headers=None):
        h = {"Authorization": self._basic(), "Content-Type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        r = self.requests.post(TOKEN_URL, data=data, headers=h, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Token-anrop misslyckades ({r.status_code}): {r.text[:300]}")
        return r.json()

    def token(self):
        if self._token and time.time() < self._exp:
            return self._token
        if not self.s["tenant_id"]:
            raise RuntimeError("FORTNOX_TENANT_ID saknas – kör 'python fortnox_core.py setup' först.")
        tok = self.token_request({"grant_type": "client_credentials"}, {"TenantId": str(self.s["tenant_id"])})
        self._token = tok["access_token"]
        self._exp = time.time() + int(tok.get("expires_in", 3600)) - 120
        return self._token

    def get(self, path, params=None, token=None):
        for attempt in range(8):
            wait = self.MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            r = self.session.get(API + path, params=params, timeout=60, headers={
                "Authorization": f"Bearer {token or self.token()}", "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code == 401 and attempt == 0 and not token:
                self._exp = 0
                continue
            if r.status_code >= 500:
                time.sleep(3 + attempt * 3)
                continue
            raise RuntimeError(f"GET {path} gav {r.status_code}: {r.text[:300]}")
        raise RuntimeError(f"GET {path} misslyckades efter flera försök")

    def paged(self, path, key, params=None):
        params = dict(params or {})
        params["limit"] = 500
        page = 1
        while True:
            params["page"] = page
            data = self.get(path, params)
            yield from data.get(key, [])
            if page >= int(data.get("MetaInformation", {}).get("@TotalPages", 1) or 1):
                break
            page += 1


# --------------------------------------------------------------------------- #
# Koppling (OAuth) – används av guiden i appen
# --------------------------------------------------------------------------- #
def auth_url(s, redirect, state):
    return AUTH_URL + "?" + urlencode({
        "client_id": s["client_id"], "redirect_uri": redirect, "scope": SCOPES, "state": state,
        "access_type": "offline", "response_type": "code", "account_type": "service"})


def exchange_code(s, code, redirect):
    """Byter godkännandekoden mot token och tar reda på tenant-id + företagsnamn."""
    fx = Fortnox(s)
    tok = fx.token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect})
    tenant, name = None, ""
    for path, key in (("/companyinformation", "CompanyInformation"), ("/settings/company", "CompanySettings")):
        try:
            info = fx.get(path, token=tok["access_token"]).get(key, {})
            tenant = tenant or info.get("DatabaseNumber")
            name = name or info.get("CompanyName", "")
        except Exception:
            pass
    if not tenant:
        try:
            pl = tok["access_token"].split(".")[1]
            pl += "=" * (-len(pl) % 4)
            c = json.loads(base64.urlsafe_b64decode(pl))
            tenant = c.get("tenantId") or c.get("tenant_id")
        except Exception:
            pass
    if not tenant:
        raise RuntimeError("Kunde inte läsa ut tenant-id från Fortnox.")
    return str(tenant), name


def article_samples(s, n=5):
    """Några artiklar med alla fält – för att välja kategorifält."""
    fx = Fortnox(s)
    out = []
    for a in fx.get("/articles", {"limit": n}).get("Articles", [])[:n]:
        out.append(fx.get(f"/articles/{quote(str(a['ArticleNumber']), safe='')}").get("Article", {}))
    return out


# --------------------------------------------------------------------------- #
# setup – reserv: koppla från en dator med Python (behövs normalt inte)
# --------------------------------------------------------------------------- #
def cmd_setup():
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    s = load_settings()
    s["client_id"] = s["client_id"] or input("Client ID: ").strip()
    s["client_secret"] = s["client_secret"] or input("Client Secret: ").strip()
    redirect = os.environ.get("FORTNOX_REDIRECT_URI", "http://localhost:8765/callback")
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urlencode({
        "client_id": s["client_id"], "redirect_uri": redirect, "scope": SCOPES, "state": state,
        "access_type": "offline", "response_type": "code", "account_type": "service"})
    print("\nGodkänn kopplingen som systemadministratör i Fortnox (webbläsaren öppnas):\n" + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    result = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            result.update({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Klart! Gå tillbaka till kommandofönstret.</h2>".encode())

        def log_message(self, *a):
            pass

    p = urlparse(redirect)
    srv = HTTPServer((p.hostname, p.port or 80), H)
    while "code" not in result and "error" not in result:
        srv.handle_request()
    if "error" in result or result.get("state") != state:
        sys.exit(f"Kopplingen avbröts: {result}")

    fx = Fortnox(s)
    tok = fx.token_request({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect})
    tenant, name = None, ""
    for path, key in (("/companyinformation", "CompanyInformation"), ("/settings/company", "CompanySettings")):
        try:
            info = fx.get(path, token=tok["access_token"]).get(key, {})
            tenant = tenant or info.get("DatabaseNumber")
            name = name or info.get("CompanyName", "")
        except Exception:
            pass
    if not tenant:
        try:
            pl = tok["access_token"].split(".")[1]
            pl += "=" * (-len(pl) % 4)
            c = json.loads(base64.urlsafe_b64decode(pl))
            tenant = c.get("tenantId") or c.get("tenant_id")
        except Exception:
            pass
    if not tenant:
        sys.exit("Kunde inte läsa ut tenant-id. Kontakta Fortnox support och fråga efter ert TenantId.")
    print(f"\nKlart! {name}\n\nLägg in detta i Streamlit Secrets och GitHub Secrets:\n\n"
          f'FORTNOX_TENANT_ID = "{tenant}"\n')
    # Visa artikelfälten direkt, så att ni ser vilket fält som innehåller kategorin
    try:
        first = fx.get("/articles", {"limit": 3}, token=tok["access_token"]).get("Articles", [])
        print("Så här ser några av era artiklar ut – leta upp fältet där kategorin står:")
        for a in first[:3]:
            d = fx.get(f"/articles/{quote(str(a['ArticleNumber']), safe='')}",
                       token=tok["access_token"]).get("Article", {})
            print(f"\nArtikel {d.get('ArticleNumber')}:")
            for key, v in d.items():
                if v not in (None, "", 0, False) and not isinstance(v, (dict, list)):
                    print(f"  {key:28} {v}")
    except Exception as e:
        print(f"(Kunde inte visa artikelfält: {e})")


# --------------------------------------------------------------------------- #
# Synk
# --------------------------------------------------------------------------- #
LOCK_MINUTES = 30


def sync(db=None, force=False, log=print):
    """Inkrementell synk. Returnerar True om en synk kördes."""
    own = db is None
    db = db or DB()
    s = load_settings(db)
    _log = log

    def log(msg):
        _log(msg)
        try:
            db.meta_set("sync_progress", msg)
        except Exception:
            pass
    running = db.meta_get("sync_running")
    if running and not force:
        age = (utcnow() - dt.datetime.fromisoformat(running)).total_seconds() / 60
        if age < LOCK_MINUTES:
            log("En annan synk pågår redan – hoppar över.")
            if own:
                db.close()
            return False
    db.meta_set("sync_running", utcnow().isoformat())
    try:
        fx = Fortnox(s)
        last = db.meta_get("last_sync_fortnox")  # Fortnox lastmodified-format, svensk tid
        lm = {"lastmodified": last} if last else {}
        started_fx = now_local().strftime("%Y-%m-%d %H:%M")

        if not db.meta_get("company") and not s["foretagsnamn"]:
            try:
                db.meta_set("company", fx.get("/companyinformation")["CompanyInformation"].get("CompanyName", ""))
            except Exception:
                pass

        rows = [(c["CustomerNumber"], json.dumps(c)) for c in fx.paged("/customers", "Customers", lm)]
        db.executemany(UPSERT_CUSTOMER, rows)
        db.commit()
        log(f"Kunder: {len(rows)} nya/ändrade")

        falt = s["kategori"]["falt"]
        arts = list(fx.paged("/articles", "Articles", lm))
        in_list = falt in ("ArticleNumber", "Description", "EAN") or any(falt in a for a in arts[:5])
        db.executemany(UPSERT_ARTICLE, [(a["ArticleNumber"], json.dumps(a), 1 if in_list else 0) for a in arts])
        db.commit()
        if not in_list:
            todo = [r[0] for r in db.all("SELECT nr FROM articles WHERE detailed=0")]
            for i, nr in enumerate(todo, 1):
                try:
                    d = fx.get(f"/articles/{quote(str(nr), safe='')}").get("Article", {})
                    db.execute("UPDATE articles SET json=?, detailed=1 WHERE nr=?", (json.dumps(d), nr))
                except RuntimeError as e:
                    log(f"  ! Artikel {nr}: {e}")
                if i % 100 == 0:
                    db.commit()
                    log(f"  artikeldetaljer {i}/{len(todo)}")
            db.commit()
        log(f"Artiklar: {len(arts)} nya/ändrade")

        from_date = f"{now_local().year - int(s['ar_bakat'])}-01-01"
        have = {r[0] for r in db.all("SELECT docnr FROM invoices")}
        in_period = [i["DocumentNumber"] for i in fx.paged("/invoices", "Invoices", {"fromdate": from_date})]
        modified = set()
        if last:
            modified = {i["DocumentNumber"] for i in
                        fx.paged("/invoices", "Invoices", {"lastmodified": last, "fromdate": from_date})}
        todo = [d for d in in_period if d not in have or d in modified]
        log(f"Fakturor i perioden: {len(in_period)}, att hämta: {len(todo)}")
        db.meta_set("sync_total", str(len(todo)))
        for i, docnr in enumerate(todo, 1):
            inv = fx.get(f"/invoices/{docnr}").get("Invoice", {})
            db.execute(UPSERT_INVOICE, (docnr, inv.get("InvoiceDate"), inv.get("CustomerNumber"),
                                        1 if inv.get("Cancelled") else 0, json.dumps(inv)))
            if i % 100 == 0:
                db.commit()
                log(f"Hämtar fakturor: {i} av {len(todo)}")
                if i % 500 == 0:          # visa delresultat under en lång första synk
                    save_report(db, s)
        db.commit()

        save_report(db, s)
        db.meta_set("last_sync_fortnox", started_fx)
        db.meta_set("last_sync_utc", utcnow().isoformat())
        db.meta_set("sync_error", "")
        log("Synk klar.")
        return True
    except Exception as e:
        try:
            db.con.rollback()
            db.meta_set("sync_error", f"{now_local():%Y-%m-%d %H:%M} – {e}")
        except Exception:
            pass
        raise
    finally:
        try:
            db.con.rollback()
            db.meta_set("sync_running", "")
        finally:
            if own:
                db.close()


def is_configured(db):
    s = load_settings(db)
    chosen = "kategori" in json.loads(db.meta_get("settings") or "{}")
    return bool(s["client_id"] and s["client_secret"] and s["tenant_id"] and chosen)


def sync_if_stale(max_age_min=15, log=print):
    db = DB()
    try:
        if not is_configured(db):
            return False
        done = db.meta_get("last_sync_utc")
        if done:
            age = (utcnow() - dt.datetime.fromisoformat(done)).total_seconds() / 60
            if age < max_age_min:
                return False
        return sync(db, log=log)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Rapport
# --------------------------------------------------------------------------- #
def num(x):
    try:
        return float(str(x).replace(",", ".")) if x not in (None, "") else 0.0
    except ValueError:
        return 0.0


def category_for(article, k):
    val = article.get(k["falt"]) if article else None
    val = "" if val is None else str(val).strip()
    if val and k.get("regex"):
        m = re.search(k["regex"], val)
        val = (m.group(1) if m and m.groups() else (m.group(0) if m else "")).strip()
    val = k.get("mappning", {}).get(val, val)
    return val or k["saknas"]


def build_dataset(db, s, today=None):
    today = today or now_local().date()
    years = list(range(today.year - int(s["ar_bakat"]), today.year + 1))
    md_today = (today.month, today.day)
    k = s["kategori"]
    excl = set(map(str, s.get("exkludera_artiklar", [])))

    customers = {}
    for nr, js in db.all("SELECT nr,json FROM customers"):
        c = json.loads(js)
        customers[nr] = {"nr": nr, "name": c.get("Name") or nr, "city": c.get("City") or "",
                         "active": c.get("Active", True) is not False}
    articles = {nr: json.loads(js) for nr, js in db.all("SELECT nr,json FROM articles")}

    cell, acell, skipped = {}, {}, 0
    for date, cust, js in db.all("SELECT date,customer,json FROM invoices WHERE cancelled=0"):
        if not date:
            continue
        d = dt.date.fromisoformat(date[:10])
        if d.year not in years:
            continue
        inv = json.loads(js)
        fx = (num(inv.get("CurrencyRate")) or 1.0) / (num(inv.get("CurrencyUnit")) or 1.0)
        vat_incl = bool(inv.get("VATIncluded"))
        is_ytd = (d.month, d.day) <= md_today
        if cust not in customers:
            customers[cust] = {"nr": cust, "name": inv.get("CustomerName") or cust, "city": "", "active": True}
        for row in inv.get("InvoiceRows", []):
            art = str(row.get("ArticleNumber") or "").strip()
            if not art or art in excl:
                if not art and num(row.get("Total")):
                    skipped += 1
                continue
            amt = num(row.get("Total"))
            if vat_incl:
                amt /= 1 + num(row.get("VAT")) / 100
            amt *= fx
            if amt == 0:
                continue
            cat = category_for(articles.get(art), k)
            for key, store in (((cust, cat, d.year), cell), ((cust, art, d.year), acell)):
                v = store.setdefault(key, [0.0, 0.0])
                v[0] += amt
                if is_ytd:
                    v[1] += amt

    cats = sorted({x[1] for x in cell})
    cust_list = sorted(customers.values(), key=lambda c: c["name"].lower())
    ci = {c["nr"]: i for i, c in enumerate(cust_list)}
    ki = {c: i for i, c in enumerate(cats)}
    yi = {y: i for i, y in enumerate(years)}
    used = sorted({x[1] for x in acell})
    ai = {a: i for i, a in enumerate(used)}
    return {
        "generated": now_local().strftime("%Y-%m-%d %H:%M"),
        "company": s.get("foretagsnamn") or (db.meta_get("company") or ""),
        "today": today.isoformat(),
        "years": years,
        "categoryField": k["falt"],
        "categories": cats,
        "customers": [[c["nr"], c["name"], c["city"], 1 if c["active"] else 0] for c in cust_list],
        "articles": [[a, (articles.get(a) or {}).get("Description", ""), (articles.get(a) or {}).get("EAN", ""),
                      ki[category_for(articles.get(a), k)]] for a in used],
        "cells": [[ci[c], ki[x], yi[y], round(v[0]), round(v[1])] for (c, x, y), v in cell.items()],
        "artCells": [[ci[c], ai[a], yi[y], round(v[0]), round(v[1])] for (c, a, y), v in acell.items()],
        "skippedRows": skipped,
    }


def save_report(db, s=None):
    ds = build_dataset(db, s or load_settings(db))
    db.execute("INSERT INTO report(id,generated,json) VALUES(1,?,?) "
               "ON CONFLICT(id) DO UPDATE SET generated=excluded.generated, json=excluded.json",
               (ds["generated"], json.dumps(ds, ensure_ascii=False, separators=(",", ":"))))
    db.commit()
    return ds


def load_report(db=None):
    own = db is None
    db = db or DB()
    try:
        r = db.one("SELECT json FROM report WHERE id=1")
        return json.loads(r[0]) if r else None
    finally:
        if own:
            db.close()


def render_html(ds):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(ds, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return html.replace("/*__DATA__*/null", payload)


# --------------------------------------------------------------------------- #
# fields / demo
# --------------------------------------------------------------------------- #
def cmd_fields():
    fx = Fortnox(load_settings())
    for a in list(fx.paged("/articles", "Articles", {}))[:3]:
        d = fx.get(f"/articles/{quote(str(a['ArticleNumber']), safe='')}").get("Article", {})
        print(f"\nArtikel {d.get('ArticleNumber')}:")
        for key, v in d.items():
            if v not in (None, "", 0, False) and not isinstance(v, (dict, list)):
                print(f"  {key:28} {v}")


def cmd_demo(db=None):
    db = db or DB()
    for t in ("customers", "articles", "invoices", "meta", "report"):
        db.execute(f"DELETE FROM {t}")
    rnd = random.Random(7)
    cats = ["Handverktyg", "Elverktyg", "Skruv & infästning", "Skyddsutrustning",
            "Färg & spackel", "Lim & tätning", "Förbrukning", "Belysning"]
    arts = []
    for i, cat in enumerate(cats):
        for j in range(6):
            ean = f"735000{i:02d}{j:04d}"
            ean += str((10 - sum(int(c) * (3 if n % 2 else 1) for n, c in enumerate(ean)) % 10) % 10)
            nr = f"{i + 1}{j + 1:03d}"
            arts.append((nr, json.dumps({"ArticleNumber": nr, "Description": f"{cat} artikel {j + 1}",
                                         "EAN": ean, "Manufacturer": cat}), 1))
    db.executemany(UPSERT_ARTICLE, arts)
    names = ["Bygg & Montage Gislaved AB", "Smålands Fastighetsservice", "Värnamo Snickeri", "Nordic Plåt AB",
             "Anderstorp El & Tele", "Gnosjö Industriservice", "Reftele Måleri", "Hestra Trädgård & Mark",
             "Jönköpings VVS-Teknik", "Smålandsstenar Bygg", "Skillingaryd Maskin AB", "Burseryd Renovering",
             "Vaggeryd Entreprenad", "Bredaryd Fönster & Dörr", "Unnaryd Lantbruksservice", "Tranås Golv AB"]
    today = now_local().date()
    doc, invs, custs = 1000, [], []
    for n, name in enumerate(names):
        nr = str(1001 + n)
        custs.append((nr, json.dumps({"CustomerNumber": nr, "Name": name, "City": name.split()[0], "Active": True})))
        size = rnd.choice([0.4, 1, 1, 2, 4])
        prof = {c: rnd.random() for c in cats}
        lost = rnd.sample(cats, rnd.randint(0, 2))
        for y in range(today.year - 3, today.year + 1):
            end = today if y == today.year else dt.date(y, 12, 31)
            for _ in range(int(rnd.randint(8, 20) * size)):
                d = dt.date(y, 1, 1) + dt.timedelta(days=rnd.randint(0, (end - dt.date(y, 1, 1)).days))
                rows = [{"ArticleNumber": f"{cats.index(c) + 1}{rnd.randint(1, 6):03d}",
                         "Total": round(rnd.uniform(200, 4000) * size, 2), "VAT": 25}
                        for c in cats if rnd.random() < (prof[c] - (0.9 if c in lost and y >= today.year - 1 else 0)) * 0.6]
                if rows:
                    doc += 1
                    invs.append((str(doc), d.isoformat(), nr, 0, json.dumps(
                        {"DocumentNumber": str(doc), "InvoiceDate": d.isoformat(), "CustomerNumber": nr,
                         "CustomerName": name, "Cancelled": False, "VATIncluded": False,
                         "CurrencyRate": 1, "CurrencyUnit": 1, "InvoiceRows": rows})))
    custs += [("2001", json.dumps({"CustomerNumber": "2001", "Name": "Rydaholm Bygg (ny kund)", "Active": True}))]
    db.executemany(UPSERT_CUSTOMER, custs)
    db.executemany(UPSERT_INVOICE, invs)
    db.commit()
    db.meta_set("company", "Demoföretaget AB (påhittad data)")
    s = load_settings()
    s["kategori"]["falt"] = "Manufacturer"
    save_report(db, s)
    db.meta_set("last_sync_utc", utcnow().isoformat())
    print(f"Demodata inlagd: {len(custs)} kunder, {len(invs)} fakturor.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        cmd_setup()
    elif cmd == "sync":
        sync(force="--force" in sys.argv)
    elif cmd == "fields":
        cmd_fields()
    elif cmd == "demo":
        cmd_demo()
    else:
        print(__doc__)
