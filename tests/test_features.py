"""Feature dictionary: rules, timestamps, snapshot storage, Excel output."""
import json

import openpyxl

from conftest import ISBN_A, ISBN_B, ISBN_C
from pipeline.batches import prepare_batch
from pipeline.features import FEATURES, build_features, parse_quantity
from pipeline.results import import_results
from test_batches_results_taxonomy import rec, results_file, with_images  # noqa: F401  (fixture)


def test_parse_quantity():
    assert parse_quantity("über 5,5 Mio. verkaufte Bücher") == 5_500_000
    assert parse_quantity("415.3k") == 415_300
    assert parse_quantity("325.000 Follower") == 325_000
    assert parse_quantity("1.2 million copies") == 1_200_000
    assert parse_quantity("44400") == 44_400
    assert parse_quantity("keine Zahl") is None


def test_feature_names_unique_and_complete():
    names = [f.name for f in FEATURES]
    assert len(names) == len(set(names))
    for f in FEATURES:
        assert f.description and f.subcategory and f.before_release in ("yes", "partly", "risk")


def test_build_features_values_timestamps_and_snapshots(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=3)
    F = lambda a, v, e: {"attribute": a, "value": v, "evidence": e, "evidence_type": "visible_text"}
    import_results(conn, s, results_file(tmp_path, "b.json", [
        rec(ISBN_A, "img_001", findings=[F("marketing_priority_signal", "Spitzentitel", "SPITZENTITEL"),
                                         F("bestseller_label", "SPIEGEL Bestseller Platz 1", "Platz 1")]),
        rec(ISBN_A, "img_002", findings=[F("social_media_follower_count", "325.000 Follower", "325.000 Follower"),
                                         F("marketing_campaign_label", "Große Marketingkampagne", "Große Marketingkampagne")]),
        rec(ISBN_B, "img_001", findings=[F("marketing_priority_signal", "Werbeschwerpunkt", "Werbeschwerpunkt")]),
        rec(ISBN_C, "img_001", findings=[F("genre_label", "Roman", "Roman")]),
    ]))
    rep = build_features(conn, s)
    assert rep["books"] == 3 and rep["features"] == len(FEATURES)
    val = lambda isbn, f: json.loads(conn.execute(
        "SELECT value FROM book_features WHERE isbn13=? AND feature=? AND run_id=?", (isbn, f, rep["run_id"])).fetchone()[0])
    assert val(ISBN_A, "priority_level") == "lead_title"
    assert val(ISBN_B, "priority_level") == "focus_title"
    assert val(ISBN_C, "priority_level") == "none"
    assert val(ISBN_A, "campaign_size") == "big_campaign"
    assert val(ISBN_A, "bestseller_rank1") is True
    assert val(ISBN_A, "max_social_followers") == 325_000
    assert val(ISBN_C, "printed_genre_label") == "roman"
    row = conn.execute("SELECT first_seen_at, extracted_at, evidence_images FROM book_features "
                       "WHERE isbn13=? AND feature='max_social_followers'", (ISBN_A,)).fetchone()
    assert row["first_seen_at"] and row["extracted_at"] and row["evidence_images"] == "img_002"
    rep2 = build_features(conn, s)                       # second run = new snapshot, old one kept
    assert rep2["run_id"] == rep["run_id"] + 1
    assert conn.execute("SELECT COUNT(DISTINCT run_id) FROM book_features").fetchone()[0] == 2
    wb = openpyxl.load_workbook(s.path(s.outputs_dir) / "feature_dictionary.xlsx", read_only=True)
    assert wb.sheetnames[:3] == ["Features", "Book features (wide)", "Book features (long)"]
    head = next(wb["Features"].iter_rows(values_only=True))
    assert {"Feature", "Description", "% books", "Before release", "From subcategory"} <= set(head)
