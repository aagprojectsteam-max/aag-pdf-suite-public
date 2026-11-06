#!/usr/bin/env python3
"""OCR and duplicate engine of AAG - PDF Suite (from AAG PDF Dedup 0.1.0, commit ef3bd6c).

A page is removed only when another kept page proves that it adds nothing:
  1. candidates come from OCR text similarity, visual-word similarity and neighbours;
  2. SIFT + homography must align the two pages (survives angle, zoom and crop);
  3. after alignment, nearly all ink of the removed page must exist on the kept page.

The input file is never modified. Removed pages are also saved to their own PDF.
Lines that start with @@ are JSON progress events for the window.
"""
import argparse
import csv
import html
import json
import math
import multiprocessing as mp
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zlib
from pathlib import Path

import cv2
import numpy as np
import pikepdf

VERSION = "0.1.0"
CACHE_VERSION = 1

RENDER_LONG_EDGE = 1400   # one common size, so sharp and blurry copies look alike to SIFT
SIFT_FEATURES = 4000
MIN_KEYPOINTS = 60        # below this a page counts as blank and is never removed
RATIO = 0.8
RANSAC_PX = 5.0
MIN_INLIERS = 30
INK_TOLERANCE_PX = 3
AUTO_COVER = 0.97         # share of a page's ink that the other page must contain
MUTUAL_MARGIN = 0.002     # a bigger gap means one page holds more: only the other may go
DETAIL_GAP = 1.2          # one copy has this many times the pixels per letter: it must not go
AUTO_HULL = 0.50          # share of the page that the matched points must span
REVIEW_COVER = 0.80
REVIEW_TEXT = 0.85
TEXT_DIMS = 8192
TEXT_MIN_CHARS = 80
TOP_K = 8
NEIGHBOURS = 2
VOCAB = 512
CHUNK = 25


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def progress(stage, done=0, total=0):
    print("@@" + json.dumps({"stage": stage, "done": done, "total": total}), flush=True)


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def page_count(pdf):
    with pikepdf.open(pdf) as doc:
        return len(doc.pages)


# ---------------------------------------------------------------- OCR and text

def ocr_stage(src, work, lang, jobs, deskew, skip_ocr):
    out = work / "ocr.pdf"
    if out.exists():
        log("OCR: using the cached result")
        return out
    if skip_ocr:
        os.link(src, out) if os.stat(src).st_dev == os.stat(work).st_dev else shutil.copy2(src, out)
        return out
    # Ubuntu's AppArmor profile lets Ghostscript create scratch files only in temp folders, and /tmp is in RAM here.
    tmp = tempfile.mkdtemp(prefix="aag-pdf-dedup-", dir="/var/tmp")
    env = dict(os.environ, TMPDIR=tmp)
    cmd = ["ocrmypdf", "-l", lang, "--jobs", str(jobs), "--output-type", "pdf",
           "--skip-text", "--rotate-pages", "--optimize", "0",
           "--tesseract-timeout", "180", "--quiet"]
    if deskew:
        cmd.append("--deskew")
    part = work / "ocr.part.pdf"
    total = page_count(src)
    log(f"OCR: starting ({lang}, {jobs} parallel jobs)")
    progress("ocr", 0, total)
    finished = threading.Event()

    def watch():
        # ocrmypdf leaves one file per finished page in its temp folder. Progress only: a change there is harmless.
        while not finished.wait(3):
            progress("ocr", min(sum(1 for _ in Path(tmp).rglob("*_ocr_tess.pdf")), total), total)

    threading.Thread(target=watch, daemon=True).start()
    try:
        run(cmd + [str(src), str(part)], env=env)
    finally:
        finished.set()
        shutil.rmtree(tmp, ignore_errors=True)
    part.rename(out)
    log("OCR: done")
    return out


def page_texts(pdf, work, pages):
    path = work / "text.txt"
    if not path.exists():
        run(["pdftotext", "-enc", "UTF-8", str(pdf), str(path)])
    parts = path.read_text("utf-8", "replace").split("\f")
    return (parts + [""] * pages)[:pages]


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[֑-ׇ]", "", text)
    return re.sub(r"[^\w]+", " ", text).strip().lower()


