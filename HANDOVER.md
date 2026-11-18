# Handover for teammates (state: 18.11.2026)

This repository contains the **image** part of the VLB-TIX feature project. Text extraction (blurbs, descriptions,
metadata text) is a separate part that should plug into the same data model, so the feature dictionary can combine
both. Full command reference: `README.md`.

## What is NOT in git (and why)

| Not in git | Reason | How to get it |
|---|---|---|
| `data/` (SQLite DB, downloaded images) | licensed VLB-TIX images, large | shared drive copy of `data/pipeline.db` (images only if needed and approved) |
| `outputs/`, `batches/`, `chatgpt_results/` | generated files / extraction results | shared drive (Excel reports) |
| `*.xlsx` (e.g. Weekly Book Release) | internal order and forecast data | ask the project owner |
| `config.json` | local settings incl. permission reference | copy `config.example.json` -> `config.json` |
| `.auth/` | personal VLB-TIX browser session | never share - log in yourself (`vlbtix-login`) |

## Setup (Windows CMD)

```cmd
git clone <repo-url> vlb.tix
cd vlb.tix
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
copy config.example.json config.json
python -m pytest -q
```

Put the shared `pipeline.db` into `data\` to work with the real books, findings and features.

## How the image part works

```
Weekly Excel -> import-books -> books
sample-books (stratified 500) -> discover-vlbtix --sample -> images (+ VLB-TIX relations, series, previews)
prepare-batch -> LLM extraction (ChatGPT / Claude) -> import-results -> extractions + findings
export-findings / import-taxonomy / apply-review -> categories (taxonomy v3.6: 15 categories, 59 subcategories)
build-features -> feature_dictionary.xlsx + model_features_*.csv (65 features per book, with timestamps)
```

Key tables (`src/pipeline/db.py`):

- `books` (isbn13 = EAN is the key everywhere), `book_vlbtix_info`, `product_relations`, `preview_memberships`
- `images`, `extractions` (one per image and import), `findings` (isbn13, image_id, attribute, value, evidence, evidence_type)
- `taxonomy_versions`, `attribute_mappings` (attribute -> category/subcategory)
- `feature_runs`, `book_features` (value + first_seen_at / observed_at / extracted_at / computed_at per book and feature)
- `sample_books` (the discovery sample; sales band used only for sampling and coverage reporting)

## Where text extraction can plug in (suggestion)

1. **Store text findings in the same shape** as image findings: `isbn13, source, attribute, value, evidence,
   evidence_type`. Suggested new tables `text_sources` (where the text came from, when it was captured) and
   `text_findings`, instead of forcing them into `findings` (which needs an image_id).
2. **Reuse the attribute vocabulary** where meanings overlap (e.g. `bestseller_label`, `award_name`,
   `review_quote`, `genre_label`, `target_audience_signal`, `series_label`) - see `outputs/category_list_v3.6.xlsx`
   (tab "Attribute map") or `outputs/proposed_taxonomy_v3.json`.
3. **Features**: each feature in `src/pipeline/features.py` lists the attributes it reads. Text findings can feed the
   same features (e.g. `priority_level`, `is_debut`, `has_media_tie_in`) or new text-only features. Keep the same
   rules: per-book value, `before_release` rating, timestamps of when the source was captured.
4. **Before release**: record when the text was captured and, if available, when it was published - this is what
   makes a feature usable for prediction.
5. Add tests next to the existing ones (`tests/`), no network or paid API calls in tests.

## Conventions

- Python 3.12, standard library + httpx, Pillow, openpyxl (Playwright only for the optional VLB-TIX collector).
- No secrets in code or git; secrets only via environment variables. Never print tokens in logs.
- CSV files use `;`. Excel output via `util.write_xlsx` (formatted, no formulas).
- Work on a branch (`feature/text-extraction`) and open a pull request into `main`.
