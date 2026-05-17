"""Download PSX filing PDFs and extract their text content.

Two-stage extraction pipeline:

  1. opendataloader-pdf (Java-backed) reads the PDF text layer.
     Fast, free, deterministic. Works for digital PDFs (~50% of PSX filings:
     board meeting notices, dividend declarations, regulatory letters).

  2. Vision-model fallback. When stage 1 returns no text (scanned PDFs:
     quarterly results, newspaper publications, signed letters), we
     rasterize the pages with PyMuPDF and send them to whichever vision
     model the user's LLM_PROVIDER points to (OpenAI gpt-4o-mini or
     Anthropic claude-sonnet by default). The model returns a structured
     markdown summary of the financial filing.

Both stages cache their output to .cache/extracted/<doc_id>.md keyed by
the immutable PSX document ID. Re-runs are free.
"""

import glob
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parent.parent
PDF_CACHE = ROOT / ".cache" / "pdfs"
MD_CACHE = ROOT / ".cache" / "extracted"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}

DOC_ID_RE = re.compile(r"/document/(\d+)\.pdf", re.IGNORECASE)


def doc_id_from_url(url: str):
    m = DOC_ID_RE.search(url or "")
    return m.group(1) if m else None


def _setup_local_jdk():
    """Always prefer .jdk/jdk-* over the system `java`.

    On macOS, `shutil.which("java")` returns the /usr/bin/java stub even when
    no JDK is installed -- the stub just prints "Unable to locate a Java
    Runtime" and exits 1. So we must explicitly prepend our local JDK to
    PATH whenever it exists, rather than only as a fallback.
    """
    candidates = sorted(glob.glob(str(ROOT / ".jdk" / "jdk-*")))
    if candidates:
        jdk_root = candidates[-1]
        java_home = os.path.join(jdk_root, "Contents", "Home")
        if not os.path.isdir(java_home):
            java_home = jdk_root
        bin_dir = os.path.join(java_home, "bin")
        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if bin_dir not in path_parts:
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["JAVA_HOME"] = java_home
        return
    if shutil.which("java"):
        return


def _ensure_dirs():
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    MD_CACHE.mkdir(parents=True, exist_ok=True)


def _download_pdf(url: str, doc_id: str) -> Path:
    out = PDF_CACHE / f"{doc_id}.pdf"
    if out.exists() and out.stat().st_size > 0:
        return out
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    out.write_bytes(resp.content)
    return out


def _extracted_path(doc_id: str) -> Path:
    return MD_CACHE / f"{doc_id}.md"


def _read_extracted(doc_id: str) -> str:
    p = _extracted_path(doc_id)
    if not p.exists():
        return ""
    return p.read_text(errors="replace")


def _strip_markdown(md: str) -> str:
    """Drop image refs / HTML comments and collapse blank lines."""
    lines = []
    for line in md.splitlines():
        if re.match(r"^\s*!\[.*?\]\(.*?\)\s*$", line):
            continue
        if re.match(r"^\s*<!--.*?-->\s*$", line):
            continue
        lines.append(line.rstrip())
    out, prev_blank = [], False
    for line in lines:
        if not line.strip():
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(line)
    return "\n".join(out).strip()


