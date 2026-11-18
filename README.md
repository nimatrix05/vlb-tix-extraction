# VLB-TIX Image Collection & ChatGPT Discovery Pipeline (MVP)

Collects **authorized** book images, organizes them by ISBN, prepares small batches for **manual**
analysis in your ChatGPT account, imports and validates the JSON ChatGPT returns, and builds a
human-approved, versioned **image taxonomy** plus a per-ISBN feature table.

No OpenAI API, no paid calls, no cloud services. Python 3.12, SQLite, local folders.
Runtime dependencies: `httpx`, `Pillow`, `openpyxl` (+ optional `playwright`).

---

## 1. What is implemented vs. future

| Module | Status |
|---|---|
| A — ISBN import (validation, check digits, ISBN-10→13, dedupe, incremental, `,`/`;` CSVs) | ✅ implemented + tested |
| B1 — Approved image-URL manifest + secure downloader (allow-list, SSRF/redirect checks, retries, rate limit, concurrency, resume, size limits) | ✅ implemented + tested with mocked HTTP |
| B1b — Local import of image files delivered by VLB/publisher | ✅ implemented + tested |
| B2 — Authenticated VLB-TIX collector (Playwright persistent profile, manual login + MFA) | ⚠️ implemented, **disabled**, **not verified against the live site** |
| C — Pillow validation, SHA-256 dedupe, safe storage | ✅ implemented + tested |
| D — Batch preparation for manual ChatGPT upload | ✅ implemented + tested |
| E — Reusable prompts (`prompts/`) | ✅ |
| F — Result import & validation (manifest matching, corrections, archive of raw output) | ✅ implemented + tested |
| G — Findings export + category-discovery prompts (multi-part + reconciliation) | ✅ implemented + tested |
| H — CSV review (approve/rename/merge/reject/add), versioned taxonomy, final outputs | ✅ implemented + tested |
| Automated Vision API analysis | 🔜 interface only (`src/pipeline/analysis`) |
| S3 storage / PostgreSQL / distributed workers | 🔜 interfaces kept narrow (`storage.py`, `db.py`); not built |

---

## 2. Setup (Windows CMD)

Your folder already contains a `.venv` (Python 3.12.10). It also has `openai`, `httpx2` and
`playwright` installed from earlier work — **this project does not use `openai` or `httpx2`**, and
`httpx`/`Pillow` are still missing. Install the requirements:

```bat
cd /d %USERPROFILE%\Desktop\vlb.tix
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python -m pip install -e .
```

(If `pip install -e .` is blocked by the company proxy, skip it and run `set PYTHONPATH=src` in
each new CMD window instead.)

Check everything works:

```bat
python -m pytest
```

Expected: `96 passed`. Tests use synthetic data and mocked HTTP only.

> Tip: the Desktop is often synced by OneDrive. SQLite databases do not like being synced while
> open. If you see "database is locked", move the project to e.g. `C:\work\vlb.tix`.

---

## 3. Try it with synthetic demo data (no network)

```bat
python tools\make_demo_data.py
python -m pipeline init
python -m pipeline import-books input\demo_books.csv
python -m pipeline import-local-images input\demo_local_images.csv
python -m pipeline prepare-batch --books 3
python -m pipeline status
```

