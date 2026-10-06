# Kundprognos 2026 – Streamlit-app

Visar kundprognosen (Excel-tabellen) med försäljning per kund och månad från Fortnox.

Den här koden innehåller **ingen kunddata**. Datan ligger i ett privat repo och hämtas med en
GitHub-nyckel. Appen kräver lösenord.

Streamlit Secrets:

```toml
APP_PASSWORD = "…"
GITHUB_TOKEN = "github_pat_…"   # fine-grained: endast Kundprognos-2026, Contents: Read and write
```

Manuella ändringar av Säljare, 2026 Mål, Possible Increase och Total 2026 Goal sparas i
`overrides.json` i datarepot, med ändringslogg.
