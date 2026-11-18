"""Command-line interface: python -m pipeline <command> [options]  (run from the project folder)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as dbm
from .config import load_settings
from .util import log, setup_logging


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _file(p: str) -> Path:
    path = Path(p)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    return path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m pipeline", description="VLB-TIX image collection & ChatGPT discovery pipeline")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--root", default=".", help="project folder (default: current folder)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("init", help="create folders and the SQLite database")
    p = sp.add_parser("import-books", help="import/merge books from CSV or Excel (.xlsx)")
    p.add_argument("csv", metavar="file"); p.add_argument("--sheet", help="Excel sheet name (default: first sheet)")
    p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("import-image-urls", help="register approved image URLs (CSV)"); p.add_argument("csv"); p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("import-local-images", help="ingest image files delivered by an authorized party"); p.add_argument("csv"); p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("collect", help="download pending image URLs")
    p.add_argument("--limit", type=int, help="max number of books"); p.add_argument("--concurrency", type=int)
    p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("retry-failed", help="set failed sources back to pending"); p.add_argument("--include-rejected", action="store_true")
    p = sp.add_parser("vlbtix-login", help="[optional, disabled by default] manual login in a visible browser"); p.add_argument("--url")
    sp.add_parser("vlbtix-logout", help="delete the saved VLB-TIX browser profile")
    p = sp.add_parser("discover-vlbtix", help="[optional, disabled by default] find image URLs on authorized VLB-TIX pages")
    p.add_argument("--limit", type=int); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--isbn", action="append", help="only this EAN/ISBN (repeatable)")
    p.add_argument("--debug", action="store_true", help="save screenshots + page text to logs\\vlbtix_debug")
    p.add_argument("--retry-not-found", action="store_true", help="check books again that were not found before")
    p.add_argument("--refresh", action="store_true",
                   help="visit already-finished books again (e.g. to read related products); images are not re-saved")
    p.add_argument("--sample", nargs="?", const="discovery", metavar="NAME",
                   help="only the chosen books of the discovery sample; failed books are replaced by reserves")
    sp.add_parser("relations-report", help="links between books, series, previews and bundle candidates (Excel)")
    sp.add_parser("collection-report", help="per-book overview: found on VLB-TIX? how many images saved?")
    p = sp.add_parser("prepare-batch", help="copy images into a batch folder for manual ChatGPT upload")
    p.add_argument("--books", type=int, default=10); p.add_argument("--max-images", type=int); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sample", nargs="?", const="discovery", metavar="NAME",
                   help="only books of the discovery sample (default name: discovery)")
    p.add_argument("--per-book", type=int, metavar="N", help="at most N images per book in total (cover first)")
    p.add_argument("--count", type=int, default=1, metavar="N", help="create up to N batches in one go")
    p = sp.add_parser("sample-books", help="draw a stratified sample of books for feature discovery (from the weekly Excel)")
    p.add_argument("file", help="the weekly book list (.xlsx) with the column 'Type of Release (Sales Rank)'")
    p.add_argument("--sheet", help="Excel sheet name (default: first sheet)")
    p.add_argument("--size", type=int, help="total sample size (default 150; default quotas are scaled)")
    p.add_argument("--quota", help="explicit quotas, e.g. Top=35,Good=40,Normal=40,Bad=35")
    p.add_argument("--seed", type=int, default=42, help="random seed - same seed, same sample")
    p.add_argument("--max-per-publisher", type=int, default=3)
    p.add_argument("--reserve-share", type=float, default=0.25, help="reserve books per stratum (share of chosen)")
    p.add_argument("--name", default="discovery"); p.add_argument("--replace", action="store_true",
                                                                  help="draw an existing sample again")
    p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("sample-report", help="progress of the discovery sample (Excel) - also swaps in reserves")
    p.add_argument("--name", default="discovery")
    p = sp.add_parser("release-batch", help="return un-analyzed images of a batch to the pool"); p.add_argument("batch_id")
    p = sp.add_parser("rebuild-batch", help="recreate a batch folder deleted by mistake (same images, same ID)")
    p.add_argument("batch_id")
    sp.add_parser("refresh-prompts", help="update prompts of batches not sent yet with the latest attribute names")
    p = sp.add_parser("reopen-batch", help="undo release-batch so a batch's ChatGPT answer can be imported")
    p.add_argument("batch_id")
    p = sp.add_parser("import-results", help="import ChatGPT JSON results for a batch")
    p.add_argument("json_file"); p.add_argument("--batch"); p.add_argument("--correction", action="store_true",
                                                                           help="supersede earlier results (old ones are kept)")
    p = sp.add_parser("export-findings", help="export findings + category-discovery prompt")
    p.add_argument("--attributes-per-part", type=int, default=150); p.add_argument("--examples-per-attribute", type=int, default=8)
    p.add_argument("--fresh", action="store_true", help="derive categories from scratch instead of updating the current ones")
    p = sp.add_parser("import-taxonomy", help="import ChatGPT taxonomy proposal, create review CSV"); p.add_argument("json_file")
    p = sp.add_parser("apply-review", help="apply the edited review CSV -> new taxonomy version")
    p.add_argument("csv", nargs="?", default="outputs/category_review.xlsx", metavar="file",
                   help="the review file you edited (.xlsx or .csv)")
    p.add_argument("--approve-pending", action="store_true", help="treat rows without a decision as 'approve'")
    sp.add_parser("export", help="write all final outputs")
    sp.add_parser("build-features", help="turn approved categories into per-book features (feature_dictionary.xlsx, "
                                         "model_features_*.csv) with timestamps")
    p = sp.add_parser("remove-images", help="take specific saved images out (moved to data\\_removed)")
    p.add_argument("--isbn", required=True); p.add_argument("--ids", nargs="+", required=True, metavar="img_00X")
    p = sp.add_parser("prune-card-images", help="remove saved listing thumbnails (magazines, previews, awards)")
    p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("prune-icons", help="remove saved social-media logo images (Instagram/TikTok icons)")
    p.add_argument("--dry-run", action="store_true")
    sp.add_parser("remove-demo-books", help="delete the synthetic demo books (titles starting with SYNTHETIC)")
    sp.add_parser("status", help="show progress")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(Path(args.root))
    setup_logging(settings.path(settings.log_dir), args.verbose)
    from . import util
    util.CSV_DELIMITER = settings.csv_delimiter
    for d in (settings.images_dir, settings.batches_dir, settings.results_dir, settings.outputs_dir, "input"):
        settings.path(d).mkdir(parents=True, exist_ok=True)
    conn = dbm.connect(settings.db_path)
    dbm.init_db(conn)
    try:
        result = dispatch(args, settings, conn)
        if result is not None:
            _print(result)
        dbm.log_event(conn, args.cmd, json.dumps(result, default=str)[:2000] if result else "")
        return 0
    except Exception as e:  # concise message for users; full trace in logs/pipeline.log
        log.debug("command failed", exc_info=True)
        log.error("%s: %s", type(e).__name__, e)
        return 1
    finally:
        conn.close()


def _discover_sample(args, settings, conn):
    """Discover the chosen sample books; books not on VLB-TIX are swapped for reserves, which are then
    looked up too (a few rounds at most)."""
    from .sampling import books_to_discover, refresh_sample, sample_report
    from .sources.vlbtix_playwright import VlbTixPlaywrightSource
    src = VlbTixPlaywrightSource(settings, debug=args.debug)
    total = {"rounds": 0, "books_checked": 0, "found": 0, "images_saved": 0, "not_found": [], "replaced": []}
    for _ in range(4):
        refresh = refresh_sample(conn, args.sample)
        total["replaced"] += refresh["promoted"]
        todo = books_to_discover(conn, args.sample)
        if not todo:
            break
        if args.limit:
            todo = todo[:args.limit]
        rep = src.discover(conn, None, args.dry_run, todo, False, False)
        total["rounds"] += 1
        total["books_checked"] += len(todo)
        for k in ("found", "images_saved"):
            total[k] += rep.get(k, 0)
        total["not_found"] += rep.get("not_found", [])
        if args.dry_run or args.limit:
            break
    return total | {"progress": sample_report(conn, settings, args.sample)}


def dispatch(args, settings, conn):
    c = args.cmd
    if c == "init":
        return {"database": str(settings.db_path), "allowed_domains": settings.allowed_domains,
                "vlbtix_live_enabled": settings.vlbtix_live_enabled}
    if c == "import-books":
        from .books import import_books
        from .util import write_csv
        rep = import_books(conn, _file(args.csv), args.dry_run, args.sheet)
        out = {"summary": rep.summary(), "skipped_by_reason": rep.invalid_by_reason()}
        if rep.invalid:
            f = settings.path(settings.outputs_dir) / "import_books_skipped.csv"
            write_csv(f, ["line", "ean", "asin", "reason"], rep.invalid)
            out["skipped_rows_file"] = str(f)
        return out
    if c == "import-image-urls":
        from .sources.url_manifest import import_url_manifest
        return import_url_manifest(conn, _file(args.csv), args.dry_run)
    if c == "import-local-images":
        from .collector import import_local_images
        return import_local_images(conn, settings, _file(args.csv), args.dry_run)
    if c == "collect":
        from .collector import collect
        return collect(conn, settings, args.limit, args.dry_run, args.concurrency)
    if c == "retry-failed":
        from .collector import reset_failed
        return {"reset_to_pending": reset_failed(conn, args.include_rejected)}
    if c == "vlbtix-login":
        from .sources.vlbtix_playwright import interactive_login
        return {"profile": str(interactive_login(settings, args.url))}
    if c == "vlbtix-logout":
        from .sources.vlbtix_playwright import logout
        return {"profile_deleted": logout(settings)}
    if c == "discover-vlbtix" and args.sample:
        return _discover_sample(args, settings, conn)
    if c == "discover-vlbtix":
        from .sources.vlbtix_playwright import VlbTixPlaywrightSource
        from .isbn import to_isbn13
        isbns = [to_isbn13(x) or x for x in (args.isbn or [])] or None
        rep = VlbTixPlaywrightSource(settings, debug=args.debug).discover(conn, args.limit, args.dry_run, isbns,
                                                                          args.retry_not_found, args.refresh)
        if len(rep["images"]) > 30:   # keep the console readable on big runs; full list via collection-report
            rep["images"] = rep["images"][:30] + [f"... {len(rep['images']) - 30} more (see collection-report)"]
        if len(rep["relations"]) > 10:
            rep["relations"] = rep["relations"][:10] + [f"... {len(rep['relations']) - 10} more (see relations-report)"]
        return rep
    if c == "relations-report":
        from .relations import relations_report
        return relations_report(conn, settings)
    if c == "collection-report":
        from .export import collection_report
        return collection_report(conn, settings)
    if c == "prepare-batch":
        from .batches import prepare_batch
        only = None
        if args.sample:
            from .sampling import sample_isbns
            only = sample_isbns(conn, args.sample)
        reps = []
        for _ in range(max(1, args.count)):
            rep = prepare_batch(conn, settings, args.books, args.max_images, args.dry_run, only, args.per_book)
            reps.append(rep)
            if rep.get("batch_id") is None or args.dry_run:
                break
        return reps[0] if len(reps) == 1 else {"batches": [r for r in reps if r.get("batch_id")],
                                               "images": sum(r.get("images", 0) for r in reps),
                                               "note": reps[-1].get("message", "")}
    if c == "sample-books":
        from .sampling import build_sample, parse_quotas
        return build_sample(conn, settings, _file(args.file), args.sheet, args.name,
                            parse_quotas(args.quota, args.size), args.seed, args.max_per_publisher,
                            args.reserve_share, args.replace, args.dry_run)
    if c == "sample-report":
        from .sampling import sample_report
        return sample_report(conn, settings, args.name)
    if c == "release-batch":
        from .batches import release_batch
        return {"released_images": release_batch(conn, args.batch_id)}
    if c == "rebuild-batch":
        from .batches import rebuild_batch
        return rebuild_batch(conn, settings, args.batch_id)
    if c == "refresh-prompts":
        from .batches import refresh_prompts
        return {"prompts_updated": refresh_prompts(conn, settings)}
    if c == "reopen-batch":
        from .batches import reopen_batch
        return reopen_batch(conn, args.batch_id)
    if c == "import-results":
        from .results import import_results
        rep = import_results(conn, settings, _file(args.json_file), args.batch, args.correction)
        return {k: v for k, v in rep.items() if k != "issues"} | {"issue_count": len(rep["issues"]),
                                                                  "issues": rep["issues"][:15]}
    if c == "export-findings":
        from .taxonomy import export_findings
        return export_findings(conn, settings, args.attributes_per_part, args.examples_per_attribute, args.fresh)
    if c == "import-taxonomy":
        from .taxonomy import import_taxonomy
        return import_taxonomy(conn, settings, _file(args.json_file))
    if c == "apply-review":
        from .taxonomy import apply_review
        return apply_review(conn, settings, _file(args.csv), args.approve_pending)
    if c == "build-features":
        from .features import build_features
        return build_features(conn, settings)
    if c == "export":
        from .export import export_all
        return export_all(conn, settings)
    if c == "remove-images":
        from .collector import remove_images
        from .isbn import to_isbn13
        return remove_images(conn, settings, to_isbn13(args.isbn) or args.isbn, args.ids)
    if c == "prune-card-images":
        from .collector import prune_card_thumbnails
        return prune_card_thumbnails(conn, settings, args.dry_run)
    if c == "prune-icons":
        from .collector import prune_icon_images
        return prune_icon_images(conn, settings, args.dry_run)
    if c == "remove-demo-books":
        from .books import remove_demo_books
        return remove_demo_books(conn)
    if c == "status":
        from .export import status
        return status(conn)
    raise SystemExit(f"unknown command {c}")


if __name__ == "__main__":
    sys.exit(main())