Open `batches\batch_001\` — it contains `images\`, `manifest.csv`, `extraction_prompt.txt`
and `UPLOAD_INSTRUCTIONS.txt`.

---

## 4. Real workflow

### 4.1 Configure
Copy `config.example.json` to `config.json` and set `allowed_domains` to the image hosts you are
**permitted** to download from. With an empty list, nothing is downloaded (secure default).

### 4.2 Books (Excel or CSV)
Excel files are read directly. The header row is found automatically: title rows above it and a second
(e.g. German) header row below it are skipped. Recognised columns (English or German):
`EAN`/`ISBN`, `ASIN`, `Title`/`Titel`, `Author`/`Autor`, `Brand`/`Marke`/`Publisher`/`Verlag`,
`Publication Date`/`Veröffentlichungsdatum`. **All other columns are ignored** (order quantities,
forecasts, comments, contact persons are never read or stored).

```bat
python -m pipeline import-books "Weekly Book Release - 30.11.2026.xlsx" --dry-run
python -m pipeline import-books "Weekly Book Release - 30.11.2026.xlsx"
python -m pipeline status
```
- Uses the first sheet unless you pass `--sheet "Overview"`.
- If the EAN is missing/invalid but the ASIN is an ISBN-10, it is converted to ISBN-13.
- Skipped rows (non-book EANs, wrong check digits, no ID) are listed with line number and reason in
  `outputs\import_books_skipped.csv`.
- Re-importing (e.g. next week's file) only adds new books and fills empty fields.

CSV works the same way (`,` or `;`, UTF-8 or Windows encoding).

### 4.3 Images — choose an authorized source
**a) Approved URL list** (`input\image_urls.csv`: `isbn13, image_url, source_page, rights_note`)
```bat
python -m pipeline import-image-urls input\image_urls.csv
python -m pipeline collect --limit 20 --dry-run
python -m pipeline collect --limit 20
python -m pipeline retry-failed
```
`--limit` = number of books. Interrupt any time (Ctrl+C) and re-run; finished images are skipped.

**b) Files delivered by VLB / publisher** (`isbn13, file_path, rights_note`)
```bat
python -m pipeline import-local-images input\delivered_images.csv
```

**c) Authenticated VLB-TIX collector** — see section 6. Disabled until permissions are confirmed.

Images land in `data\images\<ISBN>\img_001.jpg`; metadata in `data\pipeline.db`.

### 4.3b Related products, series, previews and bundle candidates
While visiting each book page, `discover-vlbtix` scrolls the whole page (sections load late) and saves:
- **Related products** from sections like *Referenztitel*, *Produkte derselben Reihe*, *Ähnliche Produkte*,
  *Weitere Ausgabearten*: related EAN, title, author, publisher, price, release date, availability.
- **Series**: e.g. "The Scouring, Band 1" → series + volume.
- **Preview catalogues** (*Enthalten in Vorschauen*): name, date, number of products.
- **Specials** (*Enthalten in Specials*, e.g. "Lieblingsbuch der Unabhängigen 2026 | Nominierungen",
  magazine features): saved as data - strong demand signals - never as the book's images.
Their covers are never mixed into the book's own images. Books already finished can be visited again
for this data with `--refresh` (images are not downloaded twice).

```bat
python -m pipeline discover-vlbtix --refresh --limit 20
python -m pipeline relations-report
```
→ `outputs\book_relations.xlsx` with tabs **Bundle candidates** (pairs of books in our list, scored:
series 3, Referenztitel 2, same author 2, Ähnliche Produkte 1, same preview 0.5; plus same publisher,
price difference, both released), **Links** (every related product, flag "in our list"), **Series**,
**Previews**, **Clusters** (groups of linked books) and **Book features** (per-EAN relation features for
the prediction model). Links to books not yet in our list become active automatically when a later
weekly list containing them is imported - just run `relations-report` again.

### 4.3c Discovery sample (feature-list creation)

Before the feature list is fixed, ChatGPT should see a balanced set of books: hits and flops, many
publishers, all seasons and price levels. `sample-books` draws it from the weekly Excel.

```cmd
python -m pipeline sample-books "Weekly Book Release - 30.11.2026.xlsx" --dry-run
python -m pipeline sample-books "Weekly Book Release - 30.11.2026.xlsx"
python -m pipeline discover-vlbtix --sample
python -m pipeline prepare-batch --sample --per-book 3 --max-images 20 --books 50 --count 20
python -m pipeline sample-report
python -m pipeline build-features
```

- Quotas per sales band (column *Type of Release (Sales Rank)*): Top 35, Good 40, Normal 40, Bad 35.
  Change with `--quota Top=30,Good=40,...` or `--size 100`. The band is used **only** for choosing.
- Books already analysed by ChatGPT count towards their band's quota.
- Spread over release quarters, max 3 books per publisher (`--max-per-publisher`), price thirds rotated.
- Seeded (`--seed 42`): same command, same list. `--replace` draws again.
- Reserve books replace chosen books that are not on VLB-TIX or have no images (automatic during
  `discover-vlbtix --sample` and `sample-report`).
- `--per-book 3` = at most 3 images per book in total, lowest image numbers (cover) first.
- `outputs\sample_books.xlsx`: Summary, Sample (role, band, quarter, VLB-TIX status, images, analysed).

### 4.4 Manual ChatGPT analysis
```bat
python -m pipeline prepare-batch --books 10
```
Follow `batches\batch_001\UPLOAD_INSTRUCTIONS.txt`:
1. New ChatGPT chat → paste `extraction_prompt.txt` → attach all files from `images\`.
2. Save ChatGPT's JSON answer as `chatgpt_results\batch_001.json` (UTF-8).
3. Import:
```bat
python -m pipeline import-results chatgpt_results\batch_001.json
```
The report lists accepted, rejected (wrong ISBN, wrong image ID, duplicates, invented filenames) and
**missing** images. Redo only the missing/rejected ones in ChatGPT and import as a correction —
earlier results are kept, never deleted:
```bat
python -m pipeline import-results chatgpt_results\batch_001_fix.json --batch batch_001 --correction
```
Start with 10–20 images per batch (`batch_max_images` in config) and adjust to what your ChatGPT
plan accepts and how accurate the answers are. Upload copies are downscaled to 2048 px; originals
stay untouched.

### 4.5 Category discovery
**Update mode (default once a taxonomy exists):** `export-findings` builds on the latest approved
version (or, if none, the latest imported proposal). ChatGPT gets the current categories plus the
attributes not yet placed, keeps existing categories stable, adds new ones only where needed and marks
every change (`unchanged / extended / new / renamed / merged / split`). Attach
`outputs\category_discovery\update_input.json` with `outputs\category_discovery_prompt.txt`.
The review file then shows a `change` column. Use `export-findings --fresh` to start from scratch.

**First round (fresh):**
```bat
python -m pipeline export-findings
```
Creates `outputs\all_image_findings.json` (everything) and `outputs\category_discovery\part_XX.json`
+ `part_XX_prompt.txt` (attributes with Python-computed counts and a few examples each).
Paste the prompt, attach the part file, save the answer as `outputs\proposed_taxonomy.json`.
With several parts, do each part, then use `reconciliation_prompt_template.txt` to merge them.

```bat
python -m pipeline import-taxonomy outputs\proposed_taxonomy.json
```

### 4.6 Human review
Open `outputs\category_review.csv` in Excel. For each row set `decision`:

| decision | meaning |
|---|---|
| `approve` | keep as proposed |
| `rename` | fill `final_category` / `final_subcategory` |
| `merge` | fill `final_category` / `final_subcategory` with an existing target |
| `reject` | attributes stay unmapped |
| `add` | new row: your own category + `attributes` (separated by `;`) |

You can also move attributes between rows by editing `attributes`. Then:
```bat
python -m pipeline apply-review outputs\category_review.csv
python -m pipeline export
```
Each `apply-review` creates a new taxonomy **version**; earlier versions stay in the database.

### 4.8 Features (from approved categories)

```cmd
python -m pipeline build-features
```

Turns the categories into ~65 per-book features (definitions in `src\pipeline\features.py`) and writes
`outputs\feature_dictionary.xlsx`:

- **Features**: feature, description, type, levels, from subcategory, attributes used, before release
  (yes / partly / risk), books with value, % books, % per sales band, source, aggregation.
- **Book features (wide)**: one row per EAN, one column per feature (also `model_features_wide.csv`).
- **Book features (long)**: one row per EAN x feature with timestamps: first seen (image captured),
  book images last captured, extracted at, computed at, days captured before release (also `model_features_long.csv`).
- **Subcategories without feature** and **How to read**.

Every run is stored as a snapshot (tables `feature_runs`, `book_features`), so re-running later shows when
features appear or change. Sales bands are only used for the coverage columns, never as a feature.

### 4.7 Outputs (`outputs\`)
Human-facing tables are written as formatted **Excel (.xlsx)** files; the matching `.csv` files use
`;` as separator so German Excel opens them in columns (set `"csv_delimiter": ","` in `config.json`
for English Excel or other tools). Edit the review in `outputs\category_review.xlsx` and run
`apply-review` without arguments.

| File | Content |
|---|---|
| `image_manifest.csv` | every image: ISBN, image ID, source URL/page, hash, size, rights note, status |
| `image_findings.jsonl` | one finding per line with ISBN, image ID, evidence, evidence type, category |
| `category_candidates.json` | ChatGPT proposal + counts computed from the data + gaps/conflicts |
| `image_taxonomy.json` | approved, versioned taxonomy with attribute mapping and review decisions |
| `book_image_features.csv` | one row per ISBN; counts per approved (sub)category — ready to merge |
| `processing_report.json` | progress & status counts |

The image taxonomy is standalone, so it can later be merged with the colleague's text taxonomy
via `attribute_mapping` / category keys.

---

## 5. All commands

```bat
python -m pipeline init
python -m pipeline import-books "Weekly Book Release - 30.11.2026.xlsx" [--sheet Overview] [--dry-run]
python -m pipeline import-image-urls input\image_urls.csv [--dry-run]
python -m pipeline import-local-images input\delivered_images.csv [--dry-run]
python -m pipeline collect --limit 20 [--concurrency 2] [--dry-run]
python -m pipeline retry-failed [--include-rejected]
python -m pipeline vlbtix-login [--url https://...]        (optional, disabled by default)
python -m pipeline discover-vlbtix [--limit 20] [--isbn EAN] [--dry-run] [--debug] [--retry-not-found] [--refresh]
python -m pipeline collection-report
python -m pipeline relations-report
python -m pipeline remove-demo-books
python -m pipeline prune-icons [--dry-run]
python -m pipeline prune-card-images [--dry-run]
python -m pipeline remove-images --isbn EAN --ids img_002 img_003
python -m pipeline vlbtix-logout
python -m pipeline prepare-batch --books 10 [--max-images 20] [--dry-run] [--sample] [--per-book 3] [--count 20]
python -m pipeline sample-books <weekly.xlsx> [--size 150 | --quota Top=35,...] [--seed 42] [--replace] [--dry-run]
python -m pipeline sample-report
python -m pipeline discover-vlbtix --sample
python -m pipeline release-batch batch_001
python -m pipeline import-results chatgpt_results\batch_001.json [--batch batch_001] [--correction]
python -m pipeline export-findings
python -m pipeline import-taxonomy outputs\proposed_taxonomy.json
python -m pipeline apply-review outputs\category_review.csv [--approve-pending]
python -m pipeline export
python -m pipeline status
```
Add `-v` for detailed console output. Full log: `logs\pipeline.log`.

---

## 6. Optional authenticated VLB-TIX collector

**Status: implemented but not tested against the real VLB-TIX site; no VLB-TIX endpoints or page
structure are assumed.** It only works when *both* are set in `config.json`:

```json
"vlbtix_live_enabled": true,
"vlbtix_permission_reference": "Approved by <name>, <date>, ticket/email <ref>",
"vlbtix_allowed_page_domains": ["<vlb-tix host>"],
"vlbtix_login_url": "https://<vlb-tix host>/<login page>",
"allowed_domains": ["<image host(s) used on the product pages>"]
```

Set these only after VLB-TIX automation **and** data-use permissions are confirmed in writing.

How login works — no passwords in code, config or logs:
```bat
python -m pip install playwright
python -m playwright install chromium
python -m pipeline vlbtix-login
```
1. A **visible** Chromium window opens with a dedicated profile (`.auth\vlbtix_profile`).
2. **You** log in, including MFA. The program does not type or click anything.
3. Return to CMD and press Enter. The session stays in that profile.

Later runs reuse the profile headless:
```bat
python -m pipeline discover-vlbtix --limit 20
python -m pipeline collect --limit 20
```
**Finding each book (by EAN) — VLB-TIX:** VLB-TIX encodes its "Produkte finden" search in the
`qp=` part of the address (the search term, a backlist flag and page size, compressed). The pipeline
builds that address for every EAN itself, so use:
```json
"vlbtix_search_url_template": "https://www.vlbtix.de/app/products?qp={qp}"
```
It searches current titles first and, if the book is not there, the backlist
(`vlbtix_search_backlist_fallback`). It opens the result showing that EAN (link or click) and
verifies the EAN on the book page. ASINs are not searchable in VLB-TIX; every book is searched
by its EAN (ISBN-10 ASINs were already converted to EANs during import).

Other sites: set ONE of
- `vlbtix_search_url_template` — the VLB-TIX search address with the EAN replaced by `{isbn}`
  (search once by hand, copy the address bar, replace the EAN). The collector searches each EAN,
  follows the result whose link/result box contains that EAN, and
- `vlbtix_product_url_template` — if book pages have the ISBN in their address.

Before taking any image, the collector checks that the book page **shows the same EAN/ISBN**;
otherwise the book is skipped and listed under `not_found` (never mis-assigned). Images are never
taken from the search-results page itself. ASINs are Amazon codes and are not used for VLB-TIX
(ISBN-10 ASINs are already converted to EANs during import).

**Downloading:** `discover-vlbtix` downloads the chosen images immediately inside the logged-in
session (VLB image addresses carry a short-lived `access_token`). Tokens are removed from every
address before it is stored, logged or exported. Images in another product's block
(e.g. "Referenztitel"), social-media link icons (Instagram/TikTok/… logos, see
`vlbtix_skip_icon_names`) and thumbnails smaller than `vlbtix_min_image_edge` (200 px) are skipped.
`python -m pipeline prune-icons` removes icons saved by an earlier version (moved to `data\_removed`).
Finished books are skipped on the next run; books with failed downloads are retried automatically;
`--retry-not-found` re-checks books that were not found.

Check what was saved:
```bat
python -m pipeline collection-report
```
→ `outputs\collection_report.xlsx` with tabs **Summary**, **Books** (one row per book: VLB-TIX status,
images saved/failed/rejected, book page), **Images** (one row per saved image: file, size, page
section, source URL without token) and **Problems** (failed/rejected downloads).
Images are in `data\images\<EAN>\`.

`discover-vlbtix` opens each book's page (from `source_ref`, or the templates above), collects generic image references, keeps
only allow-listed image hosts, and registers them. `collect` downloads them with the same security
checks, using the session cookies only for allow-listed hosts.

If the session expires (login page or password field detected) or an access-denied/CAPTCHA page
appears, collection **stops** and tells you to run `vlbtix-login` again. It never fills login
forms, bypasses MFA/CAPTCHAs, or retries around blocks. Pages are visited at most every 3 s.

The profile folder contains session cookies — treat it like a password. It is git-ignored; do not
copy or share it. Delete it with `python -m pipeline vlbtix-logout` when the project ends.
Override the location with `set VLBTIX_PROFILE_DIR=C:\secure\vlbtix_profile`.

---

## 7. Security & compliance

Implemented:
- HTTPS only; domain allow-list (empty by default); no IP-literal, localhost, `.internal` or
  metadata hosts; every DNS result must be public (blocks 10.x, 192.168.x, 169.254.169.254, IPv6
  private ranges); every redirect hop is re-checked; max 3 redirects; standard ports only.
- Download size limit (header + streamed byte count), content-type check, timeouts, per-host rate
  limit, retries with backoff and `Retry-After`, configurable concurrency (max 16).
- Pillow validation: format allow-list (JPEG/PNG/WEBP/GIF), min/max dimensions, decompression-bomb
  guard, truncated/corrupt detection. Downloaded content is never executed.
- File names are generated (`img_001.jpg`), never taken from the web; path-traversal checks;
  Windows reserved names blocked.
- Raw ChatGPT outputs archived read-only and stored in SQLite; corrections supersede, never delete.
- CSV exports neutralise Excel formula injection (`=`, `+`, `-`, `@`).
- Prompts tell ChatGPT to treat text in images as data, not instructions; no face identification;
  no sensitive-attribute inference; facts vs. interpretations separated.
- Secrets never in config; log filter masks `token=`, `password=` etc.; `.gitignore` excludes
  `.auth/`, `.env`, images, DB, batches, results and outputs.

Your responsibilities before real data:
- [ ] Confirm licensing/usage rights for downloading and keeping VLB-TIX/publisher images.
- [ ] Confirm VLB-TIX permits automated access with your account (if using section 6).
- [ ] IT/legal approval to upload licensed images to ChatGPT; use the company-approved workspace
      with training on your data disabled.
- [ ] Define retention: e.g. delete `data\images` and `batches\` after the project; keep only
      `outputs\` and the DB if allowed.
- [ ] Keep the project folder in a location only you (or the team) can access.

---

## 8. Scaling notes (towards 100,000 books)

- Already in place: incremental imports (100k ISBNs import in a few seconds — tested), streaming
  downloads to disk, persistent per-item status, resumable stages, dedupe, rate limits, retries.
- Manual ChatGPT upload does **not** scale to 100k books (~5–10k batches). The next step is an
  automated analyzer implementing `pipeline.analysis.ImageAnalyzer` that writes the same JSON that
  `import-results` already validates.
- Swap points: `storage.LocalStorage` → S3-compatible class; `db.connect` → PostgreSQL;
  `collector.collect` → worker queue. Not built in the MVP.

## 9. Folder layout
```
vlb.tix\
  src\pipeline\        code (cli, books, collector, images, security, storage, batches,
                       results, taxonomy, export, sources\, analysis\)
  prompts\             image_extraction.txt, category_discovery.txt, category_reconciliation.txt
  input\               books.csv, image_urls.csv (templates)
  data\                pipeline.db, images\<ISBN>\..., image_manifest.csv   (git-ignored)
  batches\             batch_001\ ...                                       (git-ignored)
  chatgpt_results\     your saved JSON + _archive\ + _reports\              (git-ignored)
  outputs\             final files                                          (git-ignored)
  tests\               pytest suite (synthetic, offline)
  tools\               make_demo_data.py
```
