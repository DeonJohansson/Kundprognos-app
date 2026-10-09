"""Sida: GAP-analys – vilka produktkategorier varje kund köper, har slutat köpa eller aldrig köpt.

Datan synkas från Fortnox av GitHub Actions i det privata repot Kundprognos-2026
(.github/workflows/gap-sync.yml) och läses här från gap_report.json. Visas som tydliga
tabeller i Excel-stil.
"""
import io
import os
import base64
import json

import numpy as np
import pandas as pd
import requests
import streamlit as st

DATA_REPO = "DeonJohansson/Kundprognos-2026"
MISSING_CAT = "Okategoriserad"

FILL = {"lost": "background-color:#f8d7d3;color:#8a1c12", "never": "background-color:#eef1f6;color:#8a93a3",
        "down": "background-color:#fbe9c6", "new": "background-color:#d6efe0", "grow": "background-color:#e3f3e8",
        "ok": ""}
STATUS_TXT = {"lost": "Tappad", "down": "Minskar", "never": "Aldrig köpt", "new": "Ny i år", "grow": "Växer", "ok": "Stabil"}


# ---------------------------------------------------------------- data från det privata repot
def _gh(path, raw=True):
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:                     # lokalt testläge: läs filen bredvid appen
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
        st.error("GITHUB_TOKEN saknas i appens Secrets.")
        st.stop()
    r = requests.get(f"https://api.github.com/repos/{DATA_REPO}/contents/{path}", timeout=60, headers={
        "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"})
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        st.error(f"Kunde inte hämta {path} från GitHub ({r.status_code}).")
        st.stop()
    return r.text


@st.cache_data(ttl=900, show_spinner="Hämtar GAP-data …")
def load_ds():
    raw = _gh("gap_report.json")
    return json.loads(raw) if raw else None


@st.cache_data(ttl=300, show_spinner=False)
def load_status():
    raw = _gh("gap_status.json")
    return json.loads(raw) if raw else {}


ds = load_ds()
status = load_status()
if not ds:
    st.title("GAP-analys")
    if status and not status.get("ok"):
        st.error(f"Synken från Fortnox misslyckades: {status.get('error', 'okänt fel')}")
    else:
        st.info("GAP-rapporten byggs just nu från Fortnox (första gången hämtas fyra års fakturor och det kan "
                "ta upp till ett par timmar). Ladda om sidan senare.")
    st.stop()


# ---------------------------------------------------------------- data som matriser
@st.cache_data(show_spinner=False)
def arrays(generated: str, _ds):
    years, cats, custs = _ds["years"], _ds["categories"], _ds["customers"]
    NC, NK, NY = len(custs), len(cats), len(years)
    full = np.zeros((NC, NK, NY))
    ytd = np.zeros((NC, NK, NY))
    for c, k, y, f, t in _ds["cells"]:
        full[c, k, y] = f
        ytd[c, k, y] = t
    cust = pd.DataFrame(custs, columns=["Kundnr", "Kund", "Ort", "Aktiv"])
    return full, ytd, cust


FULL, YTD, CUST = arrays(ds["generated"], ds)
YEARS, CATS = ds["years"], ds["categories"]


def status_matrix(V, yi, p1):
    v = V[:, :, yi]
    ever = (V[:, :, :yi] > 0).any(axis=2) if yi > 0 else np.zeros(v.shape, bool)
    st_ = np.full(v.shape, "ok", dtype=object)
    st_[(v <= 0) & ever] = "lost"
    st_[(v <= 0) & ~ever] = "never"
    st_[(v > 0) & ~ever] = "new"
    st_[(v > 0) & ever & (p1 > 0) & (v < p1 * 0.7)] = "down"
    st_[(v > 0) & ever & (p1 > 0) & (v > p1 * 1.15)] = "grow"
    return st_


def short(v):
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f} mkr".replace(".", ",")
    if a >= 1e3:
        return f"{v / 1e3:.0f} tkr"
    return f"{v:.0f} kr"


