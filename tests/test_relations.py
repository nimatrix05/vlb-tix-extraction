"""Related products, series, previews, linking and bundle candidates (no network)."""
import openpyxl

from conftest import ISBN_A, ISBN_B, ISBN_C, write
from pipeline.books import import_books
from pipeline.relations import build_relations, relations_report
from pipeline.sources import vlbtix_playwright as vp


def test_parse_cards_from_real_vlbtix_text():
    c = vp.parse_product_card("Position 1Kopf aus – Lust an\nvon Julia Henchen\nmvg\nSoftcoverText240 Seiten\n"
                              "EUR 17,00\nET: 21.11.2023\n978-3-7474-0568-0\n Lieferbar", "9783747405680")
    assert (c["title"], c["author"], c["publisher"], c["price_eur"], c["release_date"], c["availability"]) == \
        ("Kopf aus – Lust an", "Julia Henchen", "mvg", "17.00", "21.11.2023", "Lieferbar")
    p = vp.parse_preview_card("Black Library\nBlack Library\n25.03.2025\n4 Produkte")
    assert (p["preview_name"], p["preview_date"], p["product_count"]) == ("Black Library", "25.03.2025", 4)
    assert vp.parse_series("Black Library\nThe Scouring, Band 1\n") == ("The Scouring", 1)
    assert vp.parse_series("Seitenanzahl Hauptinhalt: 464") == (None, None)


def _setup(project, tmp_path):
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13,author,publisher\n{ISBN_A},\"Wraight, Chris\",Black Library\n"
                                                f"{ISBN_B},\"Rath, Robert\",Black Library\n{ISBN_C},\"Muster, Max\",mvg\n"))
    ins = conn.execute
    ins("INSERT INTO book_vlbtix_info VALUES (?,?,?,?,?,?,?)", (ISBN_A, "The Scouring", 1, 20.0, "19.01.2026", "Lieferbar", "t"))
    ins("INSERT INTO book_vlbtix_info VALUES (?,?,?,?,?,?,?)", (ISBN_B, "The Scouring", 2, 22.0, "01.06.2026", "Lieferbar", "t"))
    ins("INSERT INTO product_relations(isbn13, relation, related_ean, title, seen_at) VALUES (?,?,?,?,?)",
        (ISBN_A, "Referenztitel", "9783747405680", "not in list", "t"))
    ins("INSERT INTO product_relations(isbn13, relation, related_ean, title, seen_at) VALUES (?,?,?,?,?)",
        (ISBN_C, "Ähnliche Produkte", ISBN_A, "in list", "t"))
    ins("INSERT INTO product_relations(isbn13, relation, related_ean, title, seen_at) VALUES (?,?,?,?,?)",
        (ISBN_A, "Weitere Ausgabearten", ISBN_C, "e-book edition", "t"))
    conn.commit()
    return s, conn


def test_linking_scores_and_clusters(project, tmp_path):
    s, conn = _setup(project, tmp_path)
    d = build_relations(conn)
    ab = d["pairs"][tuple(sorted((ISBN_A, ISBN_B)))]
    assert ab["score"] == 3.0 and "same series" in ab["reasons"][0]
    ac = d["pairs"][tuple(sorted((ISBN_A, ISBN_C)))]
    assert ac["score"] == 1.0                      # 'Ähnliche Produkte' only; other format is not a bundle link
    assert len(d["clusters"]) == 1 and d["clusters"][0][1] == 2


def test_relations_report_excel(project, tmp_path):
    s, conn = _setup(project, tmp_path)
    rep = relations_report(conn, s)
    assert rep["bundle_candidates"] == 1 and rep["related_in_our_list"] == 2
    wb = openpyxl.load_workbook(rep["excel_file"])
    assert wb.sheetnames == ["Summary", "Bundle candidates", "Links", "Series", "Previews & Specials", "Clusters",
                             "Book features"]
    feats = {r[0]: r for r in wb["Book features"].iter_rows(min_row=2, values_only=True)}
    assert feats[ISBN_B][4] == "yes" and feats[ISBN_A][4] == "no"     # earlier volume in our list
    links = list(wb["Links"].iter_rows(min_row=2, values_only=True))
    assert {(l[2], l[3]) for l in links} >= {("9783747405680", "no"), (ISBN_A, "yes")}


def test_link_activates_when_book_imported_later(project, tmp_path):
    s, conn = _setup(project, tmp_path)
    assert tuple(sorted((ISBN_A, "9783747405680"))) not in build_relations(conn)["pairs"]
    import_books(conn, write(tmp_path / "next_week.csv", "isbn13\n9783747405680\n"))
    assert tuple(sorted((ISBN_A, "9783747405680"))) in build_relations(conn)["pairs"]


def test_card_author_without_translator_and_label_kinds():
    from pipeline.relations import relation_kind
    c = vp.parse_product_card("Warhammer 40.000 - Night Lords\nDer Sammelband\nvon Aaron Dembski-Bowden, übersetzt von "
                              "Sebastian Gehart\nBlack Library\nPaperback, 928 Seiten\nEUR 25,00\nET: 10.12.2024\n"
                              "978-1-80407-890-7\nLieferbar", "9781804078907")
    assert c["author"] == "Aaron Dembski-Bowden" and c["publisher"] == "Black Library" and c["price_eur"] == "25.00"
    assert relation_kind("Produkt derselben Reihe") == "series_link"
    assert relation_kind("Ähnliches Produkt") == "similar"
    assert relation_kind("Warhammer 40.000 The Horus Heresy") == "reference"
    assert relation_kind("Weitere Ausgabearten") is None


def test_same_author_alone_is_not_a_bundle(project, tmp_path):
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13,author\n{ISBN_A},\"Riedl, Matthias\"\n{ISBN_B},\"Riedl, Matthias\"\n"))
    d = build_relations(conn)
    p = d["pairs"][tuple(sorted((ISBN_A, ISBN_B)))]
    assert p["score"] == 1.0 and not d["clusters"]
    rep = relations_report(conn, s)
    assert rep["bundle_candidates"] == 0
    feats = {r[0]: r for r in openpyxl.load_workbook(rep["excel_file"])["Book features"].iter_rows(min_row=2, values_only=True)}
    assert feats[ISBN_A][11] == 1         # 'Other books by same author in list' is kept as a feature


def test_remove_images(project, tmp_path):
    from conftest import image_bytes
    from pipeline.collector import import_local_images, remove_images
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13\n{ISBN_A}\n"))
    (tmp_path / "i").mkdir()
    for n in range(3):
        (tmp_path / "i" / f"{n}.png").write_bytes(image_bytes((n * 80, 2, 2)))
    import_local_images(conn, s, write(tmp_path / "m.csv", "isbn13,file_path\n" +
                                       "".join(f"{ISBN_A},i/{n}.png\n" for n in range(3))))
    rep = remove_images(conn, s, ISBN_A, ["img_002", "img_003", "img_009"])
    assert rep["removed"] == ["img_002", "img_003"] and rep["refused"] == ["img_009: not found"]
    assert [r[0] for r in conn.execute("SELECT image_id FROM images")] == ["img_001"]
    assert (s.path("data/_removed") / ISBN_A / "img_002.png").exists()


def test_new_vlbtix_labels():
    from pipeline.relations import relation_kind
    assert relation_kind("Vergleichstitel") == "comparison"
    assert relation_kind("Ersetzt") is None
    assert relation_kind("Produkt desselben Autors / derselben Autorin") == "same_author"
