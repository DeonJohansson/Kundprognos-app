"""Laddar dashboardens interna inställningar (budget, säljare, besöksmål) från det privata
repot DeonJohansson/Kundprognos-2026 (dashboard_config.json), så att de inte ligger i den publika koden."""
import base64
import json

import requests
import streamlit as st

CONFIG_REPO = "DeonJohansson/Kundprognos-2026"


@st.cache_data(ttl=3600, show_spinner=False)
def load_config():
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:
        st.error("GITHUB_TOKEN saknas i Secrets – dashboardens budget kan inte läsas.")
        st.stop()
    r = requests.get(f"https://api.github.com/repos/{CONFIG_REPO}/contents/dashboard_config.json",
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=20)
    if r.status_code != 200:
        st.error(f"Kunde inte läsa dashboard_config.json från {CONFIG_REPO} ({r.status_code}).")
        st.stop()
    return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
