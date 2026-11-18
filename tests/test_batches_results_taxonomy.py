import csv
import json

import pytest

from conftest import ISBN_A, ISBN_B, ISBN_C, image_bytes, write
from pipeline.batches import prepare_batch, release_batch
from pipeline.books import import_books
from pipeline.collector import import_local_images
from pipeline.export import export_all, status
from pipeline.results import ResultImportError, import_results
from pipeline.taxonomy import TaxonomyError, apply_review, export_findings, import_taxonomy


@pytest.fixture
def with_images(project, tmp_path):
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13,title\n{ISBN_A},A\n{ISBN_B},B\n{ISBN_C},C\n"))
    (tmp_path / "i").mkdir()
    rows = []
    for n, isbn in enumerate([ISBN_A, ISBN_A, ISBN_B, ISBN_C]):
        (tmp_path / "i" / f"{n}.png").write_bytes(image_bytes((n * 40, 10, 10), size=(3000, 1000)))
        rows.append(f"{isbn},i/{n}.png")
    import_local_images(conn, s, write(tmp_path / "m.csv", "isbn13,file_path\n" + "\n".join(rows) + "\n"))
    return s, conn


def rec(isbn, image_id, findings=None, **kw):
    return {"isbn": isbn, "image_id": image_id, "image_type": "front_cover", "visible_text": ["Titel"],
            "findings": findings if findings is not None else [
                {"attribute": "Bestseller Label", "value": "SPIEGEL", "evidence": "SPIEGEL-Bestseller",
                 "evidence_type": "visible_text"}], "needs_review": False, **kw}


def results_file(tmp_path, name, records, batch_id="batch_001", fenced=False):
    text = json.dumps({"batch_id": batch_id, "records": records}, ensure_ascii=False)
    return write(tmp_path / name, f"Here you go:\n```json\n{text}\n```" if fenced else text)


def test_prepare_batch_structure_and_resizing(with_images):
    s, conn = with_images
    rep = prepare_batch(conn, s, books=2)
    assert rep["batch_id"] == "batch_001" and rep["images"] == 3 and rep["books"] == 2
    bdir = s.path("batches/batch_001")
    files = sorted(p.name for p in (bdir / "images").iterdir())
    assert files == [f"{ISBN_A}_img_001.jpg", f"{ISBN_A}_img_002.jpg", f"{ISBN_B}_img_001.jpg"]  # resized -> jpg
    prompt = (bdir / "extraction_prompt.txt").read_text(encoding="utf-8")
    assert f"{ISBN_A}_img_002.jpg  ->  isbn: {ISBN_A}, image_id: img_002" in prompt and "{{" not in prompt
    assert (bdir / "UPLOAD_INSTRUCTIONS.txt").exists()
    with (bdir / "manifest.csv").open(encoding="utf-8-sig") as fh:
        assert len(list(csv.DictReader(fh, delimiter=";"))) == 3
    # originals untouched
    assert conn.execute("SELECT width FROM images WHERE image_key=?", (f"{ISBN_A}_img_001",)).fetchone()[0] == 3000
    assert prepare_batch(conn, s, books=10)["images"] == 1   # only the remaining book
    assert prepare_batch(conn, s, books=10)["batch_id"] is None


def test_batch_image_cap_and_release(with_images):
    s, conn = with_images
    rep = prepare_batch(conn, s, books=10, max_images=1)
    assert rep["images"] == 1
    assert release_batch(conn, "batch_001") == 1


def test_import_results_happy_path_and_duplicate_file(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=2)
    f = results_file(tmp_path, "batch_001.json", [rec(ISBN_A, "img_001"), rec(ISBN_A, "img_002"),
                                                  rec(ISBN_B, "img_001", needs_review=True)], fenced=True)
    rep = import_results(conn, s, f)
    assert (rep["accepted"], rep["rejected"], rep["missing"]) == (3, 0, [])
    assert conn.execute("SELECT attribute FROM findings LIMIT 1").fetchone()[0] == "bestseller_label"
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "imported"
    assert conn.execute("SELECT analysis_status FROM images WHERE image_key=?",
                        (f"{ISBN_B}_img_001",)).fetchone()[0] == "needs_review"
    archived = list(s.path("chatgpt_results/_archive").iterdir())
    assert len(archived) == 1 and archived[0].read_bytes() == f.read_bytes()
    with pytest.raises(ResultImportError, match="already imported"):
        import_results(conn, s, f)


