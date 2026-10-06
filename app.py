"""Kundprognos 2026 – Streamlit-version av Excel-rapporten.

Koden är publik men innehåller ingen kunddata. Datan ligger i det privata repot
DeonJohansson/Kundprognos-2026:
    data.json       – skrivs om varje morgon av Fortnox-synken (GitHub Actions)
    overrides.json  – manuella ändringar av Säljare, 2026 Mål, Possible Increase och
                      Total 2026 Goal, sparade från den här appen

Streamlit Secrets:
    APP_PASSWORD = "..."
    GITHUB_TOKEN = "github_pat_..."   # fine-grained, endast Kundprognos-2026, Contents: Read and write
"""
import base64
import hmac
import io
import json
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Kundprognos 2026", page_icon="📊", layout="wide")

DATA_REPO = "DeonJohansson/Kundprognos-2026"
HERE = os.path.dirname(__file__)
TZ = ZoneInfo("Europe/Stockholm")
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
EDIT_FIELDS = {"Säljare": "sal", "2026 Mål": "mal", "Possible Increase": "inc", "Total 2026 Goal": "tg"}


# ---------------------------------------------------------------- inloggning
def check_password():
    pw = st.secrets.get("APP_PASSWORD")
    if not pw:
        st.error("APP_PASSWORD saknas i appens Secrets.")
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("Kundprognos 2026")
    with st.form("login"):
        given = st.text_input("Lösenord", type="password")
        if st.form_submit_button("Logga in"):
            if hmac.compare_digest(given, pw):
                st.session_state["auth_ok"] = True
                st.rerun()
            st.error("Fel lösenord.")
    st.stop()


check_password()


# ---------------------------------------------------------------- GitHub-lagring
def _gh_headers():
    return {"Authorization": f"Bearer {st.secrets['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def gh_get(path):
    """Returnerar (text, sha) – lokalt läge utan token läser filer bredvid appen."""
    if not st.secrets.get("GITHUB_TOKEN"):
        p = os.path.join(HERE, path)
        return (open(p, encoding="utf-8").read(), None) if os.path.exists(p) else (None, None)
    r = requests.get(f"https://api.github.com/repos/{DATA_REPO}/contents/{path}", headers=_gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        st.error(f"Kunde inte hämta {path} från GitHub ({r.status_code}). Kontrollera GITHUB_TOKEN i Secrets.")
        st.stop()
    j = r.json()
    if j.get("content") is None or j.get("encoding") != "base64":   # stora filer
        raw = requests.get(j["download_url"], headers=_gh_headers(), timeout=30).text
        return raw, j["sha"]
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def gh_put(path, text, sha, message):
    if not st.secrets.get("GITHUB_TOKEN"):
        open(os.path.join(HERE, path), "w", encoding="utf-8").write(text)
        return True, None
    body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{DATA_REPO}/contents/{path}", headers=_gh_headers(),
                     json=body, timeout=30)
    if r.status_code in (200, 201):
        return True, None
    if r.status_code == 409 or r.status_code == 422:
        return False, "Någon annan sparade samtidigt. Ladda om sidan och gör ändringen igen."
    if r.status_code in (401, 403):
        return False, "GitHub-nyckeln saknar skrivbehörighet (Contents: Read and write)."
    return False, f"GitHub svarade {r.status_code}."


@st.cache_data(ttl=3600, show_spinner="Hämtar data …")
def fetch_data():
    raw, _ = gh_get("data.json")
    if raw is None:
        st.error("data.json saknas i datarepot.")
        st.stop()
    return raw


def fetch_overrides():
    """Läses vid varje sidladdning så att ändringar syns direkt för alla."""
    if "ovr" not in st.session_state:
        raw, sha = gh_get("overrides.json")
        st.session_state["ovr"] = (json.loads(raw) if raw else {"rows": {}, "log": []}, sha)
    return st.session_state["ovr"]


# ---------------------------------------------------------------- data
def goal_of(r):
    return r["tg"] if r.get("tg") is not None else (r.get("mal") or 0) + (r.get("inc") or 0)


def apply_overrides(rows, ovr):
    out = []
    for r in rows:
        o = ovr.get("rows", {}).get(r["id"])
        out.append({**r, **o} if o else r)
    return out


def frame(rows, main):
    recs = []
    for r in rows:
        m = (r.get("m") or [None] * 12) + [None] * 12
        rec = {"id": r["id"], "Kundnamn": r.get("n", "")}
        if main:
            goal = goal_of(r)
            rec.update({
                "Län": r.get("lan", ""), "Industry": r.get("ind", ""), "Säljare": r.get("sal", ""),
                "2024 Försäljning": r.get("y24"), "2025 Försäljning": r.get("y25"),
                "2026 Mål": r.get("mal"), "Possible Increase": r.get("inc"),
                "Total 2026 Goal": goal, "Noteringar": r.get("note", ""),
            })
        for i, mn in enumerate(MONTHS):
            rec[mn] = m[i]
        total = sum(x or 0 for x in m[:12])
        rec["TOTAL"] = total
        if main:
            rec["% of Goal"] = (total / goal * 100) if goal and goal > 0 else None
        rec["Fortnox kundnr"] = r.get("fnr", "")
        recs.append(rec)
    df = pd.DataFrame(recs).set_index("id")
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]) and c != "% of Goal":
            df[c] = df[c].round(0)
    return df


