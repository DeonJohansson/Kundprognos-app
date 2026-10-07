"""Sida: GAP-analys – vilka produktkategorier varje kund köper, har slutat köpa eller aldrig köpt.

Datan kommer från GAP-databasen (DATABASE_URL), som synkas mot Fortnox i bakgrunden av
fortnox_core. Visas som tydliga tabeller i Excel-stil.
"""
import io
import os
import sys
import threading

import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fortnox_core as fc  # noqa: E402

FILL = {"lost": "background-color:#f8d7d3;color:#8a1c12", "never": "background-color:#eef1f6;color:#8a93a3",
        "down": "background-color:#fbe9c6", "new": "background-color:#d6efe0", "grow": "background-color:#e3f3e8",
        "ok": ""}
STATUS_TXT = {"lost": "Tappad", "down": "Minskar", "never": "Aldrig köpt", "new": "Ny i år", "grow": "Växer", "ok": "Stabil"}


# ---------------------------------------------------------------- databas och synk
if not os.environ.get("DATABASE_URL"):
    st.title("GAP-analys")
    st.error("GAP-analysens databas är inte inlagd. Lägg till `DATABASE_URL` (samma som i den gamla "
             "GAP-appen) under appens **Settings → Secrets** på Streamlit.")
    st.stop()


@st.cache_resource
def db_conn():
    return fc.DB(autocommit=True)


def _db_error(e):
    import re as _re
    msg = _re.sub(r"postgres(ql)?://\S+", "postgresql://…", str(e)).strip().splitlines()
    msg = msg[0] if msg else type(e).__name__
    st.title("GAP-analys")
    st.error("**GAP-analysen når inte sin databas (Neon) just nu.** Övriga sidor fungerar som vanligt.")
    low = msg.lower()
    if "quota" in low or "compute time" in low or "exceeded" in low:
        st.info("Neon säger att databasens gratiskvot är slut för månaden. Logga in på console.neon.tech och "
                "kontrollera **Usage** – kvoten nollställs vid månadsskiftet, eller uppgradera planen.")
    elif "password" in low or "authentication" in low:
        st.info("Neon nekade inloggningen. Kontrollera att `DATABASE_URL` under `[gap]` i appens Secrets "
                "är samma som connection string i Neon (lösenordet kan ha bytts).")
    elif "timeout" in low or "timed out" in low or "could not connect" in low or "connection" in low:
        st.info("Databasen svarade inte i tid. Neon väcker databasen vid första besöket efter en paus – "
                "vänta en halv minut och klicka **Försök igen**.")
    st.caption(f"Tekniskt felmeddelande: {msg}")
    if st.button("Försök igen", type="primary"):
        db_conn.clear()
        st.rerun()
    st.stop()


def db():
    try:
        con = db_conn()
        try:
            con.execute("SELECT 1").fetchone()
        except Exception:
            db_conn.clear()
            con = db_conn()
        return con
    except Exception as e:
        db_conn.clear()
        _db_error(e)


D = db()


def start_sync_if_stale():
    """Startar en bakgrundssynk mot Fortnox när någon öppnar sidan och datan är äldre än en timme.
    Ingen ständig loop – då kan Neon-databasen somna mellan besöken och sparar kvot."""
    if st.session_state.get("gap_sync_started"):
        return
    st.session_state["gap_sync_started"] = True

    def run():
        try:
            fc.sync_if_stale(60)
        except Exception as e:
            print("GAP-synkfel:", e, flush=True)

    threading.Thread(target=run, daemon=True).start()


@st.cache_data(ttl=60, show_spinner="Hämtar GAP-data …")
def load_ds():
    return fc.load_report(D)


s = fc.load_settings(D)
if not (s["client_id"] and s["client_secret"] and s["tenant_id"]):
    st.title("GAP-analys")
    st.warning("Fortnox-kopplingen för GAP-analysen saknas. Lägg in `FORTNOX_CLIENT_ID` och "
               "`FORTNOX_CLIENT_SECRET` från den gamla GAP-appen under **Settings → Secrets**.")
    st.stop()
start_sync_if_stale()
ds = load_ds()
if not ds:
    st.title("GAP-analys")
    st.info("Ingen GAP-rapport finns ännu. Första hämtningen från Fortnox pågår – ladda om sidan om en stund.")
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