def kr(v):
    """Belopp i hela kronor: 1 234 567 kr. Noll/saknas visas som –."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or round(v) == 0:
        return "–"
    return f"{v:,.0f} kr".replace(",", "\u00a0")


def pct_txt(v):
    return "" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:+.0f} %"


def pct(a, b):
    return round((a - b) / b * 100) if b > 0 else None


def excel_bytes(df, sheet):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name=sheet[:31])
    return buf.getvalue()


# ---------------------------------------------------------------- kolumnval (standard sparas i datarepot)
SET_FILE = "gap_settings.json"


def _put(path, text, sha, message):
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:                     # lokalt testläge
        open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path), "w",
             encoding="utf-8").write(text)
        return True, None
    body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{DATA_REPO}/contents/{path}", json=body, timeout=30, headers={
        "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code in (200, 201):
        return True, None
    if r.status_code in (409, 422):
        return False, "Någon annan sparade samtidigt. Ladda om sidan och försök igen."
    if r.status_code in (401, 403):
        return False, "GitHub-nyckeln saknar skrivbehörighet (Contents: Read and write)."
    return False, f"GitHub svarade {r.status_code}."


def gap_settings():
    if "gap_set" not in st.session_state:
        if not st.secrets.get("GITHUB_TOKEN"):     # lokalt testläge
            lp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), SET_FILE)
            st.session_state["gap_set"] = (json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {}, None)
        else:
            meta = _gh(SET_FILE, raw=False)
            j = json.loads(meta) if meta else None
            st.session_state["gap_set"] = ((json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"])
                                           if j else ({}, None))
    return st.session_state["gap_set"]


def col_picker(tab, title, cols, fixed, yr):
    """Popover för att välja kolumner i en tabell. Returnerar kolumnerna som ska visas.
    Standard sparas som *dolda* kolumner, så nya kategorier syns automatiskt. Årtal sparas
    som {år}/{fjol} så att valet gäller oavsett vilket år man tittar på."""
    def sid(c):
        return str(c).replace(str(yr), "{år}").replace(str(yr - 1), "{fjol}")

    def label(i):
        return i.replace("{år}", str(yr)).replace("{fjol}", str(yr - 1))

    sett, sha = gap_settings()
    hidden_default = set(sett.get("hidden_columns", {}).get(tab, []))
    opts = [sid(c) for c in cols if c not in fixed]
    key = f"gap_cols_{tab}"
    if key not in st.session_state or set(st.session_state.get(key + "_opts", [])) != set(opts):
        prev_sel = st.session_state.get(key)
        known = set(st.session_state.get(key + "_opts", []))
        st.session_state[key] = ([o for o in opts if o in prev_sel or o not in known] if prev_sel is not None
                                 else [o for o in opts if o not in hidden_default])
        st.session_state[key + "_opts"] = opts
    with st.popover(f"Kolumner ({len(st.session_state[key]) + len(fixed)} av {len(cols)})"):
        st.multiselect(f"Visa kolumner i {title}", opts, key=key, format_func=label,
                       help=f"{', '.join(fixed)} visas alltid. Valet gäller direkt för dig.")
        b1, b2 = st.columns(2)
        if b1.button("Visa alla", key=key + "_all", width="stretch"):
            st.session_state[key] = list(opts)
            st.rerun()
        if b2.button("Återställ standard", key=key + "_reset", width="stretch"):
            st.session_state[key] = [o for o in opts if o not in hidden_default]
            st.rerun()
        st.divider()
        st.caption(f"Spara de valda kolumnerna som standard i {title}. Alla som öppnar rapporten ser då dessa kolumner.")
        if st.button("Spara som standard för alla", key=key + "_save", type="primary", width="stretch"):
            hid = [o for o in opts if o not in st.session_state[key]]
            new = {**sett, "hidden_columns": {**sett.get("hidden_columns", {}), tab: hid}}
            ok, err = _put(SET_FILE, json.dumps(new, ensure_ascii=False, indent=1), sha,
                           f"GAP-analys: standardkolumner i {title}")
            st.session_state.pop("gap_set", None)
            st.session_state["gap_flash"] = (ok, f"Standardkolumnerna i {title} är sparade." if ok else err)
            st.rerun()
    chosen = set(st.session_state[key])
    return [c for c in cols if c in fixed or sid(c) in chosen]


# ---------------------------------------------------------------- sidhuvud och filter
h1, h2 = st.columns([3, 2])
h1.title("GAP-analys")
if "gap_flash" in st.session_state:
    _ok, _msg = st.session_state.pop("gap_flash")
    (st.success if _ok else st.error)(_msg)
h2.markdown(f"<div style='text-align:right;padding-top:1.4rem;color:#5d6779'>🟢 Synkad mot Fortnox {ds['generated']}"
            f"<br><small>Köp per produktkategori ({ds.get('categoryField', '')}) · exkl. moms</small></div>",
            unsafe_allow_html=True)

f = st.columns([1.3, 0.8, 2, 1.4, 1.2])
mode = f[0].segmented_control("Period", ["Samma period", "Hela året"], default="Samma period",
                              key="gap_mode", label_visibility="collapsed") or "Samma period"
yr = f[1].selectbox("År", YEARS[::-1], key="gap_year", label_visibility="collapsed")
q = f[2].text_input("Sök", placeholder="Sök kund, kundnummer eller ort …", key="gap_q", label_visibility="collapsed")
flt = f[3].selectbox("Urval", ["Alla kunder", "Kunder med gap", "Kunder med tappade kategorier"],
                     key="gap_filter", label_visibility="collapsed")
hide_empty = f[4].checkbox("Dölj kunder utan köp", value=True, key="gap_hide")
show_inactive = bool(st.session_state.get("gap_inactive", False))

V = YTD if mode == "Samma period" else FULL
yi = YEARS.index(yr)
cur = V[:, :, yi]
# Innevarande år är inte slut: jämför alltid mot samma period förra året (jan – dagens datum),
# även i läget "Hela året", annars ser det ut som att allt har minskat.
running = yr == int(ds["today"][:4])
prev = (YTD if running else V)[:, :, yi - 1] if yi > 0 else np.zeros(cur.shape)
ST = status_matrix(V, yi, prev)

base = CUST.copy()
base["Totalt"] = cur.sum(axis=1)
base["Föregående år"] = prev.sum(axis=1)
base["Gap"] = ((ST == "lost") | (ST == "never")).sum(axis=1)
base["Tappade"] = (ST == "lost").sum(axis=1)
base["Köp någon gång"] = (V > 0).any(axis=(1, 2))

mask = np.ones(len(base), bool)
if not show_inactive:
    mask &= base["Aktiv"].astype(bool).to_numpy()
if hide_empty:
    mask &= base["Köp någon gång"].to_numpy()
if q:
    hay = (base["Kund"].fillna("").astype(str) + " " + base["Kundnr"].fillna("").astype(str) + " "
           + base["Ort"].fillna("").astype(str)).str.lower()
    mask &= hay.str.contains(q.strip().lower(), regex=False).to_numpy()
if flt == "Kunder med gap":
    mask &= (base["Gap"] > 0).to_numpy()
elif flt == "Kunder med tappade kategorier":
    mask &= (base["Tappade"] > 0).to_numpy()
idx = np.flatnonzero(mask)

# ---------------------------------------------------------------- nyckeltal
tot, ptot = cur[idx].sum(), prev[idx].sum()
buyers = int((cur[idx].sum(axis=1) > 0).sum())
cats_bought = int((cur[idx] > 0).sum())
lost_cnt = int((ST[idx] == "lost").sum())
lost_mask = (ST[idx] == "lost") & (prev[idx] > 0)
lost_val = float(prev[idx][lost_mask].sum())
down_mask = ST[idx] == "down"
down_val = float((prev[idx] - cur[idx])[down_mask].sum())
d = pct(tot, ptot)
per_ytd = "jan–" + ds["today"][8:10].lstrip("0") + "/" + ds["today"][5:7].lstrip("0")
per = per_ytd if mode == "Samma period" else "helår"
per_cmp = per_ytd if (mode == "Samma period" or running) else "helår"
k = st.columns(4)
k[0].metric(f"Försäljning {yr} ({per})", short(tot), None if d is None else f"{d:+d} % mot {yr - 1} ({per_cmp})")
k[1].metric("Köpande kunder", f"{buyers:,}".replace(",", " "),
            help=f"I snitt {cats_bought / buyers if buyers else 0:.1f} av {len(CATS)} kategorier per kund")
k[2].metric("Tappade kategorier", f"{lost_cnt:,}".replace(",", " "),
            help=f"{short(lost_val)} köptes i dessa kategorier {yr - 1}")
k[3].metric("Kategoritäckning", f"{(cats_bought / (buyers * len(CATS)) * 100 if buyers else 0):.0f} %",
            help="Andel av möjliga kund × kategori med köp")
k2 = st.columns(4)
k2[0].metric(f"Tappat {yr} mot {yr - 1} ({per_cmp})", kr(-lost_val) if lost_val else "0 kr",
             f"{int(lost_mask.sum()):,} kund × kategori".replace(",", " "), delta_color="off",
             help=f"Det kunderna köpte för {yr - 1} ({per_cmp}) i kategorier där de inte har köpt något alls {yr}.")
k2[1].metric(f"Minskat {yr} mot {yr - 1} ({per_cmp})", kr(-down_val) if down_val else "0 kr",
             f"{int(down_mask.sum()):,} kund × kategori".replace(",", " "), delta_color="off",
             help=f"Hur mycket mindre kunderna köpt {yr} än {yr - 1} ({per_cmp}) i kategorier som minskat mer än 30 % "
                  "(status Minskar). Tappade kategorier räknas inte här.")
k2[2].metric(f"Tappat + minskat", kr(-(lost_val + down_val)) if lost_val + down_val else "0 kr",
             help="Summan av de två till vänster.")

if running and mode == "Hela året":
    st.caption(f"ℹ️ {yr} pågår fortfarande, så jämförelser mot {yr - 1} görs mot samma period ({per_ytd}) – "
               "inte mot hela förra året.")

t1, t2, t3 = st.tabs(["Matris", "Gap-lista", "Kategorier"])

# ---------------------------------------------------------------- matris
with t1:
    st.caption("Belopp per kund och kategori för valt år. "
               "🟥 Tappad (köpte tidigare, inget i år) · ⬜ Aldrig köpt · 🟧 Minskar mer än 30 % · 🟩 Ny i år")
    sub = base.iloc[idx]
    mat = pd.DataFrame(cur[idx], columns=CATS)
    sts = pd.DataFrame(ST[idx], columns=CATS)
    order = np.argsort(-sub["Totalt"].to_numpy(), kind="stable")
    sub, mat, sts = sub.iloc[order].reset_index(drop=True), mat.iloc[order].reset_index(drop=True), sts.iloc[order].reset_index(drop=True)
    chg = [pct(a, b) for a, b in zip(sub["Totalt"], sub["Föregående år"])]
    table = pd.concat([pd.DataFrame({
        "Kundnr": sub["Kundnr"].astype(str), "Kund": sub["Kund"], "Ort": sub["Ort"],
        f"Totalt {yr}": sub["Totalt"].round(0).astype("int64"), "Förändring %": chg, "Gap": sub["Gap"]}), mat.round(0).astype("int64")], axis=1)
    if len(table) > 1500:
        st.info(f"Visar de 1 500 största av {len(table)} kunder – använd sök eller urval för att hitta fler, "
                "eller ladda ner hela tabellen som Excel.")
    mcols = col_picker("matris", "Matris", list(table.columns), ["Kund"], yr)
    shown = table.head(1500)
    colors = pd.DataFrame("", index=shown.index, columns=shown.columns)
    for c in CATS:
        colors[c] = sts[c].head(1500).map(FILL)
    fmt = {c: kr for c in [f"Totalt {yr}", *CATS]}
    fmt["Förändring %"] = pct_txt
    styler = shown.style.apply(lambda _: colors, axis=None).format(fmt)
    cfg = {"Kund": st.column_config.TextColumn("Kund", width="large", pinned=True),
           "Kundnr": st.column_config.TextColumn("Kundnr", width="small", pinned=True)}
    st.dataframe(styler, hide_index=True, width="stretch", height=600, column_config=cfg, column_order=mcols)
    st.download_button("Ladda ner matrisen som Excel", excel_bytes(table, f"Matris {yr}"),
                       f"GAP-matris-{yr}.xlsx", key="dl_mat")

    # ---- artiklar per kategori (Excel)
    @st.cache_data(show_spinner=False)
    def articles_in_category(generated, k_idx, _ds):
        idxs = {i for i, a in enumerate(_ds.get("articles", [])) if a[3] == k_idx}
        per_year, buyers = {}, {}
        for c, a, y, f_, t_ in _ds.get("artCells", []):
            if a in idxs:
                per_year.setdefault(a, [0.0] * len(_ds["years"]))[y] += f_
                buyers.setdefault(a, set()).add(c)
        recs = []
        for i in sorted(idxs):
            a = _ds["articles"][i]
            rec = {"Artikelnummer": str(a[0]), "Benämning": a[1], "EAN": a[2], "Kategori": _ds["categories"][k_idx]}
            for y_i, y in enumerate(_ds["years"]):
                rec[f"Försäljning {y} (kr)"] = round(per_year.get(i, [0.0] * len(_ds["years"]))[y_i])
            rec["Antal kunder"] = len(buyers.get(i, ()))
            recs.append(rec)
        df = pd.DataFrame(recs)
        return df.sort_values(f"Försäljning {_ds['years'][-1]} (kr)", ascending=False) if len(df) else df

    st.markdown("**Artiklar i en kategori**")
    missing = MISSING_CAT
    ac = st.columns([2, 2, 3])
    cat_pick = ac[0].selectbox("Kategori", CATS, index=CATS.index(missing) if missing in CATS else 0,
                               key="gap_artcat", label_visibility="collapsed")
    arts_df = articles_in_category(ds["generated"], CATS.index(cat_pick), ds)
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in cat_pick).strip() or "kategori"
    ac[1].download_button(f"Ladda ner {len(arts_df)} artiklar som Excel", excel_bytes(arts_df, safe),
                          f"GAP-artiklar-{safe}.xlsx", key="dl_artcat", disabled=len(arts_df) == 0,
                          width="stretch")
    ac[2].caption(f"Alla artiklar i vald kategori med artikelnummer, benämning, EAN, försäljning per år och "
                  f"antal kunder. Kategorin kommer från fältet {ds.get('categoryField', '')} i Fortnox.")

# ---------------------------------------------------------------- gap-lista
with t2:
    gl = st.columns([4, 1.2])
    with_never = gl[0].checkbox("Ta med kategorier som kunden aldrig köpt", value=False, key="gap_never")
    with gl[1]:
        gcols = col_picker("gaplista", "Gap-listan", ["Kundnr", "Kund", "Kategori", "Status", str(yr), str(yr - 1),
                                                     "Snitt tidigare år", "Bästa år"], ["Kund"], yr)
    wanted = {"lost", "down"} | ({"never"} if with_never else set())
    rows = []
    sub_st, sub_cur = ST[idx], cur[idx]
    sub_prev_years = V[idx, :, :yi]
    for i, ci in enumerate(idx):
        for kk in range(len(CATS)):
            s_ = sub_st[i, kk]
            if s_ not in wanted:
                continue
            hist = sub_prev_years[i, kk]
            rows.append({"Kundnr": str(CUST.at[ci, "Kundnr"]), "Kund": CUST.at[ci, "Kund"], "Kategori": CATS[kk],
                         "Status": STATUS_TXT[s_], str(yr): round(sub_cur[i, kk]),
                         str(yr - 1): round(prev[ci, kk]) if yi > 0 else 0,
                         "Snitt tidigare år": round(hist.mean()) if hist.size else 0,
                         "Bästa år": round(hist.max()) if hist.size else 0, "_rank": {"lost": 0, "down": 1, "never": 2}[s_]})
    if not rows:
        st.info("Inga gap för valt urval.")
    else:
        g = pd.DataFrame(rows).sort_values(["_rank", "Bästa år", str(yr - 1)], ascending=[True, False, False]).drop(columns="_rank")
        st.caption(f"{len(g):,} rader".replace(",", " ") + (" – visar de 5 000 första" if len(g) > 5000 else ""))
        gs = g.head(5000)
        gst = gs.style.apply(lambda col: col.map({"Tappad": FILL["lost"], "Minskar": FILL["down"],
                                                  "Aldrig köpt": FILL["never"]}).fillna(""), subset=["Status"]).format(
            {c: kr for c in [str(yr), str(yr - 1), "Snitt tidigare år", "Bästa år"]})
        st.dataframe(gst, hide_index=True, width="stretch", height=600, column_order=gcols,
                     column_config={"Kund": st.column_config.TextColumn("Kund", width="large", pinned=True)})
        st.download_button("Ladda ner gap-listan som Excel", excel_bytes(g, f"Gap {yr}"), f"GAP-lista-{yr}.xlsx", key="dl_gap")

# ---------------------------------------------------------------- kategorier
with t3:
    active = (cur[idx].sum(axis=1) > 0)
    crow = []
    for kk, name in enumerate(CATS):
        tot_k, prev_k = cur[idx, kk].sum(), prev[idx, kk].sum()
        b = int((cur[idx, kk] > 0).sum())
        crow.append({"Kategori": name, f"Försäljning {yr}": round(tot_k), f"Försäljning {yr - 1}": round(prev_k),
                     "Förändring %": pct(tot_k, prev_k), "Köpande kunder": b,
                     "Penetration %": round(b / active.sum() * 100) if active.sum() else 0,
                     "Tappade kunder": int((ST[idx, kk] == "lost").sum()),
                     "Aldrig köpt": int((ST[idx, kk] == "never").sum())})
    cdf = pd.DataFrame(crow).sort_values(f"Försäljning {yr}", ascending=False)
    ccols = col_picker("kategorier", "Kategorier", list(cdf.columns), ["Kategori"], yr)
    cst = cdf.style.format({f"Försäljning {yr}": kr, f"Försäljning {yr - 1}": kr, "Förändring %": pct_txt,
                            "Penetration %": lambda v: f"{v:.0f} %"}).apply(
        lambda col: ["color:#1e7a46" if (v is not None and not pd.isna(v) and v >= 0) else "color:#b3261e" for v in col],
        subset=["Förändring %"])
    st.dataframe(cst, hide_index=True, width="stretch", height=min(40 + 35 * len(cdf), 700), column_order=ccols,
                 column_config={"Kategori": st.column_config.TextColumn("Kategori", width="large", pinned=True)})
    st.download_button("Ladda ner kategorierna som Excel", excel_bytes(cdf, f"Kategorier {yr}"),
                       f"GAP-kategorier-{yr}.xlsx", key="dl_cat")

# ---------------------------------------------------------------- fot
c1, c2 = st.columns([1.2, 5])
c1.toggle("Visa inaktiva kunder", key="gap_inactive")
c2.caption("Synkas automatiskt från Fortnox varannan timme på vardagar (en gång per dag på helger).")
if status and not status.get("ok"):
    st.caption(f"⚠️ Senaste synken misslyckades ({status.get('at', '')[:16].replace('T', ' ')} UTC): "
               f"{status.get('error', '')}. Rapporten visar senaste lyckade data.")
