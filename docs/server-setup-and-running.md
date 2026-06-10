# Server Setup and Running Scrapers

Instructions for setting up the scrapers on the production Ubuntu server and running them. Run all commands from the server over SSH.

---

## One-Time Setup

### 1. Clone the repo

```bash
cd /home/deploy
git clone https://github.com/eric-price-ok/tulsa-jobboard-scrapers.git tjs-scrapers
cd tjs-scrapers
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Install Google Chrome

**Do not use `apt install chromium-browser`** — on Ubuntu 24.04 LTS this installs a snap-confined Chromium that cannot be driven headlessly by Selenium. Install Google Chrome stable (a proper deb package) instead:

```bash
wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y /tmp/google-chrome.deb
google-chrome --version
```

`webdriver-manager` (included in requirements.txt) automatically downloads the matching ChromeDriver on first scraper run and caches it — no manual ChromeDriver install needed.

If snap Chromium was previously installed, remove it first:
```bash
sudo snap remove chromium
```

### 4. Install the PostgreSQL client (optional)

Only needed if you want to run `psql` directly from the host. The database runs inside Docker, so `docker exec` (see Useful SQL Commands below) works without this.

```bash
sudo apt install -y postgresql-client
```

Note: `postgresql-client-common` alone does not provide the `psql` binary — you need `postgresql-client` (without a version number; apt picks the right one).

### 5. Expose PostgreSQL to the host (optional)

The PostgreSQL container is on the Docker internal network only. If you need to connect directly via `psql` from the host, edit `docker-compose.prod.yml` in the tulsajobspot directory:

```bash
nano /home/deploy/tulsajobspot/docker-compose.prod.yml
```

Change the `db` service from:
```yaml
db:
  ports: []
  restart: unless-stopped
```

To:
```yaml
db:
  ports:
    - "127.0.0.1:5433:5432"
  restart: unless-stopped
```

Then restart the db container:
```bash
cd /home/deploy/tulsajobspot
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db
```

This binds PostgreSQL to localhost only on port 5433. Nothing is exposed publicly.

> Note: if `up -d db` fails with "address already in use", try port 5434 or another unused port and update `POSTGRES_PORT` accordingly.

---

## Running a Scraper

### 1. Activate the virtual environment

```bash
cd /home/deploy/tjs-scrapers
source venv/bin/activate
```

### 2. Set environment variables

Pull credentials directly from the web app's `.env` file:

```bash
export $(grep POSTGRES_ /home/deploy/tulsajobspot/.env | xargs)
export POSTGRES_PORT=5433
export PYTHONPATH=/home/deploy/tjs-scrapers
```

> `POSTGRES_PORT=5433` overrides whatever is in the `.env` file to match the host port binding.  
> `PYTHONPATH` is required so scrapers in subdirectories (e.g. `adp/`, `workday/`) can find the `utils/` package at the repo root.

### 3. Run the scraper

```bash
python adp/ok-cancer-spec-adp-api-selenium.py
```

---

## Useful SQL Commands

All SQL is run through the Docker db container. The database password is required — pull it from the `.env` file using `-e PGPASSWORD=...` so you never have to type it:

```bash
docker exec -e PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2) \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot -c "<SQL HERE>"
```

**Check recent scraping log entries:**
```bash
docker exec -e PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2) \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "SELECT job_board, jobs_found, jobs_added, jobs_updated, status, started_at FROM scrapinglog ORDER BY started_at DESC LIMIT 5;"
```

**Check pending (unapproved) jobs:**
```bash
docker exec -e PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2) \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "SELECT id, job_title, created_at FROM joblistings WHERE approved=false ORDER BY created_at DESC LIMIT 20;"
```

**Delete jobs from a specific scraper (e.g. to re-scrape with fixes):**
```bash
docker exec -e PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2) \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "DELETE FROM joblistings WHERE source_job_board = 'Oklahoma Cancer Specialists ADP';"
```

**Wipe all job listings and reset the ID sequence (full reset on a fresh DB):**
```bash
docker exec -e PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2) \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "TRUNCATE TABLE joblistings RESTART IDENTITY;"
```

---

## Pulling Updates

When new scraper changes are pushed to GitHub:

```bash
cd /home/deploy/tjs-scrapers
git pull origin main
```

No container restart needed — scrapers run directly on the host.
