"""Modules G + H — export findings for ChatGPT category discovery, import proposals,
human review via CSV, and versioned approved taxonomy. All counts are computed here, never by the LLM."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .config import Settings
from .db import now
from .util import log, read_table, slug, strip_code_fences, write_csv, write_json, write_xlsx

DECISIONS = {"approve", "rename", "merge", "reject", "add"}
UNASSIGNED = "(unassigned)"


class TaxonomyError(Exception):
    pass


# ------------------------------------------------------------------ statistics
def attribute_stats(conn: sqlite3.Connection) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT f.attribute, COUNT(*) n, COUNT(DISTINCT f.isbn13) b, COUNT(DISTINCT f.isbn13||f.image_id) i "
            "FROM findings f JOIN extractions e ON e.extraction_id=f.extraction_id AND e.is_current=1 "
            "GROUP BY f.attribute"):
        stats[r[0]] = {"attribute": r[0], "findings_count": r[1], "books_count": r[2], "images_count": r[3],
                       "evidence_types": {}, "sample_values": [], "sample_evidence": []}
    for attr, et, n in conn.execute(
            "SELECT f.attribute, COALESCE(f.evidence_type,'unknown'), COUNT(*) FROM findings f JOIN extractions e "
            "ON e.extraction_id=f.extraction_id AND e.is_current=1 GROUP BY 1,2"):
        stats[attr]["evidence_types"][et] = n
    vals: dict[str, Counter] = defaultdict(Counter)
    evid: dict[str, list] = defaultdict(list)
    for attr, value, ev in conn.execute(
            "SELECT f.attribute, f.value, f.evidence FROM findings f JOIN extractions e "
            "ON e.extraction_id=f.extraction_id AND e.is_current=1 ORDER BY f.finding_id"):
        vals[attr][value] += 1
        if ev and len(evid[attr]) < 3 and ev not in evid[attr]:
            evid[attr].append(ev)
    for attr, c in vals.items():
        stats[attr]["sample_values"] = [json.loads(v) if v else None for v, _ in c.most_common(5)]
        stats[attr]["sample_evidence"] = evid[attr]
    return stats


def _group_stats(conn: sqlite3.Connection, attributes: list[str]) -> dict:
    if not attributes:
        return {"findings_count": 0, "books_count": 0, "images_count": 0}
    q = ",".join("?" * len(attributes))
    r = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT f.isbn13), COUNT(DISTINCT f.isbn13||f.image_id) FROM findings f "
        f"JOIN extractions e ON e.extraction_id=f.extraction_id AND e.is_current=1 WHERE f.attribute IN ({q})",
        attributes).fetchone()
    return {"findings_count": r[0], "books_count": r[1], "images_count": r[2]}


# ------------------------------------------------------------------ G: export for discovery
def iter_current_findings(conn: sqlite3.Connection):
    for r in conn.execute(
            "SELECT f.isbn13, f.image_id, e.image_type, f.attribute, f.value, f.evidence, f.evidence_type, "
            "e.needs_review FROM findings f JOIN extractions e ON e.extraction_id=f.extraction_id AND e.is_current=1 "
            "ORDER BY f.isbn13, f.image_id, f.finding_id"):
        yield {"isbn": r[0], "image_id": r[1], "image_type": r[2], "attribute": r[3],
               "value": json.loads(r[4]) if r[4] else None, "evidence": r[5], "evidence_type": r[6],
               "needs_review": bool(r[7])}


def current_taxonomy(conn: sqlite3.Connection) -> tuple[str, dict] | None:
    """The taxonomy to build on: the latest APPROVED version, else the latest imported proposal.
    Returned in the proposal format (categories > subcategories > attributes)."""
    row = conn.execute("SELECT version, taxonomy_json FROM taxonomy_versions ORDER BY version DESC LIMIT 1").fetchone()
    if row:
        t = json.loads(row[1])
        cats = [{"name": c["name"], "description": c.get("description"), "attributes": c.get("attributes", []),
                 "subcategories": [{"name": s["name"], "description": s.get("description"),
                                    "attributes": s.get("attributes", [])} for s in c.get("subcategories", [])]}
                for c in t.get("categories", [])]
        return f"approved version {row[0]}", {"categories": cats}
    row = conn.execute("SELECT proposal_id, source_file, raw_json FROM taxonomy_proposals "
                       "ORDER BY proposal_id DESC LIMIT 1").fetchone()
    if row:
        p = json.loads(row[2])
        cats = [{"name": c.get("name"), "description": c.get("description"), "attributes": c.get("attributes") or [],
                 "subcategories": [{"name": s.get("name"), "description": s.get("description"),
                                    "attributes": s.get("attributes") or []} for s in c.get("subcategories") or []]}
                for c in p.get("categories", [])]
        return f"latest proposal #{row[0]} ({row[1]}), not yet approved", {"categories": cats}
    return None


def _taxonomy_attributes(t: dict) -> set[str]:
    return {slug(a, 80) for c in t["categories"] for a in (c.get("attributes") or []) +
            [a for s in c.get("subcategories") or [] for a in s.get("attributes") or []]}


def export_findings(conn: sqlite3.Connection, settings: Settings, max_attributes_per_part: int = 150,
                    examples_per_attribute: int = 8, fresh: bool = False) -> dict:
    out = settings.path(settings.outputs_dir)
    stats = attribute_stats(conn)
    findings = list(iter_current_findings(conn))  # full export (text only, small even for 100k records)
    write_json(out / "all_image_findings.json", {
        "generated_at": now(), "finding_count": len(findings), "attribute_count": len(stats),
        "attribute_summary": sorted(stats.values(), key=lambda s: -s["findings_count"]),
        "findings": findings})

    base = None if fresh else current_taxonomy(conn)
    if base:
        # UPDATE mode: build on the existing taxonomy; examples mostly for attributes not yet placed.
        label, tax = base
        known = _taxonomy_attributes(tax)
        new_attrs = sorted(a for a in stats if a not in known)
        per_attr: dict[str, list] = defaultdict(list)
        for f in findings:
            cap = examples_per_attribute if f["attribute"] in new_attrs else 2
            if len(per_attr[f["attribute"]]) < cap:
                per_attr[f["attribute"]].append(f)
        pdir = out / "category_discovery"
        pdir.mkdir(parents=True, exist_ok=True)
        write_json(pdir / "update_input.json", {
            "mode": "update", "based_on": label, "current_taxonomy": tax, "new_attributes": new_attrs,
            "attribute_summary": [stats[a] for a in sorted(stats)],
            "findings": [f for a in sorted(stats) for f in per_attr[a]]})
        prompt = settings.path("prompts/category_update.txt").read_text(encoding="utf-8").replace("{{BASE}}", label)
        (out / "category_discovery_prompt.txt").write_text(prompt, encoding="utf-8")
        log.info("update mode: building on %s; %d new attribute(s) to place", label, len(new_attrs))
        return {"mode": "update", "based_on": label, "findings": len(findings), "attributes": len(stats),
                "new_attributes": len(new_attrs), "attach": str(pdir / "update_input.json"),
                "prompt": str(out / "category_discovery_prompt.txt")}

    # Discovery parts: split by attribute so every attribute is seen whole in exactly one part,
    # with a capped number of example findings (ChatGPT needs patterns, not every row).
    examples: dict[str, list] = defaultdict(list)
    for f in findings:
        if len(examples[f["attribute"]]) < examples_per_attribute:
            examples[f["attribute"]].append(f)
    attrs = sorted(stats)  # alphabetical keeps related names (bestseller_*) together
    parts = [attrs[i:i + max_attributes_per_part] for i in range(0, len(attrs), max_attributes_per_part)] or [[]]
    pdir = out / "category_discovery"
    pdir.mkdir(parents=True, exist_ok=True)
    template = settings.path("prompts/category_discovery.txt").read_text(encoding="utf-8")
    for n, chunk in enumerate(parts, 1):
        write_json(pdir / f"part_{n:02d}.json", {
            "part": n, "part_count": len(parts),
            "attribute_summary": [stats[a] for a in chunk],
            "findings": [f for a in chunk for f in examples[a]]})
        (pdir / f"part_{n:02d}_prompt.txt").write_text(
            template.replace("{{PART}}", str(n)).replace("{{PART_COUNT}}", str(len(parts))), encoding="utf-8")
    (out / "category_discovery_prompt.txt").write_text(
        template.replace("{{PART}}", "1").replace("{{PART_COUNT}}", str(len(parts))), encoding="utf-8")
    if len(parts) > 1:
        (pdir / "reconciliation_prompt_template.txt").write_text(
            settings.path("prompts/category_reconciliation.txt").read_text(encoding="utf-8"), encoding="utf-8")
    log.info("exported %d findings, %d attributes, %d discovery part(s)", len(findings), len(stats), len(parts))
    return {"mode": "fresh", "findings": len(findings), "attributes": len(stats), "parts": len(parts),
            "attach": str(pdir / "part_01.json"), "prompt": str(out / "category_discovery_prompt.txt")}


# ------------------------------------------------------------------ G: import proposal
def _load_json(path: Path):
    try:
        return json.loads(strip_code_fences(path.read_text(encoding="utf-8-sig")))
    except json.JSONDecodeError as e:
        raise TaxonomyError(f"invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None


def _change_note(x: dict) -> str:
    parts = [x.get("change_note") or ""]
    if x.get("previous_name"):
        parts.append(f"previously: {x['previous_name']}")
    if x.get("merged_from"):
        parts.append("merged from: " + ", ".join(map(str, x["merged_from"])))
    return "; ".join(p for p in parts if p)


def _flatten(proposal: dict) -> list[dict]:
    if not isinstance(proposal, dict) or not isinstance(proposal.get("categories"), list):
        raise TaxonomyError('proposal must be an object with a "categories" list')
    rows = []
    for c in proposal["categories"]:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            raise TaxonomyError(f"category without name: {str(c)[:80]}")
        cname = c["name"].strip()
        rows.append({"category": cname, "subcategory": None, "description": c.get("description"),
                     "attributes": [slug(a, 80) for a in c.get("attributes") or [] if isinstance(a, str)],
                     "examples": [e for e in c.get("examples") or [] if isinstance(e, str)][:5],
                     "status": c.get("status") or "", "change_note": _change_note(c), **_suggestion(c)})
        for s in c.get("subcategories") or []:
            if not isinstance(s, dict) or not str(s.get("name") or "").strip():
                raise TaxonomyError(f"subcategory without name in {cname}")
            rows.append({"category": cname, "subcategory": s["name"].strip(), "description": s.get("description"),
                         "attributes": [slug(a, 80) for a in s.get("attributes") or [] if isinstance(a, str)],
                         "examples": [e for e in s.get("examples") or [] if isinstance(e, str)][:5],
                         "status": s.get("status") or "", "change_note": _change_note(s), **_suggestion(s)})
    return rows


def _suggestion(node: dict) -> dict:
    """Optional pre-filled review decision from the proposal (the reviewer can overwrite it)."""
    d = str(node.get("suggested_decision") or "").strip().lower()
    return {"suggested_decision": d if d in DECISIONS else "",
            "suggested_final_category": str(node.get("suggested_final_category") or "").strip(),
            "suggested_final_subcategory": str(node.get("suggested_final_subcategory") or "").strip(),
            "suggestion_note": str(node.get("suggestion_note") or "").strip()}


def import_taxonomy(conn: sqlite3.Connection, settings: Settings, path: Path) -> dict:
    proposal = _load_json(path)
    rows = _flatten(proposal)
    stats = attribute_stats(conn)
    cur = conn.execute("INSERT INTO taxonomy_proposals(source_file, raw_json, imported_at) VALUES (?,?,?)",
                       (path.name, json.dumps(proposal, ensure_ascii=False), now()))
    proposal_id = cur.lastrowid
    conn.commit()

    assigned: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        r["path"] = r["category"] + (f" > {r['subcategory']}" if r["subcategory"] else "")
        for a in r["attributes"]:
            assigned[a].append(r["path"])
        r.update(_group_stats(conn, r["attributes"]))
    for r in rows:   # category header rows: show totals over their subcategories (display only)
        if r["subcategory"] is None and not r["attributes"]:
            sub_attrs = [a for x in rows if x["category"] == r["category"] and x["subcategory"] for a in x["attributes"]]
            r.update(_group_stats(conn, sub_attrs))
    unassigned = sorted({slug(a, 80) for a in proposal.get("unassigned_attributes") or [] if isinstance(a, str)})
    known = set(stats)
    unknown = sorted((set(assigned) | set(unassigned)) - known)
    unmapped = sorted(known - set(assigned) - set(unassigned))
    duplicates = {a: p for a, p in assigned.items() if len(p) > 1}

    out = settings.path(settings.outputs_dir)
    write_json(out / "category_candidates.json", {
        "proposal_id": proposal_id, "source_file": path.name, "imported_at": now(),
        "note": "counts computed by the pipeline from current findings; not from the LLM",
        "explanation": proposal.get("explanation"), "open_questions": proposal.get("open_questions"),
        "synonym_groups": proposal.get("synonym_groups"),
        "candidates": rows, "unassigned_attributes": [{"attribute": a, **stats.get(a, {})} for a in unassigned],
        "unmapped_attributes_not_in_proposal": unmapped, "unknown_attributes_not_in_data": unknown,
        "attributes_assigned_more_than_once": duplicates})

    review_rows = []
    for r in rows:
        review_rows.append((proposal_id, r["category"], r["subcategory"] or "", r.get("status", ""),
                            r.get("change_note", ""), r.get("suggested_decision", ""),
                            r.get("suggested_final_category", ""), r.get("suggested_final_subcategory", ""),
                            ";".join(r["attributes"]),
                            r["findings_count"], r["books_count"], " | ".join(r["examples"][:3]),
                            r["description"] or "", r.get("suggestion_note", "")))
    for a in unassigned + unmapped:
        s = stats.get(a, {})
        review_rows.append((proposal_id, UNASSIGNED, "", "", "", "", "", "", a, s.get("findings_count", 0),
                            s.get("books_count", 0), " | ".join(s.get("sample_evidence", [])[:2]), "",
                            "not in proposal" if a in unmapped else "LLM left unassigned"))
    review_header = ["proposal_id", "proposed_category", "proposed_subcategory", "change", "change_note", "decision",
                     "final_category", "final_subcategory", "attributes", "findings_count", "books_count", "examples",
                     "description", "notes"]
    review_csv = out / "category_review.csv"
    write_csv(review_csv, review_header, review_rows)
    write_xlsx(out / "category_review.xlsx", [("Review", review_header, review_rows)],
               {"change": 11, "change_note": 40, "decision": 12, "final_category": 24, "final_subcategory": 24,
                "attributes": 45, "examples": 45, "description": 45, "notes": 30})
    log.info("proposal %d: %d (sub)categories, %d unassigned, %d unmapped, %d unknown, %d multi-assigned",
             proposal_id, len(rows), len(unassigned), len(unmapped), len(unknown), len(duplicates))
    return {"proposal_id": proposal_id, "candidates": len(rows), "unassigned": len(unassigned),
            "unmapped": len(unmapped), "unknown": len(unknown), "multi_assigned": len(duplicates),
            "changes": dict(Counter(r.get("status") or "unmarked" for r in rows)),
            "review_file": str(out / "category_review.xlsx")}


# ------------------------------------------------------------------ H: apply human review
def apply_review(conn: sqlite3.Connection, settings: Settings, csv_path: Path, approve_pending: bool = False) -> dict:
    rows = read_table(csv_path)
    if not rows:
        raise TaxonomyError("review file is empty")
    errors, decisions = [], []
    mapping: dict[str, tuple[str, str | None]] = {}
    descriptions: dict[tuple[str, str | None], str] = {}
    proposal_ids = {r.get("proposal_id") for r in rows if r.get("proposal_id")}
    for line, r in enumerate(rows, start=2):
        d = (r.get("decision") or "").strip().lower()
        pc, ps = r.get("proposed_category") or "", r.get("proposed_subcategory") or ""
        if not d:
            if approve_pending and pc and pc != UNASSIGNED:
                d = "approve"
            elif pc == UNASSIGNED:
                d = "reject"   # unassigned attributes stay unmapped unless the reviewer acts
            else:
                errors.append(f"line {line}: no decision for '{pc} > {ps}'")
                continue
        if d not in DECISIONS:
            errors.append(f"line {line}: unknown decision {d!r} (use {', '.join(sorted(DECISIONS))})")
            continue
        fc, fs = (r.get("final_category") or "").strip(), (r.get("final_subcategory") or "").strip()
        if d in ("rename", "merge", "add") and not fc:
            errors.append(f"line {line}: decision '{d}' needs final_category")
            continue
        if d == "approve" and pc == UNASSIGNED and not fc:
            errors.append(f"line {line}: approving an unassigned attribute needs final_category")
            continue
        attrs = [slug(a, 80) for a in (r.get("attributes") or "").replace(",", ";").split(";") if a.strip()]
        if d == "reject":
            decisions.append((pc, ps, d, None, r.get("notes")))
            continue
        cat = fc or pc
        sub = (fs or (ps if not fc else "")) or None
        final_path = cat + (f" > {sub}" if sub else "")
        decisions.append((pc or None, ps or None, d, final_path, r.get("notes")))
        if r.get("description"):
            descriptions.setdefault((cat, sub), r["description"])
        for a in attrs:
            if a in mapping and mapping[a] != (cat, sub):
                errors.append(f"line {line}: attribute {a!r} mapped to both {mapping[a]} and {(cat, sub)}")
            mapping[a] = (cat, sub)
    if errors:
        raise TaxonomyError("review file has problems:\n  " + "\n  ".join(errors))

    version = (conn.execute("SELECT MAX(version) FROM taxonomy_versions").fetchone()[0] or 0) + 1
    tree: dict[str, dict] = {}
    for a, (cat, sub) in sorted(mapping.items()):
        c = tree.setdefault(cat, {"name": cat, "key": slug(cat), "description": descriptions.get((cat, None)),
                                  "attributes": [], "subcategories": {}})
        if sub:
            s = c["subcategories"].setdefault(sub, {"name": sub, "key": slug(sub),
                                                    "description": descriptions.get((cat, sub)), "attributes": []})
            s["attributes"].append(a)
        else:
            c["attributes"].append(a)
    categories = []
    for c in sorted(tree.values(), key=lambda x: x["name"]):
        all_attrs = c["attributes"] + [a for s in c["subcategories"].values() for a in s["attributes"]]
        subs = []
        for s in sorted(c["subcategories"].values(), key=lambda x: x["name"]):
            subs.append({**s, **_group_stats(conn, s["attributes"])})
        categories.append({**{k: v for k, v in c.items() if k != "subcategories"},
                           "subcategories": subs, **_group_stats(conn, all_attrs)})
    known = set(attribute_stats(conn))
    taxonomy = {"taxonomy": "image", "version": version, "created_at": now(),
                "based_on_proposal_ids": sorted(proposal_ids), "review_file": csv_path.name,
                "categories": categories,
                "attribute_mapping": {a: {"category": c, "subcategory": s} for a, (c, s) in sorted(mapping.items())},
                "unmapped_attributes": sorted(known - set(mapping)),
                "review_decisions": [{"proposed": (p or "") + (f" > {s}" if s else ""), "decision": d,
                                      "approved_as": f, "notes": n} for p, s, d, f, n in decisions]}
    conn.execute("INSERT INTO taxonomy_versions(version, proposal_id, review_file, created_at, taxonomy_json) "
                 "VALUES (?,?,?,?,?)", (version, int(min(proposal_ids)) if proposal_ids else None, csv_path.name,
                                        now(), json.dumps(taxonomy, ensure_ascii=False)))
    conn.executemany("INSERT INTO category_reviews(version, proposed_path, decision, approved_path, notes) "
                     "VALUES (?,?,?,?,?)", [(version, (p or "") + (f" > {s}" if s else ""), d, f, n)
                                            for p, s, d, f, n in decisions])
    conn.executemany("INSERT INTO attribute_mappings(version, attribute, category, subcategory) VALUES (?,?,?,?)",
                     [(version, a, c, s) for a, (c, s) in mapping.items()])
    conn.commit()
    write_json(settings.path(settings.outputs_dir) / "image_taxonomy.json", taxonomy)
    log.info("approved taxonomy version %d: %d categories, %d attributes mapped, %d unmapped",
             version, len(categories), len(mapping), len(taxonomy["unmapped_attributes"]))
    return {"version": version, "categories": len(categories), "mapped_attributes": len(mapping),
            "unmapped_attributes": len(taxonomy["unmapped_attributes"])}


def current_mapping(conn: sqlite3.Connection) -> tuple[int | None, dict[str, tuple[str, str | None]]]:
    v = conn.execute("SELECT MAX(version) FROM taxonomy_versions").fetchone()[0]
    if not v:
        return None, {}
    return v, {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT attribute, category, subcategory FROM attribute_mappings WHERE version=?", (v,))}