data = json.loads(fetch_data())
ovr, ovr_sha = fetch_overrides()
meta = data.get("meta", {})
rows_eff = apply_overrides(data.get("rows", []), ovr)
df_main = frame(rows_eff, True)
df_nya = frame(data.get("nya", []), False)


# ---------------------------------------------------------------- hjälpare för visning
def kr(v):
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:,.2f} mkr".replace(",", " ").replace(".", ",")
    if a >= 1e3:
        return f"{v / 1e3:,.0f} tkr".replace(",", " ")
    return f"{v:,.0f} kr".replace(",", " ")


NUM = st.column_config.NumberColumn
TXT = st.column_config.TextColumn


def colcfg(cols):
    cfg = {}
    for c in cols:
        if c == "Kundnamn":
            cfg[c] = TXT(c, width="large", pinned=True)
        elif c in ("Län", "Industry", "Noteringar"):
            cfg[c] = TXT(c, width="medium")
        elif c in ("Säljare", "Fortnox kundnr"):
            cfg[c] = TXT(c, width="small")
        elif c == "% of Goal":
            cfg[c] = st.column_config.ProgressColumn(c, format="%.1f %%", min_value=0, max_value=100, width="small")
        else:
            cfg[c] = NUM(c, format="localized", step=1)
    return cfg


def filters(df, key, with_dims):
    cols = st.columns([2, 1, 1, 1] if with_dims else [1])
    q = cols[0].text_input("Sök", placeholder="Sök kund, län, bransch …", key=f"q_{key}", label_visibility="collapsed")
    out = df
    if with_dims:
        for col, name in zip(cols[1:], ["Säljare", "Län", "Industry"]):
            opts = sorted(v for v in df[name].dropna().unique() if v)
            sel = col.multiselect(name, opts, key=f"{name}_{key}", placeholder=name, label_visibility="collapsed")
            if sel:
                out = out[out[name].isin(sel)]
    if q:
        hay = out.select_dtypes(include="object").astype(str).agg(" ".join, axis=1).str.lower()
        out = out[hay.str.contains(q.lower(), regex=False)]
    return out


def totals_row(df, main):
    t = {c: (None if pd.api.types.is_numeric_dtype(df[c]) else "") for c in df.columns}
    t["Kundnamn"] = f"Summa ({len(df)} rader)"
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]) and c != "% of Goal":
            t[c] = df[c].fillna(0).sum()
    if main:
        g = t.get("Total 2026 Goal") or 0
        t["% of Goal"] = (t["TOTAL"] / g * 100) if g else None
    return pd.DataFrame([t])


def excel_bytes(df, sheet):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name=sheet)
    return buf.getvalue()


