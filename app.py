"""Optinord rapporter – en Streamlit-app med flera sidor.

Sidor:
    Kundprognos 2026  – views/kundprognos.py  (data från det privata repot Kundprognos-2026)
    GAP-analys        – views/gap_analys.py   (gap_report.json i Kundprognos-2026, synkas av GitHub Actions)

    Fortnox-dashboard – views/dashboard_fortnox.py
    Salesforce        – views/dashboard_salesforce.py

Streamlit Secrets (TOML):
    APP_PASSWORD = "..."          # lösenord för hela appen
    GITHUB_TOKEN = "..."          # läs/skriv-nyckel till Kundprognos-2026 (Kundprognos och GAP-analys)

    [dashboard]                   # Fortnox-dashboard och Salesforce (samma värden som i gamla optinord-dashboard)
    FORTNOX_CLIENT_ID = "..."
    ...
"""
import hmac
import os

import streamlit as st

st.set_page_config(page_title="Optinord rapporter", page_icon="📊", layout="wide")

try:
    if "APP_PASSWORD" in st.secrets:
        os.environ.setdefault("APP_PASSWORD", str(st.secrets["APP_PASSWORD"]))
except Exception:
    pass


def check_password():
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        st.error("APP_PASSWORD saknas i appens Secrets.")
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("Optinord rapporter")
    with st.form("login"):
        given = st.text_input("Lösenord", type="password")
        if st.form_submit_button("Logga in"):
            if hmac.compare_digest(given, pw):
                st.session_state["auth_ok"] = True
                st.rerun()
            st.error("Fel lösenord.")
    st.stop()


check_password()

HERE = os.path.dirname(os.path.abspath(__file__))
V = os.path.join(HERE, "views")
pages = {
    "Rapporter": [
        st.Page(os.path.join(V, "kundprognos.py"), title="Kundprognos 2026", icon="📊", url_path="kundprognos",
                default=True),
        st.Page(os.path.join(V, "gap_analys.py"), title="GAP-analys", icon="🧩", url_path="gap-analys"),
    ],
    "Optinord dashboard": [
        st.Page(os.path.join(V, "dashboard_fortnox.py"), title="Fortnox", icon="📈", url_path="dashboard"),
        st.Page(os.path.join(V, "dashboard_salesforce.py"), title="Salesforce", icon="☁️", url_path="salesforce"),
    ],
}
st.navigation(pages).run()