def test_import_results_mismatch_missing_and_invalid(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=2)
    f = results_file(tmp_path, "batch_001.json", [
        rec(ISBN_A, "img_001"),
        rec(ISBN_A, "img_001"),             # duplicate in file
        rec(ISBN_B, "img_002"),             # image_id not in batch for that ISBN
        rec(ISBN_C, "img_001"),             # ISBN not in batch
        rec("9780000000001", "img_001"),    # invalid ISBN
        rec(ISBN_A, "cover.jpg"),           # invented filename instead of image_id
        rec(ISBN_A, "img_002", findings=[{"value": "x"}, {"attribute": "mood", "value": "dark",
                                                            "evidence": "x", "evidence_type": "guess"}]),
    ])
    rep = import_results(conn, s, f)
    assert rep["accepted"] == 2 and rep["rejected"] == 5
    assert rep["missing"] == [f"{ISBN_B}_img_001"]
    issues = " ".join(i["issue"] for i in rep["issues"])
    assert "ISBN/image mix-up" in issues and "not part of this batch" in issues and "duplicate" in issues
    assert "unknown evidence_type" in issues and "no attribute" in issues
    assert conn.execute("SELECT needs_review FROM extractions WHERE image_id='img_002'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "partially_imported"


def test_correction_supersedes_but_keeps_original(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=1)
    import_results(conn, s, results_file(tmp_path, "batch_001.json", [rec(ISBN_A, "img_001")]))
    fix = results_file(tmp_path, "fix.json", [rec(ISBN_A, "img_001", findings=[
        {"attribute": "award", "value": "X", "evidence": "Preis", "evidence_type": "visible_text"}])])
    rep = import_results(conn, s, fix)                       # without --correction: rejected
    assert rep["accepted"] == 0 and "already imported" in rep["issues"][0]["issue"]
    fix2 = write(tmp_path / "fix2.json", fix.read_text(encoding="utf-8") + " ")
    rep = import_results(conn, s, fix2, correction=True)
    assert rep["accepted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM extractions WHERE is_current=1").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM result_imports").fetchone()[0] == 3  # all evidence retained


def test_bad_json_and_wrong_batch(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=1)
    with pytest.raises(ResultImportError, match="invalid JSON"):
        import_results(conn, s, write(tmp_path / "batch_001.json", '{"records": [ {"isbn": ] }'))
    with pytest.raises(ResultImportError, match="batch_id"):
        import_results(conn, s, results_file(tmp_path, "batch_001b.json", [], batch_id="batch_009"), batch_id="batch_001")
    with pytest.raises(ResultImportError, match="unknown batch"):
        import_results(conn, s, results_file(tmp_path, "x.json", [], batch_id="batch_042"))


def _analyzed(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=3)
    F = lambda a, v, e: {"attribute": a, "value": v, "evidence": e, "evidence_type": "visible_text"}
    import_results(conn, s, results_file(tmp_path, "batch_001.json", [
        rec(ISBN_A, "img_001", findings=[F("bestseller_label", "SPIEGEL", "SPIEGEL-Bestseller"),
                                         F("author_event", "reading", "Lesung in Berlin")]),
        rec(ISBN_A, "img_002", findings=[F("bestseller_badge", "#1", "Platz 1"), F("series_label", "Band 2", "Band 2")]),
        rec(ISBN_B, "img_001", findings=[F("author_event", "signing", "Signierstunde"), F("odd_thing", "x", "x")]),
        rec(ISBN_C, "img_001", findings=[F("=cmd|' /C calc'!A0", "x", "=1+1")]),
    ]))
    return s, conn


PROPOSAL = {
    "categories": [
        {"name": "Marketing & Promotion", "description": "Sales claims", "attributes": [],
         "subcategories": [{"name": "Bestseller", "attributes": ["bestseller_label", "bestseller_badge"],
                            "examples": ["SPIEGEL-Bestseller"]}]},
        {"name": "Author Events", "attributes": ["author_event"], "examples": ["Lesung in Berlin"]},
        {"name": "Book Series", "attributes": ["series_label", "not_in_data"]},
    ],
    "unassigned_attributes": ["odd_thing"],
}


def test_taxonomy_discovery_review_and_export(with_images, tmp_path):
    s, conn = _analyzed(with_images, tmp_path)
    rep = export_findings(conn, s, max_attributes_per_part=2)
    assert rep["parts"] == 3 and rep["findings"] == 7
    data = json.loads(s.path("outputs/all_image_findings.json").read_text(encoding="utf-8"))
    ae = next(a for a in data["attribute_summary"] if a["attribute"] == "author_event")
    assert (ae["findings_count"], ae["books_count"]) == (2, 2)   # computed in Python
    assert s.path("outputs/category_discovery_prompt.txt").exists()

    prop = write(tmp_path / "proposed_taxonomy.json", "```json\n" + json.dumps(PROPOSAL) + "\n```")
    rep = import_taxonomy(conn, s, prop)
    assert rep["unknown"] == 1 and rep["unmapped"] == 1          # not_in_data / formula-like attribute
    cands = json.loads(s.path("outputs/category_candidates.json").read_text(encoding="utf-8"))
    best = next(c for c in cands["candidates"] if c["subcategory"] == "Bestseller")
    assert (best["findings_count"], best["books_count"]) == (2, 1)

    assert s.path("outputs/category_review.xlsx").exists()
    review = s.path("outputs/category_review.csv")
    with pytest.raises(TaxonomyError, match="no decision"):
        apply_review(conn, s, review)
    rows = list(csv.DictReader(review.open(encoding="utf-8-sig"), delimiter=";"))
    for r in rows:
        if r["proposed_category"] == "Marketing & Promotion" and not r["proposed_subcategory"]:
            r["decision"] = "approve"
        elif r["proposed_subcategory"] == "Bestseller":
            r["decision"] = "rename"; r["final_category"] = "Marketing & Promotion"; r["final_subcategory"] = "Bestseller Signals"
        elif r["proposed_category"] == "Author Events":
            r["decision"] = "approve"
        elif r["proposed_category"] == "Book Series":
            r["decision"] = "merge"; r["final_category"] = "Marketing & Promotion"; r["final_subcategory"] = "Series"
        elif r["attributes"] == "odd_thing":
            r["decision"] = "reject"
    rows.append({**{k: "" for k in rows[0]}, "decision": "add", "final_category": "Awards", "attributes": "award_label"})
    with review.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    rep = apply_review(conn, s, review)
    assert rep["version"] == 1
    tax = json.loads(s.path("outputs/image_taxonomy.json").read_text(encoding="utf-8"))
    assert tax["attribute_mapping"]["bestseller_badge"] == {"category": "Marketing & Promotion",
                                                            "subcategory": "Bestseller Signals"}
    assert "odd_thing" in tax["unmapped_attributes"]
    assert {c["name"] for c in tax["categories"]} == {"Marketing & Promotion", "Author Events", "Awards"}

    out = export_all(conn, s)
    assert out["taxonomy_version"] == 1 and out["image_findings_lines"] == 7
    lines = [json.loads(l) for l in s.path("outputs/image_findings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(l["isbn"] and l["evidence"] is not None for l in lines)
    feats = {r["isbn13"]: r for r in csv.DictReader(s.path("outputs/book_image_features.csv").open(encoding="utf-8-sig"), delimiter=";")}
    assert feats[ISBN_A]["cat__marketing_promotion"] == "3" and feats[ISBN_A]["cat__author_events"] == "1"
    assert feats[ISBN_C]["uncategorized_findings"] == "1"
    raw = s.path("outputs/image_manifest.csv").read_text(encoding="utf-8-sig")
    assert raw.count("\n") == 5
    jsonl = s.path("outputs/image_findings.jsonl").read_text(encoding="utf-8")
    assert "=1+1" in jsonl                       # JSON keeps raw evidence
    assert conn.execute("SELECT attribute FROM findings WHERE evidence='=1+1'").fetchone()[0] == "cmd_c_calc_a0"
    report = json.loads(s.path("outputs/processing_report.json").read_text(encoding="utf-8"))
    assert report["images"] == 4 and report["taxonomy_version"] == 1
    assert status(conn)["current_extractions"] == 4


def test_review_conflicting_mapping_rejected(with_images, tmp_path):
    s, conn = _analyzed(with_images, tmp_path)
    import_taxonomy(conn, s, write(tmp_path / "p.json", json.dumps(PROPOSAL)))
    p = write(tmp_path / "r.csv", "proposed_category,decision,final_category,attributes\n"
                                  "A,approve,,author_event\nB,approve,,author_event\n")
    with pytest.raises(TaxonomyError, match="mapped to both"):
        apply_review(conn, s, p)


def test_known_attributes_in_next_prompt_and_cancelled_batch(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=1)
    first = s.path("batches/batch_001/extraction_prompt.txt").read_text(encoding="utf-8")
    assert "(none yet" in first and "{{" not in first
    import_results(conn, s, results_file(tmp_path, "batch_001.json", [rec(ISBN_A, "img_001"), rec(ISBN_A, "img_002")]))
    prepare_batch(conn, s, books=1)
    second = s.path("batches/batch_002/extraction_prompt.txt").read_text(encoding="utf-8")
    assert "bestseller_label" in second.split("ALREADY IN USE")[1].split("=== RULES")[0]
    assert release_batch(conn, "batch_002") == 1
    with pytest.raises(ResultImportError, match="cancelled"):
        import_results(conn, s, results_file(tmp_path, "batch_002.json", [rec(ISBN_B, "img_001")], batch_id="batch_002"))


def test_reopen_cancelled_batch_then_import(with_images, tmp_path):
    from pipeline.batches import reopen_batch
    s, conn = with_images
    prepare_batch(conn, s, books=1)                     # ISBN_A img_001 + img_002
    release_batch(conn, "batch_001")                     # released too early
    with pytest.raises(ResultImportError, match="reopen-batch"):
        import_results(conn, s, results_file(tmp_path, "batch_001.json", [rec(ISBN_A, "img_001")]))
    assert reopen_batch(conn, "batch_001")["images_back_in_batch"] == 2
    rep = import_results(conn, s, results_file(tmp_path, "batch_001b.json", [rec(ISBN_A, "img_001")]),
                         batch_id="batch_001")
    assert rep["accepted"] == 1 and rep["missing"] == [f"{ISBN_A}_img_002"]


def test_waiting_prompts_refreshed_after_import(with_images, tmp_path):
    s, conn = with_images
    prepare_batch(conn, s, books=1)
    prepare_batch(conn, s, books=1)               # batch_002 prepared before any results exist
    p2 = s.path("batches/batch_002/extraction_prompt.txt")
    assert "(none yet" in p2.read_text(encoding="utf-8")
    rep = import_results(conn, s, results_file(tmp_path, "batch_001.json", [rec(ISBN_A, "img_001"), rec(ISBN_A, "img_002")]))
    assert rep["prompts_updated"] == ["batch_002"]
    txt = p2.read_text(encoding="utf-8")
    assert "bestseller_label" in txt and f"{ISBN_B}_img_001" in txt


def test_rebuild_deleted_batch_folder(with_images, tmp_path):
    import shutil
    from pipeline.batches import rebuild_batch
    s, conn = with_images
    prepare_batch(conn, s, books=1)
    bdir = s.path("batches/batch_001")
    before = sorted(p.name for p in (bdir / "images").iterdir())
    shutil.rmtree(bdir)
    rep = rebuild_batch(conn, s, "batch_001")
    assert rep["images"] == 2 and sorted(p.name for p in (bdir / "images").iterdir()) == before
    assert (bdir / "extraction_prompt.txt").exists() and (bdir / "manifest.csv").exists()
    rep = import_results(conn, s, results_file(tmp_path, "batch_001.json", [rec(ISBN_A, "img_001"), rec(ISBN_A, "img_002")]))
    assert rep["accepted"] == 2


def test_category_update_mode_builds_on_existing_taxonomy(with_images, tmp_path):
    s, conn = _analyzed(with_images, tmp_path)
    assert export_findings(conn, s)["mode"] == "fresh"                    # no taxonomy yet
    import_taxonomy(conn, s, write(tmp_path / "v1.json", json.dumps(PROPOSAL)))
    rep = export_findings(conn, s)                                        # now builds on proposal v1
    assert rep["mode"] == "update" and "proposal" in rep["based_on"]
    data = json.loads(s.path("outputs/category_discovery/update_input.json").read_text(encoding="utf-8"))
    assert data["new_attributes"] == ["cmd_c_calc_a0", "odd_thing"]       # not in any v1 category yet
    assert data["current_taxonomy"]["categories"][0]["name"] == "Marketing & Promotion"
    prompt = s.path("outputs/category_discovery_prompt.txt").read_text(encoding="utf-8")
    assert "UPDATE of an existing taxonomy" in prompt and "{{" not in prompt
    upd = {"categories": [
        {"name": "Marketing & Promotion", "status": "extended", "change_note": "Added odd signals.",
         "attributes": [], "subcategories": [
             {"name": "Bestseller", "status": "unchanged", "attributes": ["bestseller_label", "bestseller_badge"]},
             {"name": "Other signals", "status": "new", "change_note": "New subcategory",
              "attributes": ["odd_thing", "cmd_c_calc_a0"]}]},
        {"name": "Author Events", "status": "unchanged", "attributes": ["author_event"]},
        {"name": "Book Series", "status": "renamed", "previous_name": "Series", "attributes": ["series_label"]}]}
    rep = import_taxonomy(conn, s, write(tmp_path / "v2.json", json.dumps(upd)))
    assert rep["changes"]["new"] == 1 and rep["unmapped"] == 0
    rows = list(csv.DictReader(s.path("outputs/category_review.csv").open(encoding="utf-8-sig"), delimiter=";"))
    other = next(r for r in rows if r["proposed_subcategory"] == "Other signals")
    assert other["change"] == "new"
    assert "previously: Series" in next(r for r in rows if r["proposed_category"] == "Book Series")["change_note"]
    assert export_findings(conn, s, fresh=True)["mode"] == "fresh"        # escape hatch


def test_suggested_decisions_prefill_review_and_category_totals(with_images, tmp_path):
    s, conn = _analyzed(with_images, tmp_path)
    attrs = [r[0] for r in conn.execute("SELECT DISTINCT attribute FROM findings")]
    prop = {"categories": [
        {"name": "Marketing", "attributes": [], "suggested_decision": "approve", "suggestion_note": "header",
         "subcategories": [{"name": "Bestseller", "attributes": ["bestseller_label", "bestseller_badge"],
                            "suggested_decision": "approve", "suggestion_note": "Suggested: approve"}]},
        {"name": "Other", "attributes": [], "suggested_decision": "approve",
         "subcategories": [{"name": "Rest", "attributes": [a for a in attrs if a not in ("bestseller_label", "bestseller_badge")],
                            "suggested_decision": "bogus"}]}]}
    p = write(tmp_path / "prop.json", json.dumps(prop))
    import_taxonomy(conn, s, p)
    rows = list(csv.DictReader(open(s.path(s.outputs_dir) / "category_review.csv", encoding="utf-8-sig"), delimiter=";"))
    head = next(r for r in rows if r["proposed_category"] == "Marketing" and not r["proposed_subcategory"])
    best = next(r for r in rows if r["proposed_subcategory"] == "Bestseller")
    rest = next(r for r in rows if r["proposed_subcategory"] == "Rest")
    assert best["decision"] == "approve" and best["notes"] == "Suggested: approve"
    assert rest["decision"] == ""                        # unknown suggestion is ignored
    assert int(head["findings_count"]) == int(best["findings_count"]) > 0   # header shows subcategory totals
    rest["decision"] = "approve"
    out = tmp_path / "review.csv"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader(); w.writerows(rows)
    rep = apply_review(conn, s, out)
    assert rep.get("version") or rep.get("taxonomy_version")
