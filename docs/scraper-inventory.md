# Scraper Inventory

**Status key:** Unconfirmed · Confirmed · Broken · Deprecated

**Generation key:**
- **Gen 1** — Fully self-contained; inline `DatabaseManager` class, no imports from `utils/`
- **Gen 1+** — Inline `DatabaseManager` but already imports from `utils/` (e.g. `date_utilities`, `selenium_config`)
- **Gen 2** — Fully modular; uses `utils/db_connection`, `utils/posting_operations`, `utils/company_operations`

---

## ADP

| Company | File | Gen | Status |
|---------|------|-----|--------|
| BancFirst | `adp/bancfirst-adp-api-selenium.py` | Gen 2 | Unconfirmed |
| City National Bank | `adp/city-nb-adp-api-selenium.py` | Gen 1 | Unconfirmed |
| Gateway | `adp/gateway-test.py` | Gen 1 | Unconfirmed |
| Oklahoma Cancer Specialists | `adp/ok-cancer-spec-adp-api-selenium.py` | Gen 2 | Confirmed |
| Paragon Films | `adp/paragon-films-adp-api-selenium.py` | Gen 2 | Unconfirmed |

---

## Applitrack / Frontline

| Company | File | Gen | Status |
|---------|------|-----|--------|
| Broken Arrow Public Schools | `applitrack/baps-applitrack-selenium.py` | Gen 1+ | Unconfirmed |
| Coweta Public Schools | `applitrack/cowetaps-applitrack-selenium.py` | Gen 1+ | Unconfirmed |
| Owasso Public Schools | `applitrack/owassops-applitrack-selenium.py` | Gen 1+ | Unconfirmed |

---

## Paycom

| Company | File | Gen | Status |
|---------|------|-----|--------|
| Alert 360 | `paycom/alert360-paycom-selenium-scrape.py` | Gen 1 | Unconfirmed |
| Clifford Power | `paycom/cliffordpower-paycom-selenium-scrape.py` | Gen 1 | Unconfirmed |

---

## Paylocity

| Company | File | Gen | Status |
|---------|------|-----|--------|
| B+T Group | `paylocity/btgroup-paylocity-soup.py` | Gen 1+ | Unconfirmed |

---

## UltiPro / UKG

| Company | File | Gen | Status |
|---------|------|-----|--------|
| Berendsen | `ultipro/berendsen-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Family & Children's Services | `ultipro/familycs-ultipro-selenium-scrape.py` | Gen 2 | Unconfirmed |
| Flintco | `ultipro/flintco-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Levare | `ultipro/levare-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Mathis Brothers | `ultipro/mathis-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Matrix PDM Engineering | `ultipro/matrixpdm-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Matrix RM | `ultipro/matrixrm-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Matrix SC | `ultipro/matrixsc-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Melton Truck Lines | `ultipro/melton-ultipro-selenium-scrape.py` | Gen 2 | Unconfirmed |
| OAI | `ultipro/oai-ultipro-selenium-scrape.py` | Gen 1+ | Unconfirmed |

---

## Workday

| Company | File | Gen | Status |
|---------|------|-----|--------|
| AAA | `workday/aaa-workday-api-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| AEP | `workday/aep-workday-api-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Aristocrat | `workday/aristocrat-workday-api-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| ChampionX | `workday/championx-workday-api-selenium-scrape.py` | Gen 1+ | Unconfirmed |
| Greenheck | `workday/greenheck-workday-api-selenium-scrape.py` | Gen 2 | Unconfirmed |
| ONEOK | `workday/oneok-workday-api-scrape-selenium.py` | Gen 2 | Unconfirmed |
| Relation Insurance | `workday/relation-workday-api-scrape-selenium.py` | Gen 1+ | Unconfirmed |
| Tulsa County Sheriff's Office | `workday/tcso-scrape-selenium.py` | Gen 1 | Unconfirmed |
| Webco Industries | `workday/webco-workday-scrape-selenium.py` | Gen 2 | Unconfirmed |
| Williams Companies | `workday/williams-workday-api-selenium-scrape.py` | Gen 2 | Unconfirmed |

---

## Custom

| Company | File | Gen | Status |
|---------|------|-----|--------|
| Ameristar Casino | `custom/ameristar-api-selenium.py` | Gen 1 | Unconfirmed |
| Ascension St. John (Broken Arrow) | `custom/ascension_stj_ba.py` | Gen 2 | Unconfirmed |
| Ascension St. John (Jenks) | `custom/ascension_stj_jenks.py` | Gen 2 | Unconfirmed |
| Ascension St. John Medical Center | `custom/ascension_stj_medical.py` | Gen 2 | Unconfirmed |
| Ascension St. John (Owasso) | `custom/ascension_stj_owasso.py` | Gen 2 | Unconfirmed |
| Ascension St. John (Sapulpa) | `custom/ascension_stj_sapulpa.py` | Gen 2 | Unconfirmed |
| Bank of Oklahoma | `custom/bank_of_oklahoma-selenium.py` | Gen 1 | Unconfirmed |
| CareATC Tulsa | `custom/careatc-tulsa.py` | Gen 2 | Unconfirmed |
| City of Bixby | `custom/city_of_bixby.py` | Gen 2 | Unconfirmed |
| City of Broken Arrow | `custom/city_of_brokenarrow.py` | Gen 2 | Unconfirmed |
| City of Jenks | `custom/city_of_jenks.py` | Gen 2 | Unconfirmed |
| City of Sand Springs | `custom/broken - city_of_sand_springs.py` | Gen 2 | Broken |
| Hillcrest Medical Center | `custom/hillcrest_medical_center.py` | Gen 1 | Unconfirmed |

---

## Templates

Reference files for building new scrapers — not run directly.

| Platform | File |
|----------|------|
| ADP | `adp/template-adp-api-selenium.py` |
| Applitrack (attachment-based descriptions) | `applitrack/template-applitrack-selenium-attachment.py` |
| Applitrack (inline descriptions) | `applitrack/template-applitrack-selenium-inline.py` |
| UltiPro | `ultipro/template-ultipro-selenium-scrape.py` |
| Workday | `workday/template-workday-api-selenium.py` |
