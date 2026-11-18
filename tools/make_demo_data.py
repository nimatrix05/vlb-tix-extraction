"""Create SYNTHETIC demo inputs (no network, no real books, no licensed images).

    python tools\\make_demo_data.py

Writes:
  input/demo_books.csv          5 fictional books (titles marked SYNTHETIC)
  input/demo_images/*.png|jpg   generated placeholder covers/posters
  input/demo_local_images.csv   mapping for `import-local-images`
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pipeline.isbn import make_isbn13  # noqa: E402

BOOKS = [
    ("Der stille Fluss", "Fiktiv Verlag", ["front_cover", "event_poster"]),
    ("Nachtzug nach Nirgendwo", "Fiktiv Verlag", ["front_cover", "bestseller_banner"]),
    ("Kochen mit Licht", "Beispiel & Söhne", ["front_cover"]),
    ("Die Erbin von Morgen (Band 2)", "Beispiel & Söhne", ["front_cover", "back_cover", "award_graphic"]),
    ("Kleine Wölfe", "Musterhaus Kinderbuch", ["front_cover"]),
]
TEXT = {
    "front_cover": "{title}",
    "back_cover": "»Ein Meisterwerk« – Beispiel-Zeitung",
    "event_poster": "Lesung in Berlin – 12. März",
    "bestseller_banner": "SPIEGEL-Bestseller (Demo)",
    "award_graphic": "Nominiert: Fiktiver Buchpreis 2026",
}
COLORS = [(30, 60, 110), (120, 30, 40), (40, 110, 70), (90, 70, 20), (60, 40, 100)]


def main() -> None:
    img_dir = ROOT / "input" / "demo_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    books_rows, img_rows = [], []
    for n, (title, pub, kinds) in enumerate(BOOKS):
        isbn = make_isbn13(f"97839999{n:04d}")
        books_rows.append([isbn, f"SYNTHETIC – {title}", pub, f"synthetic://book/{isbn}"])
        for k, kind in enumerate(kinds):
            w, h = (600, 900) if "cover" in kind else (900, 500)
            im = Image.new("RGB", (w, h), COLORS[(n + k) % len(COLORS)])
            d = ImageDraw.Draw(im)
            d.rectangle([20, 20, w - 20, h - 20], outline=(240, 240, 240), width=4)
            d.text((40, h // 2), TEXT[kind].format(title=title), fill=(255, 255, 255))
            d.text((40, h - 60), f"SYNTHETIC DEMO IMAGE {isbn}", fill=(220, 220, 220))
            ext = "jpg" if k % 2 == 0 else "png"
            name = f"{isbn}_{kind}.{ext}"
            im.save(img_dir / name, quality=90) if ext == "jpg" else im.save(img_dir / name)
            img_rows.append([isbn, f"demo_images/{name}", "synthetic demo image - no rights restrictions", ""])
    with (ROOT / "input" / "demo_books.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["isbn13", "title", "publisher", "source_ref"])
        w.writerows(books_rows)
    with (ROOT / "input" / "demo_local_images.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["isbn13", "file_path", "rights_note", "source_page"])
        w.writerows(img_rows)
    print(f"Wrote {len(books_rows)} synthetic books and {len(img_rows)} images to {ROOT / 'input'}")


if __name__ == "__main__":
    main()