# ---------------------------------------------------------------- redigering
def txt(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def num_or_none(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    f = float(v)
    return int(f) if f.is_integer() else round(f, 2)


def fmt_val(v):
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        return f"{v:,.0f}".replace(",", " ")
    return str(v)


def save_edits(before: pd.DataFrame, after: pd.DataFrame, who: str):
    """Jämför redigerad tabell mot nuläget och sparar skillnaderna i overrides.json."""
    base = {r["id"]: r for r in data.get("rows", [])}       # värden från Excel/synken (utan overrides)
    rows_ovr = {k: dict(v) for k, v in ovr.get("rows", {}).items()}
    log = list(ovr.get("log", []))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes = 0
    for rid in after.index:
        b, a = before.loc[rid], after.loc[rid]
        mal_changed = num_or_none(a["2026 Mål"]) != num_or_none(b["2026 Mål"])
        inc_changed = num_or_none(a["Possible Increase"]) != num_or_none(b["Possible Increase"])
        tg_changed = num_or_none(a["Total 2026 Goal"]) != num_or_none(b["Total 2026 Goal"])
        sal_changed = txt(a["Säljare"]) != txt(b["Säljare"])
        if not (mal_changed or inc_changed or tg_changed or sal_changed):
            continue
        o = rows_ovr.get(rid, {})
        new = {"sal": txt(a["Säljare"]), "mal": num_or_none(a["2026 Mål"]),
               "inc": num_or_none(a["Possible Increase"])}
        auto_total = (new["mal"] or 0) + (new["inc"] or 0)
        if tg_changed and num_or_none(a["Total 2026 Goal"]) != auto_total:
            new["tg"] = num_or_none(a["Total 2026 Goal"])      # Total skrivs in manuellt
        else:
            new["tg"] = None                                    # Total = Mål + Possible Increase
        orig = base.get(rid, {})
        for f in ("sal", "mal", "inc", "tg"):
            old_eff = o.get(f, orig.get(f))
            if new[f] != old_eff:
                log.append({"at": now, "by": who or "okänd", "id": rid, "kund": orig.get("n", rid),
                            "falt": f, "fran": old_eff, "till": new[f]})
                changes += 1
            if f == "tg":
                if new["tg"] is None:
                    o.pop("tg", None)
                else:
                    o["tg"] = new["tg"]
            elif new[f] == orig.get(f):
                o.pop(f, None)                                  # samma som originalet: ingen override
            else:
                o[f] = new[f]
        if o:
            rows_ovr[rid] = o
        else:
            rows_ovr.pop(rid, None)
    if not changes:
        return True, "Inga ändringar att spara."
    doc = {"rows": rows_ovr, "log": log[-1000:], "updatedAt": now}
    ok, err = gh_put("overrides.json", json.dumps(doc, ensure_ascii=False, indent=1), ovr_sha,
                     f"Manuell ändring av {changes} värde(n) i kundprognosen ({who or 'okänd'})")
    if ok:
        st.session_state.pop("ovr", None)
        return True, f"Sparat: {changes} ändring(ar)."
    return False, err


# ---------------------------------------------------------------- sidhuvud
upd = meta.get("updatedAt")
try:
    upd_txt = datetime.fromisoformat(upd).astimezone(TZ).strftime("%Y-%m-%d kl. %H:%M")
except Exception:
    upd_txt = upd or "okänt"
c1, c2 = st.columns([3, 2])
c1.title("Kundprognos 2026")
c2.markdown(
    f"<div style='text-align:right;padding-top:1.4rem;color:#5d6779'>🟢 Live från Fortnox · uppdaterad {upd_txt}"
    f"<br><small>{meta.get('source', '')}</small></div>", unsafe_allow_html=True)

if msg := st.session_state.pop("flash", None):
    st.toast(msg[1], icon="✅" if msg[0] else "⚠️")

tab1, tab2, tab3 = st.tabs(["Kunder 2026", "Nya kunder", "Ändringslogg"])

with tab1:
    view = filters(df_main, "main", True)
    tot, goal = view["TOTAL"].sum(), view["Total 2026 Goal"].sum()
    k = st.columns(5)
    k[0].metric("Försäljning 2026", kr(tot))
    k[1].metric("Total 2026 Goal", kr(goal))
    k[2].metric("% av mål", f"{(tot / goal * 100 if goal else 0):.1f} %".replace(".", ","),
                help=f"Tidsandel av året: {date.today().timetuple().tm_yday / 365 * 100:.0f} %")
    k[3].metric("Kvar till mål", kr(max(0, goal - tot)))
    k[4].metric("2025 Försäljning", kr(view["2025 Försäljning"].fillna(0).sum()))

    edit = st.toggle("✏️ Redigera Säljare, 2026 Mål, Possible Increase och Total 2026 Goal", key="edit_mode")
    cfg = colcfg(view.columns)
    if not edit:
        st.dataframe(view, column_config=cfg, hide_index=True, use_container_width=True, height=560)
    else:
        st.caption("Klicka i en cell för att ändra. **Total 2026 Goal** räknas om automatiskt som Mål + "
                   "Possible Increase, om du inte skriver in en egen total. Ändringarna sparas först när du "
                   "klickar på **Spara ändringar**.")
        edited = st.data_editor(view, column_config=cfg, hide_index=True, use_container_width=True, height=560,
                                disabled=[c for c in view.columns if c not in EDIT_FIELDS], key="editor")
        # räkna om Total för rader där bara Mål/Increase ändrats
        auto = edited["2026 Mål"].fillna(0) + edited["Possible Increase"].fillna(0)
        mal_inc_changed = (edited["2026 Mål"].fillna(0) != view["2026 Mål"].fillna(0)) | \
                          (edited["Possible Increase"].fillna(0) != view["Possible Increase"].fillna(0))
        tg_untouched = edited["Total 2026 Goal"].fillna(0) == view["Total 2026 Goal"].fillna(0)
        edited.loc[mal_inc_changed & tg_untouched, "Total 2026 Goal"] = auto[mal_inc_changed & tg_untouched]
        n_changed = sum(1 for rid in edited.index
                        if txt(edited.at[rid, "Säljare"]) != txt(view.at[rid, "Säljare"])
                        or any(num_or_none(edited.at[rid, c]) != num_or_none(view.at[rid, c])
                               for c in ("2026 Mål", "Possible Increase", "Total 2026 Goal")))
        cc = st.columns([2, 1, 1])
        who = cc[0].text_input("Ditt namn (visas i ändringsloggen)", key="who",
                               placeholder="t.ex. Deon")
        cc[1].markdown(f"<div style='padding-top:2rem'>{n_changed} rad(er) ändrade</div>", unsafe_allow_html=True)
        if cc[2].button("Spara ändringar", type="primary", disabled=n_changed == 0, use_container_width=True):
            ok, text = save_edits(view, edited, who.strip())
            st.session_state["flash"] = (ok, text)
            if ok:
                st.session_state.pop("editor", None)
            st.rerun()
    st.dataframe(totals_row(view, True), column_config=cfg, hide_index=True, use_container_width=True)
    if meta.get("unmatchedCount"):
        with st.expander(f"{meta['unmatchedCount']} kunder är inte kopplade till Fortnox (visar Excel-siffror)"):
            st.write(", ".join(meta.get("unmatched", [])))
    st.download_button("Ladda ner som Excel", excel_bytes(view, "Kunder 2026"), "Kundprognos_2026.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab2:
    view2 = filters(df_nya, "nya", False)
    k = st.columns(3)
    k[0].metric("Försäljning nya kunder", kr(view2["TOTAL"].sum()))
    k[1].metric("Antal nya kunder", len(view2))
    k[2].metric("Med försäljning", int((view2["TOTAL"] > 0).sum()))
    cfg2 = colcfg(view2.columns)
    st.dataframe(view2.sort_values("TOTAL", ascending=False), column_config=cfg2, hide_index=True,
                 use_container_width=True, height=560)
    st.dataframe(totals_row(view2, False), column_config=cfg2, hide_index=True, use_container_width=True)
    st.download_button("Ladda ner som Excel", excel_bytes(view2, "Nya kunder"), "Nya_kunder_2026.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab3:
    log = list(reversed(ovr.get("log", [])))
    if not log:
        st.info("Inga manuella ändringar ännu.")
    else:
        names = {"sal": "Säljare", "mal": "2026 Mål", "inc": "Possible Increase", "tg": "Total 2026 Goal"}
        st.dataframe(pd.DataFrame([{
            "Tid": datetime.fromisoformat(x["at"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
            "Av": x.get("by"), "Kund": x.get("kund"), "Fält": names.get(x.get("falt"), x.get("falt")),
            "Från": fmt_val(x.get("fran")),
            "Till": "(Mål + Increase)" if x.get("till") is None and x.get("falt") == "tg" else fmt_val(x.get("till")),
        } for x in log]), hide_index=True, use_container_width=True)
