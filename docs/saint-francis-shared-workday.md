# Saint Francis Shared Workday Instance

Saint Francis Health System runs a single Workday tenant (`saintfrancis.wd115.myworkdayjobs.com`) shared across all of its hospitals and entities. Each entity is a separate **hiring company** within that tenant, identified by a `hiringCompany` ID in the URL query string.

Because of this, one scraper file is needed per entity — the files are nearly identical except for three values.

## How the scraper works

### 1. Scoping to one entity

The Workday API accepts an `appliedFacets` filter in the POST body. For a shared tenant, `hiringCompany` is the facet that restricts results to a single entity:

```python
body = {
    "appliedFacets": {"hiringCompany": ["0799604f508e1000cec34d97003e0000"]},
    "limit": 20,
    "offset": 0,
    "searchText": "",
}
```

The `hiringCompany` ID comes from the careers page URL for that entity:

```
https://saintfrancis.wd115.myworkdayjobs.com/External?hiringCompany=0799604f508e1000cec34d97003e0000
```

### 2. Location resolution

Each job from the API includes a `locationsText` field — a site name such as `South Campus - Hospital`. This is not a city name and cannot be matched directly to a served city.

The scraper resolves city in two stages:

1. **Description body** — the detail page contains a `Location:` line. The scraper extracts that text and runs it through `match_location_to_city_id`. If the value is a city/state string (`Tulsa, Oklahoma, 74133`) this resolves a `city_id` directly.

2. **Company site record** — the `locationsText` value is used as the `shortname` when looking up `companysite`. If the site record exists and already has a `city_id` set, that value is used as a fallback when step 1 found nothing.

If a site name is not yet in `companysite`, a new row is created automatically with whatever `city_id` was resolved (or `NULL` if neither stage found a city). The row can be updated later once the city is confirmed.

### 3. What the three entity-specific values are

| Variable | Where it lives | Example (Hospital South) |
|---|---|---|
| `COMPANY_NAME` | `setup_logging(...)` and `company_config['name']` | `'Saint Francis Hospital South'` |
| `source_job_board` | `company_config['source_job_board']` | `'SFHB Workday'` |
| `hiring_company_id` | `company_config['hiring_company_id']` | `'0799604f508e1000cec34d97003e0000'` |

The `jobboard` URL also needs to be updated to include the correct `hiringCompany` query parameter.

## Adding a new Saint Francis entity

### Step 1 — Find the hiring company ID

Go to the Saint Francis careers page for the entity (e.g. Saint Francis Hospital, Warren Clinic) and copy the URL. The `hiringCompany` query parameter is the ID you need:

```
https://saintfrancis.wd115.myworkdayjobs.com/External?hiringCompany=<ID HERE>
```

### Step 2 — Copy the Hospital South scraper

```bash
cp workday/stfrancis-hosp-south-workday-api-selenium.py workday/<new-filename>.py
```

Naming convention: `stfrancis-<entity-slug>-workday-api-selenium.py`

Examples:
- `stfrancis-main-workday-api-selenium.py`
- `stfrancis-warren-clinic-workday-api-selenium.py`

### Step 3 — Update the three entity-specific values

In the new file, change:

```python
logger = setup_logging('Saint Francis Hospital South')
```
to the correct entity name.

In `company_config`:

```python
'name': 'Saint Francis Hospital South',         # → new entity name (must match company.common_name in DB)
'jobboard': 'https://saintfrancis.wd115.myworkdayjobs.com/External?hiringCompany=0799604f508e1000cec34d97003e0000',
                                                 # → update hiringCompany param
'hiring_company_id': '0799604f508e1000cec34d97003e0000',
                                                 # → new ID from step 1
'source_job_board': 'SFHB Workday',             # → label for scrapinglog (e.g. 'Warren Clinic Workday')
```

Also rename the class from `SaintFrancisHospSouthScraper` to match the new entity, and update the reference to it in `main()`.

### Step 4 — Review function keywords

The `_FUNCTION_KEYWORDS` dict is already tuned for healthcare roles. If the new entity has a different focus (e.g. a specialty clinic), add or adjust keyword lists as needed.

### Step 5 — Dry run

```bash
python dry_run.py workday/<new-filename>.py
```

Review the output file. Verify that job titles, city IDs, and function mappings look correct before running live.

### Step 6 — Run live

```bash
python workday/<new-filename>.py
```

## Known entities in the Saint Francis network

| Entity | Scraper file | hiring_company_id | source_job_board |
|---|---|---|---|
| Saint Francis Hospital South | `stfrancis-hosp-south-workday-api-selenium.py` | `0799604f508e1000cec34d97003e0000` | `SFHB Workday` |
| Laureate Psychiatric Clinic | `sfh-laureate-workday-api-selenium.py` | `36d103f122b61000ce0e569e15510000` | `St Francis Laureate Workday` |

Add rows to this table as new scrapers are created.

## Note on DB-resolved vs. hardcoded config

The Laureate scraper (`sfh-laureate-workday-api-selenium.py`) uses a slightly different pattern from Hospital South: it resolves `jobboard` URL and `company_type_name` from the `company` table at runtime rather than hardcoding them. The company record must exist in the DB with its `jobboard` field set to the correct URL (including `hiringCompany` param) before the scraper runs. If you copy this scraper for a new entity, make sure the company record is created first.
