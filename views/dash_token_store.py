"""
Håller refresh-tokens vid liv över omstarter genom att spara dem i en privat
GitHub Gist. Stödjer flera filer i samma Gist (en per system, t.ex. Fortnox
och Salesforce).
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import streamlit as st
# Dashboardens nycklar ligger under [dashboard] i Secrets (faller tillbaka på rotnivån)
SEC = st.secrets["dashboard"] if "dashboard" in st.secrets else st.secrets
import requests
import streamlit as st


def _gist_headers():
    return {
        "Authorization": f"token {SEC['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }


def load_refresh_token(filename, secret_fallback_key):
    """Hämtar senaste kända refresh-token från Gisten (given fil). Faller
    tillbaka på motsvarande värde i secrets.toml om filen saknas/är tom."""
    try:
        gist_id = SEC["GIST_ID"]
        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=_gist_headers(), timeout=10)
        if resp.status_code == 200:
            files = resp.json().get("files", {})
            if filename in files:
                content = (files[filename].get("content") or "").strip()
                if content:
                    return content
    except Exception:
        pass
    return SEC.get(secret_fallback_key)


def save_refresh_token(new_token, filename):
    """Sparar en (eventuellt roterad) refresh-token till Gisten för nästa omstart."""
    try:
        gist_id = SEC["GIST_ID"]
        requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=_gist_headers(),
            json={"files": {filename: {"content": new_token}}},
            timeout=10,
        )
    except Exception:
        pass