def text_matrix(texts):
    """Hashed character 4-grams, tf-idf weighted: tolerant of OCR errors."""
    n = len(texts)
    matrix = np.zeros((n, TEXT_DIMS), np.float32)
    lengths = np.zeros(n, np.int32)
    for i, raw in enumerate(texts):
        text = normalize(raw)
        lengths[i] = len(text)
        if len(text) < TEXT_MIN_CHARS:
            continue
        data = text.encode("utf-8")
        ids = [zlib.crc32(data[k:k + 8]) % TEXT_DIMS for k in range(0, len(data) - 8, 2)]
        np.add.at(matrix[i], ids, 1.0)
    return tfidf(matrix), lengths


def tfidf(matrix):
    df = (matrix > 0).sum(0)
    idf = np.log((1 + matrix.shape[0]) / (1 + df)).astype(np.float32)
    out = np.log1p(matrix) * idf
    norm = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(norm, 1e-9)


# ---------------------------------------------------------------- page features

def ink_mask(gray):
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    ink = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, 41, 18)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    h, w = ink.shape
    x, y, bw, bh, area = stats.T
    drop = (x == 0) | (y == 0) | (x + bw >= w) | (y + bh >= h) | (area < 3)
    drop[0] = True
    keep = (~drop).astype(np.uint8) * 255
    return keep[labels]


def features_chunk(job):
    pdf, work, first, last = job
    pages = Path(work) / "pages"
    feats = Path(work) / "feat"
    todo = [p for p in range(first, last + 1) if not (feats / f"{p:06d}.npz").exists()]
    if not todo:
        return
    prefix = pages / f"chunk{first}"
    run(["pdftoppm", "-gray", "-scale-to", str(RENDER_LONG_EDGE), "-f", str(first), "-l", str(last),
         "-png", str(pdf), str(prefix)], stderr=subprocess.DEVNULL)
    for item in pages.glob(f"chunk{first}-*.png"):
        number = int(item.stem.rsplit("-", 1)[1])
        target = pages / f"{number:06d}.png"
        item.replace(target)
        gray = cv2.imread(str(target), cv2.IMREAD_GRAYSCALE)
        sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES)
        keypoints, desc = sift.detectAndCompute(gray, None)
        pts = np.float32([k.pt for k in keypoints]).reshape(-1, 2)
        desc = np.zeros((0, 128), np.uint8) if desc is None else desc.astype(np.uint8)
        ink = ink_mask(gray)
        sharp = float(cv2.Laplacian(gray, cv2.CV_32F).var())
        np.savez(feats / f"{number:06d}.npz", pts=pts, desc=desc, sharp=sharp,
                 ink=float((ink > 0).mean()), shape=np.int32(gray.shape))


def load_feat(work, page):
    return np.load(Path(work) / "feat" / f"{page:06d}.npz")


def worker_init():
    cv2.setNumThreads(1)


def image_ppi(pdf, pages):
    """Highest native image resolution on each page: the honest quality signal."""
    ppi = [0] * (pages + 1)
    pixels = [0] * (pages + 1)   # long edge of that image
    out = subprocess.run(["pdfimages", "-list", str(pdf)], capture_output=True, text=True).stdout
    for line in out.splitlines()[2:]:
        cols = line.split()
        if len(cols) >= 13 and cols[0].isdigit() and cols[12].isdigit():
            page = int(cols[0])
            if int(cols[12]) >= ppi[page]:
                ppi[page] = int(cols[12])
                pixels[page] = max(int(cols[3]), int(cols[4]))
    return ppi, pixels


# ---------------------------------------------------------------- candidates

def top_pairs(matrix, k, minimum):
    pairs = set()
    n = len(matrix)
    active = np.flatnonzero(matrix.any(1))
    if len(active) < 2:
        return pairs
    sub = matrix[active]
    for start in range(0, len(active), 512):
        sims = sub[start:start + 512] @ sub.T
        for row, sim in enumerate(sims):
            sim[start + row] = -1
            best = np.argpartition(-sim, min(k, len(sim) - 1))[:k]
            a = int(active[start + row])
            for j in best:
                if sim[j] >= minimum:
                    b = int(active[j])
                    pairs.add((min(a, b), max(a, b)))
    return pairs