def status_matrix(V, yi):
    v = V[:, :, yi]
    ever = (V[:, :, :yi] > 0).any(axis=2) if yi > 0 else np.zeros(v.shape, bool)
    p1 = V[:, :, yi - 1] if yi > 0 else np.zeros(v.shape)
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


# ---------------------------------------------------------------- sidhuvud och filter
h1, h2 = st.columns([3, 2])
h1.title("GAP-analys")
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
ST = status_matrix(V, yi)
cur = V[:, :, yi]
prev = V[:, :, yi - 1] if yi > 0 else np.zeros(cur.shape)

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
lost_val = float(prev[idx][ST[idx] == "lost"].sum())
d = pct(tot, ptot)
per = "jan–" + ds["today"][8:10].lstrip("0") + "/" + ds["today"][5:7].lstrip("0") if mode == "Samma period" else "helår"
k = st.columns(4)
k[0].metric(f"Försäljning {yr} ({per})", short(tot), None if d is None else f"{d:+d} % mot {yr - 1}")
k[1].metric("Köpande kunder", f"{buyers:,}".replace(",", " "),
            help=f"I snitt {cats_bought / buyers if buyers else 0:.1f} av {len(CATS)} kategorier per kund")
k[2].metric("Tappade kategorier", f"{lost_cnt:,}".replace(",", " "),
            help=f"{short(lost_val)} köptes i dessa kategorier {yr - 1}")
k[3].metric("Kategoritäckning", f"{(cats_bought / (buyers * len(CATS)) * 100 if buyers else 0):.0f} %",
            help="Andel av möjliga kund × kategori med köp")

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
    shown = table.head(1500)
    colors = pd.DataFrame("", index=shown.index, columns=shown.columns)
    for c in CATS:
        colors[c] = sts[c].head(1500).map(FILL)
    fmt = {c: kr for c in [f"Totalt {yr}", *CATS]}
    fmt["Förändring %"] = pct_txt
    styler = shown.style.apply(lambda _: colors, axis=None).format(fmt)
    cfg = {"Kund": st.column_config.TextColumn("Kund", width="large", pinned=True),
           "Kundnr": st.column_config.TextColumn("Kundnr", width="small", pinned=True)}
    st.dataframe(styler, hide_index=True, width="stretch", height=600, column_config=cfg)
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
    missing = fc.load_settings(D)["kategori"].get("saknas", "Okategoriserad")
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
    with_never = st.checkbox("Ta med kategorier som kunden aldrig köpt", value=False, key="gap_never")
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
        st.dataframe(gst, hide_index=True, width="stretch", height=600,
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
    cst = cdf.style.format({f"Försäljning {yr}": kr, f"Försäljning {yr - 1}": kr, "Förändring %": pct_txt,
                            "Penetration %": lambda v: f"{v:.0f} %"}).apply(
        lambda col: ["color:#1e7a46" if (v is not None and not pd.isna(v) and v >= 0) else "color:#b3261e" for v in col],
        subset=["Förändring %"])
    st.dataframe(cst, hide_index=True, width="stretch", height=min(40 + 35 * len(cdf), 700),
                 column_config={"Kategori": st.column_config.TextColumn("Kategori", width="large", pinned=True)})
    st.download_button("Ladda ner kategorierna som Excel", excel_bytes(cdf, f"Kategorier {yr}"),
                       f"GAP-kategorier-{yr}.xlsx", key="dl_cat")

# ---------------------------------------------------------------- fot
c1, c2, _ = st.columns([1.2, 1.2, 4])
c1.toggle("Visa inaktiva kunder", key="gap_inactive")
if c2.button("Hämta senaste från Fortnox nu"):
    with st.spinner("Hämtar nytt från Fortnox …"):
        try:
            ran = fc.sync()
        except Exception as e:
            ran = None
            st.toast(f"Kunde inte nå Fortnox just nu – visar senaste data. ({e})")
    if ran:
        load_ds.clear()
        arrays.clear()
        st.rerun()
    elif ran is False:
        st.toast("En hämtning pågår redan – nya siffror kommer inom några minuter.")
if D.meta_get("sync_error"):
    st.caption(f"⚠️ Senaste synken misslyckades: {D.meta_get('sync_error')}. Appen försöker igen automatiskt.")