def _run_opendataloader(pdf_paths: list, output_dir: Path):
    """Convert a batch of PDFs to markdown via the opendataloader Python wrapper."""
    if not pdf_paths:
        return
    _setup_local_jdk()
    try:
        import opendataloader_pdf
    except ImportError:
        raise RuntimeError(
            "opendataloader-pdf is not installed. Run: pip install opendataloader-pdf"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    opendataloader_pdf.convert(
        input_path=[str(p) for p in pdf_paths],
        output_dir=str(output_dir),
        format="markdown",
    )


VISION_MARKER = "<!-- extracted-via: vision -->"

VISION_INSTRUCTION = (
    "These page images are an official corporate filing submitted to the "
    "Pakistan Stock Exchange (PSX) by a listed company. Extract the key "
    "financial facts as concise markdown bullet points. Always include "
    "(when present): filing type, reporting period, revenue / net income / "
    "EPS / dividend per share / cash position, year-on-year changes, "
    "any auditor qualifications, regulatory issues, or forward guidance. "
    "Use specific numbers in PKR (or millions of PKR if that's how the "
    "filing presents them). Do NOT invent figures. If a page has no useful "
    "financial information (e.g. cover page, signatures), skip it. Output "
    "must be plain markdown without any preamble like 'here is the summary'."
)

VISION_MAX_PAGES = 8
VISION_RENDER_DPI = 110


def _rasterize_pdf(pdf_path: Path, max_pages: int, dpi: int) -> list:
    """Return a list of PNG page images as bytes."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            raise RuntimeError(
                "PyMuPDF is not installed. Run: pip install PyMuPDF"
            )
    images = []
    with pymupdf.open(str(pdf_path)) as doc:
        n = min(len(doc), max_pages)
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        for i in range(n):
            pix = doc[i].get_pixmap(matrix=matrix, alpha=False)
            images.append(pix.tobytes("png"))
    return images


def _vision_extract_pdf(pdf_path: Path) -> str:
    """OCR-via-vision fallback. Returns markdown summary or empty on failure."""
    try:
        from .llm import vision_extract
    except ImportError:
        return ""
    try:
        images = _rasterize_pdf(pdf_path, VISION_MAX_PAGES, VISION_RENDER_DPI)
    except Exception as e:
        return f"{VISION_MARKER}\n<!-- rasterization failed: {e} -->"
    if not images:
        return ""
    try:
        body = vision_extract(images, VISION_INSTRUCTION, max_tokens=1500)
    except Exception as e:
        return f"{VISION_MARKER}\n<!-- vision call failed: {e} -->"
    if not body.strip():
        return ""
    return f"{VISION_MARKER}\n{body.strip()}"


def _needs_vision_fallback(doc_id: str) -> bool:
    """A doc needs vision if opendataloader produced no usable text AND we
    haven't already tried vision on it (no VISION_MARKER in cache file)."""
    p = _extracted_path(doc_id)
    if not p.exists():
        return True
    raw = p.read_text(errors="replace")
    if VISION_MARKER in raw:
        return False
    return not _strip_markdown(raw)


def extract_many(specs: Iterable[dict], max_chars: int = 2000,
                 use_vision_fallback: bool = True) -> dict:
    """Extract markdown content for a batch of PDFs.

    Args:
        specs: iterable of dicts with at least {"pdf_url": str}.
               Optional "doc_id" override.
        max_chars: truncate each extracted markdown body to N characters.
        use_vision_fallback: when True (default), scanned PDFs that produce
            no text from opendataloader are re-extracted by sending page
            images to the configured vision model.

    Returns:
        {doc_id: {"text": str, "char_count": int, "from_cache": bool,
                  "method": "text-layer"|"vision"|"failed",
                  "error": str|None}}
    """
    _ensure_dirs()

    by_doc: dict = {}
    for spec in specs:
        url = spec.get("pdf_url")
        if not url:
            continue
        doc_id = spec.get("doc_id") or doc_id_from_url(url)
        if not doc_id:
            continue
        by_doc[doc_id] = url

    if not by_doc:
        return {}

    cached_docs: set = set()
    needs_extract: list = []
    for doc_id, url in by_doc.items():
        if _extracted_path(doc_id).exists():
            cached_docs.add(doc_id)
        else:
            needs_extract.append((doc_id, url))

    fresh_pdfs: list = []
    download_errors: dict = {}
    for doc_id, url in needs_extract:
        try:
            fresh_pdfs.append(_download_pdf(url, doc_id))
        except Exception as e:
            download_errors[doc_id] = f"download failed: {e}"

    if fresh_pdfs:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_out = Path(tmp)
            try:
                _run_opendataloader(fresh_pdfs, tmp_out)
            except subprocess.CalledProcessError as e:
                for p in fresh_pdfs:
                    download_errors.setdefault(
                        p.stem, f"opendataloader failed (exit {e.returncode})"
                    )
            except Exception as e:
                for p in fresh_pdfs:
                    download_errors.setdefault(p.stem, f"opendataloader failed: {e}")
            else:
                for p in fresh_pdfs:
                    src = tmp_out / f"{p.stem}.md"
                    if src.exists():
                        shutil.copy(src, _extracted_path(p.stem))

    if use_vision_fallback:
        for doc_id in by_doc:
            if doc_id in download_errors:
                continue
            if not _needs_vision_fallback(doc_id):
                continue
            pdf_path = PDF_CACHE / f"{doc_id}.pdf"
            if not pdf_path.exists():
                continue
            summary = _vision_extract_pdf(pdf_path)
            if summary:
                _extracted_path(doc_id).write_text(summary)

    out: dict = {}
    for doc_id, url in by_doc.items():
        if doc_id in download_errors and not _extracted_path(doc_id).exists():
            out[doc_id] = {
                "text": "",
                "char_count": 0,
                "from_cache": False,
                "method": "failed",
                "error": download_errors[doc_id],
            }
            continue
        raw = _read_extracted(doc_id)
        method = "vision" if VISION_MARKER in raw else "text-layer"
        text = _strip_markdown(raw)
        truncated = text[:max_chars]
        if len(text) > max_chars:
            truncated += "\n... [truncated]"
        out[doc_id] = {
            "text": truncated,
            "char_count": len(text),
            "from_cache": doc_id in cached_docs,
            "method": method,
            "error": None if text else "no extractable text (likely scanned PDF)",
        }
    return out


def extract_one(pdf_url: str, max_chars: int = 2000) -> dict:
    doc_id = doc_id_from_url(pdf_url)
    if not doc_id:
        return {"text": "", "char_count": 0, "from_cache": False,
                "error": "could not parse doc id from URL"}
    return extract_many([{"pdf_url": pdf_url, "doc_id": doc_id}], max_chars=max_chars)[doc_id]


if __name__ == "__main__":
    import json
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    urls = sys.argv[1:] or [
        "https://dps.psx.com.pk/download/document/273932.pdf",  # text
        "https://dps.psx.com.pk/download/document/275225.pdf",  # text
        "https://dps.psx.com.pk/download/document/275000.pdf",  # scanned -> vision
    ]
    res = extract_many([{"pdf_url": u} for u in urls], max_chars=800)
    for doc_id, info in res.items():
        print(f"\n=== {doc_id} (method={info.get('method')}, "
              f"chars={info.get('char_count')}, cached={info.get('from_cache')}) ===")
        if info.get("error"):
            print(f"  error: {info['error']}")
        print(info["text"] or "(empty)")