def visual_matrix(work, pages):
    sample = []
    rng = np.random.default_rng(7)
    for page in range(1, pages + 1):
        desc = load_feat(work, page)["desc"]
        if len(desc):
            sample.append(desc[rng.permutation(len(desc))[:150]])
    if not sample:
        return np.zeros((pages, VOCAB), np.float32)
    data = np.concatenate(sample).astype(np.float32)
    data = data[rng.permutation(len(data))[:150000]]
    k = min(VOCAB, max(2, len(data) // 20))
    criteria = (cv2.TERM_CRITERIA_MAX_ITER, 8, 0)
    _, _, centers = cv2.kmeans(data, k, None, criteria, 1, cv2.KMEANS_PP_CENTERS)
    c2 = (centers ** 2).sum(1)
    hist = np.zeros((pages, k), np.float32)
    for page in range(1, pages + 1):
        desc = load_feat(work, page)["desc"].astype(np.float32)
        if len(desc):
            words = np.argmin(c2 - 2 * desc @ centers.T, axis=1)
            hist[page - 1] = np.bincount(words, minlength=k)
    return tfidf(hist)


# ---------------------------------------------------------------- verification

def ink_cover(ink_x, ink_y, h_y_to_x):
    """Share of X's ink that lands on Y's ink once Y is aligned onto X."""
    total = int((ink_x > 0).sum())
    if total == 0:
        return 0.0
    height, width = ink_x.shape
    warped = cv2.warpPerspective(ink_y, h_y_to_x, (width, height), flags=cv2.INTER_LINEAR)
    size = 2 * INK_TOLERANCE_PX + 1
    near = cv2.dilate(warped, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    return 1.0 - int(((ink_x > 0) & (near < 64)).sum()) / total


def hull_share(points, ink):
    ys, xs = np.nonzero(ink)
    if len(xs) == 0 or len(points) < 3:
        return 0.0
    box = max(1.0, float(np.ptp(xs)) * float(np.ptp(ys)))
    return min(1.0, cv2.contourArea(cv2.convexHull(points)) / box)


def homography_ok(h, shape):
    if h is None or not np.isfinite(h).all() or abs(h[2, 2]) < 1e-9:
        return False, 0.0
    h = h / h[2, 2]
    det = float(np.linalg.det(h[:2, :2]))
    if det <= 0:
        return False, 0.0
    scale = math.sqrt(det)
    rows, cols = shape
    corners = np.float32([[0, 0], [cols, 0], [cols, rows], [0, rows]]).reshape(-1, 1, 2)
    quad = cv2.perspectiveTransform(corners, h)
    return (0.15 < scale < 7 and cv2.isContourConvex(quad)), scale


def verify_pair(job):
    work, a, b, text_sim = job
    result = {"a": a, "b": b, "text": round(text_sim, 3), "inliers": 0, "cover_a": 0.0,
              "cover_b": 0.0, "hull_a": 0.0, "hull_b": 0.0, "scale": 0.0}
    fa, fb = load_feat(work, a), load_feat(work, b)
    da, db = fa["desc"], fb["desc"]
    if min(len(da), len(db)) < MIN_KEYPOINTS:
        return result
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    good = [m[0] for m in matcher.knnMatch(da.astype(np.float32), db.astype(np.float32), k=2)
            if len(m) == 2 and m[0].distance < RATIO * m[1].distance]
    if len(good) < MIN_INLIERS:
        return result
    pa = fa["pts"][[m.queryIdx for m in good]]
    pb = fb["pts"][[m.trainIdx for m in good]]
    h, mask = cv2.findHomography(pa, pb, cv2.USAC_MAGSAC, RANSAC_PX, maxIters=4000, confidence=0.999)
    if mask is None:
        return result
    inliers = mask.ravel().astype(bool)
    result["inliers"] = int(inliers.sum())
    valid, scale = homography_ok(h, tuple(fa["shape"]))
    if result["inliers"] < MIN_INLIERS or not valid:
        return result
    pages = Path(work) / "pages"
    ink_a = ink_mask(cv2.imread(str(pages / f"{a:06d}.png"), cv2.IMREAD_GRAYSCALE))
    ink_b = ink_mask(cv2.imread(str(pages / f"{b:06d}.png"), cv2.IMREAD_GRAYSCALE))
    result.update(
        scale=round(scale, 3),
        cover_a=round(ink_cover(ink_a, ink_b, np.linalg.inv(h)), 4),
        cover_b=round(ink_cover(ink_b, ink_a, h), 4),
        hull_a=round(hull_share(pa[inliers], ink_a), 3),
        hull_b=round(hull_share(pb[inliers], ink_b), 3),
    )
    return result


# ---------------------------------------------------------------- decisions

def decide(pairs, pages, quality, force_keep, force_remove):
    """Return {removed page: page that covers it} and the pairs left for human review."""
    covers = {p: set() for p in range(1, pages + 1)}
    review = []
    for pair in pairs:
        a, b = pair["a"], pair["b"]
        strong = pair["inliers"] >= MIN_INLIERS
        gap = pair["cover_a"] - pair["cover_b"]
        a_gone = (strong and pair["cover_a"] >= AUTO_COVER and pair["hull_a"] >= AUTO_HULL
                  and gap >= -MUTUAL_MARGIN)
        b_gone = (strong and pair["cover_b"] >= AUTO_COVER and pair["hull_b"] >= AUTO_HULL
                  and gap <= MUTUAL_MARGIN)
        if strong:
            # Pixels per letter on b relative to a. Scale is measured on the renders, which
            # all share one long edge, so it converts through each page's native size.
            size_a, size_b = quality[a][2], quality[b][2]
            detail = pair["scale"] * (size_b / size_a if size_a and size_b else 1)
            a_gone = a_gone and detail > 1 / DETAIL_GAP
            b_gone = b_gone and detail < DETAIL_GAP
        if a_gone:
            covers[a].add(b)
        if b_gone:
            covers[b].add(a)
        if not (a_gone or b_gone):
            visual = strong and max(pair["cover_a"], pair["cover_b"]) >= REVIEW_COVER
            if visual or pair["text"] >= REVIEW_TEXT:
                review.append(pair)
    force_remove = force_remove - force_keep
    for page in force_keep:
        covers[page] = set()
    # A page removed by hand cannot stand in for another page, so no content goes with it.
    kept = {p for p, c in covers.items() if not c} - force_remove
    removed = {}
    pending = set(covers) - kept - force_remove
    while pending:
        progress = False
        for page in sorted(pending):
            hits = covers[page] & kept
            if hits:
                removed[page] = max(hits, key=lambda p: (quality[p], -p))
                pending.discard(page)
                progress = True
        if not progress:
            best = max(pending, key=lambda p: (quality[p], -p))
            kept.add(best)
            pending.discard(best)
    for page in force_remove:
        removed[page] = 0
    done = set(removed)
    review = [p for p in review if not ({p["a"], p["b"]} & done)]
    return removed, review


# ---------------------------------------------------------------- output

def write_pdfs(pdf, out_dir, stem, removed):
    gone = sorted(removed)
    with pikepdf.open(pdf) as doc:
        if gone:
            with pikepdf.new() as extra:
                for page in gone:
                    extra.pages.append(doc.pages[page - 1])
                extra.save(out_dir / f"{stem}.removed.pdf")
        for page in reversed(gone):
            del doc.pages[page - 1]
        doc.remove_unreferenced_resources()
        doc.save(out_dir / f"{stem}.clean.pdf")


def thumb(work, page, folder):
    target = folder / f"{page:06d}.jpg"
    if not target.exists():
        gray = cv2.imread(str(Path(work) / "pages" / f"{page:06d}.png"), cv2.IMREAD_GRAYSCALE)
        scale = 620 / gray.shape[1]
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(target), small, [cv2.IMWRITE_JPEG_QUALITY, 72])
    return f"{folder.name}/{target.name}"


REPORT_STYLE = """
summary{margin:36px 0 12px;font-size:24px;font-weight:700;color:var(--accent);cursor:pointer}
.how{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:16px 0 0}
"""

def card(work, folder, title, left, right, pair, ppi):
    def side(page, label):
        src = thumb(work, page, folder)
        return (f'<figure><a href="{src}" target="_blank"><img loading="lazy" src="{src}" alt="עמוד {page}"></a>'
                f'<figcaption>{label} · עמוד {page}{f" · {ppi[page]} ppi" if ppi[page] else ""}</figcaption></figure>')
    facts = ""
    if pair:
        facts = (f'<p class="facts">התאמת דיו: {pair["cover_a"]:.0%} / {pair["cover_b"]:.0%}'
                 f' · נקודות תואמות: {pair["inliers"]} · דמיון טקסט: {pair["text"]:.0%}</p>')
    return (f'<article><h3>{html.escape(title)}</h3>'
            f'<div class="pair">{side(*left)}{side(*right)}</div>{facts}</article>')


def write_report(out_dir, work, source, pages, removed, review, by_pair, ppi, blank):
    folder = out_dir / "report_files"
    folder.mkdir(exist_ok=True)
    cards = []
    for page, keeper in sorted(removed.items()):
        if keeper:
            pair = by_pair.get((min(page, keeper), max(page, keeper)))
            cards.append(card(work, folder, f"עמוד {page} הוסר, כי כל התוכן שלו נמצא בעמוד {keeper}",
                              (page, "הוסר"), (keeper, "נשאר"), pair, ppi))
    review_cards = [card(work, folder, f"עמודים {p['a']} ו-{p['b']}: זוג מסופק",
                         (p["a"], "עמוד א"), (p["b"], "עמוד ב"), p, ppi)
                    for p in review]
    body = "".join(cards) or "<p>לא הוסרו עמודים.</p>"
    review_body = "".join(review_cards) or "<p>אין מקרים גבוליים.</p>"
    page_html = f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>דוח כפילויות</title><style>
:root{{--bg:#f6f6f3;--card:#fff;--ink:#1e2226;--soft:#5d6670;--line:#dedfd9;--accent:#1f6f5c;--on:#fff;--change:#b5541c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16191c;--card:#20252a;--ink:#eceff1;--soft:#a5afb8;--line:#343b42;--accent:#6fcdb4;--on:#10201b;--change:#f0a35e}}}}
*{{box-sizing:border-box}}body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);font:18px/1.6 "Noto Sans Hebrew",Arial,sans-serif}}
main{{max-width:1320px;margin:auto}}h1{{font-size:30px;margin:0 0 4px}}h2{{margin:36px 0 12px;font-size:24px;color:var(--accent)}}
h3{{margin:0 0 12px;font-size:19px}}.sum{{color:var(--soft)}}article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}}
.pair{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}figure{{margin:0}}img{{width:100%;height:auto;display:block;border:1px solid var(--line);border-radius:6px;background:#fff}}
figcaption,.facts{{color:var(--soft);font-size:16px;margin:8px 0 0}}{REPORT_STYLE}</style></head>
<body><main>
<h1>דוח כפילויות</h1><p class="sum">{html.escape(source.name)} · {pages} עמודים · הוסרו {len(removed)} · נשארו {pages - len(removed)} · לבדיקה {len(review)} · עמודים כמעט ריקים שלא הושוו: {blank}</p>
<p class="how">הדוח הזה לעיון בלבד. ההחלטות על זוגות מסופקים מתקבלות בחלון של AAG - PDF Suite.</p>
<h2>מקרים גבוליים לבדיקה ({len(review)})</h2>{review_body}
<details><summary>עמודים שהוסרו ({len(removed)}), לעיון בלבד</summary>{body}</details>
</main></body></html>"""
    (out_dir / "report.html").write_text(page_html, "utf-8")


def write_summary(out_dir, work, source, pages, removed, review, ppi, blank):
    """The window reads this file: counts, and the doubtful pairs with their images."""
    folder = out_dir / "report_files"
    pairs = [{"a": p["a"], "b": p["b"], "image_a": thumb(work, p["a"], folder), "image_b": thumb(work, p["b"], folder),
              "ppi_a": ppi[p["a"]], "ppi_b": ppi[p["b"]], "cover_a": p["cover_a"], "cover_b": p["cover_b"],
              "inliers": p["inliers"], "text": p["text"]} for p in review]
    summary = {"source": str(source), "pages": pages, "removed": len(removed), "kept": pages - len(removed),
               "blank": blank, "clean": f"{source.stem}.clean.pdf", "review": pairs}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")


def write_csv(out_dir, removed, review):
    with open(out_dir / "decisions.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["decision", "page", "other_page"])
        for page, keeper in sorted(removed.items()):
            writer.writerow(["removed", page, keeper or ""])
        for pair in review:
            writer.writerow(["review", pair["a"], pair["b"]])


# ---------------------------------------------------------------- main

def page_list(value):
    pages = set()
    for part in filter(None, (value or "").split(",")):
        first, _, last = part.partition("-")
        pages.update(range(int(first), int(last or first) + 1))
    return pages


def main():
    parser = argparse.ArgumentParser(description="OCR a scanned PDF and remove repeated pages.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--out", type=Path, help="output folder (default: <name>-dedup beside the input)")
    parser.add_argument("--lang", default="heb+eng")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--no-ocr", action="store_true", help="compare only, without adding a text layer")
    parser.add_argument("--deskew", action="store_true", help="straighten pages (re-encodes page images)")
    parser.add_argument("--keep", help="pages that must stay, e.g. 12,40-42")
    parser.add_argument("--remove", help="pages to remove in addition, e.g. 7,9")
    parser.add_argument("--reverify", action="store_true", help="ignore cached page comparisons")
    parser.add_argument("--managed", action="store_true", help="run under the window: own process group, clean stop")
    args = parser.parse_args()

    if args.managed:
        try:
            os.setsid()
        except OSError:
            pass
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    source = args.pdf.resolve()
    stem = source.stem
    out_dir = (args.out or source.with_name(f"{stem}-dedup")).resolve()
    work = out_dir / ".work"
    (work / "pages").mkdir(parents=True, exist_ok=True)
    (work / "feat").mkdir(exist_ok=True)
    stamp = {"cache": CACHE_VERSION, "render": RENDER_LONG_EDGE, "size": source.stat().st_size,
             "mtime": int(source.stat().st_mtime), "lang": args.lang, "deskew": args.deskew, "ocr": not args.no_ocr}
    stamp_file = work / "stamp.json"
    if stamp_file.exists() and json.loads(stamp_file.read_text()) != stamp:
        sys.exit(f"The cache in {work} belongs to other settings. Use a different --out folder.")
    stamp_file.write_text(json.dumps(stamp))

    started = time.time()
    # Work from a private copy: the source may sit on a disk that helper tools are not allowed to read.
    private = work / "source.pdf"
    if not (work / "ocr.pdf").exists():
        progress("copy")
        shutil.copy2(source, private)
    pdf = ocr_stage(private, work, args.lang, args.jobs, args.deskew, args.no_ocr)
    private.unlink(missing_ok=True)
    pages = page_count(pdf)
    log(f"{pages} pages")

    log("Rendering pages and computing features")
    jobs = [(str(pdf), str(work), a, min(a + CHUNK - 1, pages)) for a in range(1, pages + 1, CHUNK)]
    with mp.Pool(args.jobs, worker_init) as pool:
        for index, _ in enumerate(pool.imap_unordered(features_chunk, jobs), 1):
            progress("features", min(index * CHUNK, pages), pages)
            if index % 10 == 0:
                log(f"  features: {min(index * CHUNK, pages)}/{pages}")

    pairs_file = work / "pairs.json"
    if pairs_file.exists() and not args.reverify:
        pairs = json.loads(pairs_file.read_text())
        log("Comparisons: using the cached result")
    else:
        texts, lengths = text_matrix(page_texts(pdf, work, pages))
        candidates = top_pairs(texts, TOP_K, 0.15)
        text_count = len(candidates)
        candidates |= top_pairs(visual_matrix(work, pages), TOP_K, 0.0)
        for page in range(pages):
            for step in range(1, NEIGHBOURS + 1):
                if page + step < pages:
                    candidates.add((page, page + step))
        log(f"Candidates: {len(candidates)} pairs ({text_count} from text)")
        tasks = [(str(work), a + 1, b + 1, float(texts[a] @ texts[b])) for a, b in sorted(candidates)]
        pairs = []
        with mp.Pool(args.jobs, worker_init) as pool:
            for index, item in enumerate(pool.imap_unordered(verify_pair, tasks, chunksize=8), 1):
                pairs.append(item)
                if index % 50 == 0:
                    progress("compare", index, len(tasks))
                if index % 1000 == 0:
                    log(f"  compared: {index}/{len(tasks)}")
        pairs.sort(key=lambda p: (p["a"], p["b"]))
        pairs_file.write_text(json.dumps(pairs))

    ppi, pixels = image_ppi(pdf, pages)
    quality = {p: (round(ppi[p] / 10), float(load_feat(work, p)["sharp"]), pixels[p])
               for p in range(1, pages + 1)}
    blank = sum(1 for p in range(1, pages + 1) if len(load_feat(work, p)["pts"]) < MIN_KEYPOINTS)
    removed, review = decide(pairs, pages, quality, page_list(args.keep), page_list(args.remove))
    by_pair = {(p["a"], p["b"]): p for p in pairs}

    progress("write")
    write_pdfs(pdf, out_dir, stem, removed)
    write_report(out_dir, work, source, pages, removed, review, by_pair, ppi, blank)
    write_csv(out_dir, removed, review)
    write_summary(out_dir, work, source, pages, removed, review, ppi, blank)
    log(f"Done in {time.time() - started:.0f}s: removed {len(removed)}, review {len(review)}, kept {pages - len(removed)}")
    log(f"Output: {out_dir}")


if __name__ == "__main__":
    main()
