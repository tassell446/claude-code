#!/usr/bin/env python3
"""
Convert Hogskoleprovet PDFs to Markdown using Marker.

Scans input/{YYYY-MM-DD}/ for PDF files, skips answer keys (facit.pdf),
and runs Marker's convert_single.py on each.

Requires Marker to be installed (not available in all environments).

Usage:
    python scripts/convert_pdfs.py              # Convert all PDFs
    python scripts/convert_pdfs.py --dry-run    # Preview what would be converted
    python scripts/convert_pdfs.py --exam 2025-04-05  # Convert one exam date only
    python scripts/convert_pdfs.py --force      # Re-convert even if output exists
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"

SKIP_FILENAMES = {"facit.pdf"}


def find_pdfs(exam_filter=None):
    """Scan input/ for PDF files to convert. Returns list of (pdf_path, exam_date, stem)."""
    pdfs = []
    if not INPUT_DIR.exists():
        return pdfs

    for date_dir in sorted(INPUT_DIR.iterdir()):
        if not date_dir.is_dir() or not re.match(r"\d{4}-\d{2}-\d{2}$", date_dir.name):
            continue
        if exam_filter and date_dir.name != exam_filter:
            continue

        for pdf in sorted(date_dir.glob("*.pdf")):
            if pdf.name.lower() in SKIP_FILENAMES:
                continue
            pdfs.append((pdf, date_dir.name, pdf.stem))

    return pdfs


def is_already_converted(exam_date, stem):
    """Check if Marker output already exists for this PDF."""
    expected_md = OUTPUT_DIR / exam_date / stem / f"{stem}.md"
    return expected_md.exists()


def convert_pdf(pdf_path, exam_date, stem, dry_run=False):
    """Run Marker on a single PDF."""
    out_dir = OUTPUT_DIR / exam_date
    expected_md = out_dir / stem / f"{stem}.md"

    if dry_run:
        print(f"  [DRY RUN] Would convert: {pdf_path.relative_to(ROOT)}")
        print(f"            Output: {expected_md.relative_to(ROOT)}")
        return True

    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "marker.scripts.convert_single",
        str(pdf_path),
        "--output_dir", str(out_dir),
    ]

    print(f"  Converting: {pdf_path.relative_to(ROOT)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout:
            print(f"    {result.stdout.strip()}")
        if expected_md.exists():
            print(f"    Output: {expected_md.relative_to(ROOT)}")
            return True
        else:
            print(f"    WARNING: Expected output not found at {expected_md.relative_to(ROOT)}")
            return False
    except subprocess.CalledProcessError as e:
        print(f"    ERROR: Marker failed (exit code {e.returncode})")
        if e.stderr:
            print(f"    {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("    ERROR: Marker not found. Install with: pip install marker-pdf")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Convert Hogskoleprovet PDFs to Markdown using Marker"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be converted without actually converting"
    )
    parser.add_argument(
        "--exam",
        help="Only convert PDFs for this exam date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-convert even if output already exists"
    )

    args = parser.parse_args()

    pdfs = find_pdfs(exam_filter=args.exam)

    if not pdfs:
        print("No PDFs found in input/")
        print("Expected structure: input/{YYYY-MM-DD}/provpass-N-{type}.pdf")
        sys.exit(1)

    # Filter out already-converted unless --force
    if not args.force:
        to_convert = [(p, d, s) for p, d, s in pdfs if not is_already_converted(d, s)]
        skipped = len(pdfs) - len(to_convert)
        if skipped:
            print(f"Skipping {skipped} already-converted PDF(s) (use --force to re-convert)")
    else:
        to_convert = pdfs

    if not to_convert:
        print("All PDFs already converted. Use --force to re-convert.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Converting {len(to_convert)} PDF(s)...\n")

    success = 0
    failed = 0
    for pdf_path, exam_date, stem in to_convert:
        if convert_pdf(pdf_path, exam_date, stem, dry_run=args.dry_run):
            success += 1
        else:
            failed += 1

    print(f"\nDone: {success} converted, {failed} failed")


if __name__ == "__main__":
    main()
