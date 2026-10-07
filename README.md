# Optinord rapporter

En Streamlit-app med fyra sidor, ett gemensamt lösenord och en meny i sidofältet:

| Sida | Kod | Data |
|---|---|---|
| Kundprognos 2026 | `views/kundprognos.py` | privata repot Kundprognos-2026 (synkas från Fortnox av GitHub Actions) |
| GAP-analys | `views/gap_analys.py` | `gap_report.json` i Kundprognos-2026 (synkas från Fortnox av GitHub Actions) |
| Fortnox-dashboard | `views/dashboard_fortnox.py` | Fortnox direkt |
| Salesforce | `views/dashboard_salesforce.py` | Salesforce direkt |

Koden innehåller ingen kunddata och inga nycklar. Allt hemligt ligger i appens Secrets:

```toml
APP_PASSWORD = "…"
GITHUB_TOKEN = "github_pat_…"        # Kundprognos-2026, Contents: Read and write

[dashboard]
FORTNOX_CLIENT_ID = "…"
FORTNOX_CLIENT_SECRET = "…"
FORTNOX_REFRESH_TOKEN = "…"
GITHUB_TOKEN = "…"                   # gist-nyckeln från gamla optinord-dashboard
GIST_ID = "…"
SALESFORCE_CONSUMER_KEY = "…"
SALESFORCE_CONSUMER_SECRET = "…"
```
