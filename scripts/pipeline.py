#!/usr/bin/env python3
"""
Högskoleprovet PDF extraction pipeline orchestrator.

Phases: discover → parse → merge overrides → copy images → write final → print summary

Usage:
    python scripts/pipeline.py                              # Process all exams
    python scripts/pipeline.py --status                     # Show status summary
    python scripts/pipeline.py --status --detail KEY        # Show flagged details
    python scripts/pipeline.py --exam 2024-10-20 --provpass 1  # Process one exam
    python scripts/pipeline.py --merge-only                 # Re-merge without re-parsing
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
PARSED_DIR = DATA_DIR / "parsed"
OVERRIDES_DIR = DATA_DIR / "overrides"
FINAL_DIR = DATA_DIR / "final"
IMAGES_DIR = DATA_DIR / "images"
STATUS_FILE = DATA_DIR / "status.json"

# Add scripts/ to path so we can import the parser
sys.path.insert(0, str(ROOT / "scripts"))
from parse_hogskoleprovet import parse_markdown


def make_key(exam_date, provpass_num):
    """Build a canonical key like '2024-10-20_provpass-1'."""
    return f"{exam_date}_provpass-{provpass_num}"


def discover():
    """Scan output/ for Marker results. Returns list of dicts with metadata."""
    entries = []
    if not OUTPUT_DIR.exists():
        return entries

    for date_dir in sorted(OUTPUT_DIR.iterdir()):
        if not date_dir.is_dir() or not re.match(r"\d{4}-\d{2}-\d{2}$", date_dir.name):
            continue
        exam_date = date_dir.name

        for provpass_dir in sorted(date_dir.iterdir()):
            if not provpass_dir.is_dir():
                continue

            # Find the markdown file
            md_files = list(provpass_dir.glob("*.md"))
            if not md_files:
                continue
            md_file = md_files[0]

            # Extract provpass number from directory name (e.g., provpass-1-kvant → 1)
            m = re.search(r"provpass-(\d+)", provpass_dir.name)
            if not m:
                continue
            provpass_num = int(m.group(1))

            if "kvant" in provpass_dir.name:
                provpass_type = "kvant"
            elif "verb" in provpass_dir.name:
                provpass_type = "verbal"
            else:
                provpass_type = "unknown"

            key = make_key(exam_date, provpass_num)
            entries.append({
                "key": key,
                "exam_date": exam_date,
                "provpass": provpass_num,
                "provpass_type": provpass_type,
                "provpass_dir": provpass_dir,
                "md_file": md_file,
            })

    return entries


def phase_parse(entry):
    """Parse markdown into structured JSON, write to data/parsed/."""
    PARSED_DIR.mkdir(parents=True, exist_ok=True)

    md_text = entry["md_file"].read_text(encoding="utf-8")
    result = parse_markdown(md_text, entry["exam_date"], entry["provpass"],
                            provpass_type=entry.get("provpass_type", "kvant"))

    out_path = PARSED_DIR / f"{entry['key']}.json"
    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def phase_merge_overrides(entry, parsed_data):
    """Merge manual overrides into parsed data. Returns merged data."""
    override_file = OVERRIDES_DIR / f"{entry['key']}.overrides.json"
    if not override_file.exists():
        return parsed_data

    overrides = json.loads(override_file.read_text(encoding="utf-8"))

    for q in parsed_data["questions"]:
        q_key = str(q["question_number"])
        if q_key not in overrides:
            continue
        override = overrides[q_key]

        # Shallow-merge: override individual fields
        for field in ("question_text", "options", "images", "flags",
                      "kvantitet_I", "kvantitet_II", "statements"):
            if field in override:
                q[field] = override[field]

        # Mark as reviewed if specified
        if override.get("reviewed"):
            q["reviewed"] = True

    return parsed_data


def phase_copy_images(entry):
    """Copy images from Marker output to data/images/."""
    src_dir = entry["provpass_dir"]
    dst_dir = IMAGES_DIR / entry["key"]
    dst_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for img in src_dir.glob("*.jpeg"):
        dst = dst_dir / img.name
        # Skip if destination exists and has same size
        if dst.exists() and dst.stat().st_size == img.stat().st_size:
            continue
        shutil.copy2(img, dst)
        copied += 1

    # Also copy png files if any
    for img in src_dir.glob("*.png"):
        dst = dst_dir / img.name
        if dst.exists() and dst.stat().st_size == img.stat().st_size:
            continue
        shutil.copy2(img, dst)
        copied += 1

    return copied


def rewrite_image_paths(entry, data):
    """Rewrite bare image filenames to paths relative to data/.

    Transforms e.g. '_page_2_Picture_8.jpeg' into
    'images/2024-10-20_provpass-1/_page_2_Picture_8.jpeg'
    in both images[].filename and inline markdown references.
    """
    prefix = f"images/{entry['key']}/"

    for q in data.get("questions", []):
        # Rewrite images[].filename
        for img in q.get("images", []):
            fn = img.get("filename", "")
            if fn and not fn.startswith(prefix):
                img["filename"] = prefix + fn

        # Rewrite inline markdown image refs in question_text
        if q.get("question_text"):
            q["question_text"] = re.sub(
                r"!\[([^\]]*)\]\((?!images/)([^)]+)\)",
                rf"![\1]({prefix}\2)",
                q["question_text"],
            )

        # Rewrite inline markdown image refs in options[].text
        for opt in q.get("options", []):
            if opt.get("text"):
                opt["text"] = re.sub(
                    r"!\[([^\]]*)\]\((?!images/)([^)]+)\)",
                    rf"![\1]({prefix}\2)",
                    opt["text"],
                )


def phase_write_final(entry, merged_data):
    """Write merged JSON to data/final/."""
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FINAL_DIR / f"{entry['key']}.json"
    out_path.write_text(
        json.dumps(merged_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out_path


def compute_status(entry):
    """Compute status for an entry: clean / needs_review / not_processed."""
    final_path = FINAL_DIR / f"{entry['key']}.json"
    if not final_path.exists():
        return "not_processed"

    data = json.loads(final_path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])

    for q in questions:
        if q.get("flags") and not q.get("reviewed"):
            return "needs_review"

    return "clean"


def update_status_file(entries):
    """Write data/status.json with current status of all entries."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = {}
    for entry in entries:
        s = compute_status(entry)
        final_path = FINAL_DIR / f"{entry['key']}.json"

        info = {
            "status": s,
            "exam_date": entry["exam_date"],
            "provpass": entry["provpass"],
        }

        if final_path.exists():
            data = json.loads(final_path.read_text(encoding="utf-8"))
            meta = data.get("metadata", {})
            info["total_questions"] = meta.get("total_questions", 0)
            info["questions_with_flags"] = meta.get("questions_with_flags", 0)

            flagged = [q for q in data.get("questions", []) if q.get("flags")]
            reviewed = [q for q in flagged if q.get("reviewed")]
            info["flagged_count"] = len(flagged)
            info["reviewed_count"] = len(reviewed)

        status[entry["key"]] = info

    status_data = {
        "updated_at": datetime.now().isoformat(),
        "exams": status,
    }
    STATUS_FILE.write_text(
        json.dumps(status_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return status


def print_summary(status):
    """Print a human-readable status summary."""
    if not status:
        print("No exams found in output/")
        return

    print(f"\n{'Key':<35} {'Status':<16} {'Questions':<10} {'Flagged':<10} {'Reviewed'}")
    print("-" * 85)

    for key in sorted(status):
        info = status[key]
        s = info["status"]
        total = info.get("total_questions", "-")
        flagged = info.get("flagged_count", "-")
        reviewed = info.get("reviewed_count", "-")

        # Color-code status
        if s == "clean":
            label = "clean"
        elif s == "needs_review":
            label = "NEEDS REVIEW"
        else:
            label = "not processed"

        print(f"  {key:<33} {label:<16} {str(total):<10} {str(flagged):<10} {reviewed}")

    print()


def print_detail(entry):
    """Print detailed flag info for one exam."""
    final_path = FINAL_DIR / f"{entry['key']}.json"
    if not final_path.exists():
        print(f"No final output for {entry['key']}")
        return

    data = json.loads(final_path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])
    flagged = [q for q in questions if q.get("flags")]

    if not flagged:
        print(f"{entry['key']}: No flagged questions")
        return

    print(f"\n{entry['key']}: {len(flagged)} flagged question(s)\n")
    for q in flagged:
        reviewed = q.get("reviewed", False)
        marker = "[REVIEWED]" if reviewed else "[UNREVIEWED]"
        print(f"  Q{q['question_number']} ({q['section']}) {marker}")
        for flag in q["flags"]:
            print(f"    - {flag}")
    print()


def process_entry(entry, merge_only=False):
    """Run the full pipeline for a single entry."""
    key = entry["key"]

    if merge_only:
        # Load existing parsed data
        parsed_path = PARSED_DIR / f"{key}.json"
        if not parsed_path.exists():
            print(f"  {key}: No parsed data found, skipping (run without --merge-only first)")
            return
        parsed_data = json.loads(parsed_path.read_text(encoding="utf-8"))
        print(f"  {key}: loaded parsed data")
    else:
        # Phase 1: Parse
        parsed_data = phase_parse(entry)
        meta = parsed_data["metadata"]
        print(f"  {key}: parsed {meta['total_questions']} questions ({meta['questions_with_flags']} flagged)")

    # Phase 2: Merge overrides
    merged_data = phase_merge_overrides(entry, parsed_data)

    override_file = OVERRIDES_DIR / f"{key}.overrides.json"
    if override_file.exists():
        print(f"  {key}: merged overrides from {override_file.name}")

    if not merge_only:
        # Phase 3: Copy images
        copied = phase_copy_images(entry)
        if copied > 0:
            print(f"  {key}: copied {copied} images")

    # Phase 4: Rewrite image paths for final output
    rewrite_image_paths(entry, merged_data)

    # Phase 5: Write final
    out_path = phase_write_final(entry, merged_data)
    print(f"  {key}: wrote {out_path.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(
        description="Högskoleprovet PDF extraction pipeline"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show status summary of all exams"
    )
    parser.add_argument(
        "--detail",
        help="Show flagged question details for a specific key (use with --status)"
    )
    parser.add_argument(
        "--exam",
        help="Process only this exam date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--provpass", type=int,
        help="Process only this provpass number (use with --exam)"
    )
    parser.add_argument(
        "--merge-only", action="store_true",
        help="Re-merge overrides without re-parsing"
    )

    args = parser.parse_args()

    # Discover all available exams
    entries = discover()

    # Filter if requested
    if args.exam:
        entries = [e for e in entries if e["exam_date"] == args.exam]
    if args.provpass is not None:
        entries = [e for e in entries if e["provpass"] == args.provpass]

    # Status mode
    if args.status:
        if args.detail:
            matching = [e for e in entries if e["key"] == args.detail]
            if not matching:
                print(f"No entry found for key: {args.detail}")
                sys.exit(1)
            print_detail(matching[0])
        else:
            status = update_status_file(entries)
            print_summary(status)
        return

    # Process mode
    if not entries:
        print("No Marker output found in output/")
        print("Expected structure: output/{exam_date}/provpass-N-{type}/*.md")
        sys.exit(1)

    print(f"Processing {len(entries)} exam(s)...")
    for entry in entries:
        process_entry(entry, merge_only=args.merge_only)

    # Update status
    all_entries = discover()
    status = update_status_file(all_entries)
    print_summary(status)


if __name__ == "__main__":
    main()
