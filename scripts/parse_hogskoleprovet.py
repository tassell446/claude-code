#!/usr/bin/env python3
"""
Post-processes Marker markdown output from Högskoleprovet PDFs
into structured JSON question/answer data.

Usage:
    python parse_hogskoleprovet.py <markdown_file> [--output <output.json>]
"""

import json
import re
import sys
import os
from pathlib import Path


# Section definitions based on standard Högskoleprovet structure
SECTIONS_KVANT = {
    "XYZ": {"name": "Matematisk problemlösning", "options": "ABCD"},
    "KVA": {"name": "Kvantitativa jämförelser", "options": "ABCD"},
    "NOG": {"name": "Kvantitativa resonemang", "options": "ABCDE"},
    "DTK": {"name": "Diagram, tabeller och kartor", "options": "ABCD"},
}

SECTIONS_VERBAL = {
    "ORD": {"name": "Ordförståelse", "options": "ABCDE"},
    "LÄS": {"name": "Läsförståelse", "options": "ABCD"},
    "MEK": {"name": "Meningskomplettering", "options": "ABCD"},
}

SECTIONS = {**SECTIONS_KVANT, **SECTIONS_VERBAL}


def determine_section(question_num, section_ranges):
    """Determine which section a question belongs to based on its number."""
    for section, (start, end) in section_ranges.items():
        if start <= question_num <= end:
            return section
    return "UNKNOWN"


def parse_section_ranges(md_text, provpass_type="kvant"):
    """Try to extract section ranges from the table in the markdown."""
    if provpass_type == "verbal":
        ranges = {
            "ORD": (1, 10),
            "LÄS": (11, 30),
            "MEK": (31, 40),
        }
    else:
        # Default ranges for standard Högskoleprovet kvantitativ del
        ranges = {
            "XYZ": (1, 12),
            "KVA": (13, 22),
            "NOG": (23, 28),
            "DTK": (29, 40),
        }

    # Try to parse from table if present
    table_pattern = r'\|\s*(\w+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*[–-]\s*(\d+)\s*\|'
    for match in re.finditer(table_pattern, md_text):
        section = match.group(1).upper()
        start = int(match.group(3))
        end = int(match.group(4))
        if section in SECTIONS:
            ranges[section] = (start, end)

    return ranges


def extract_images(text):
    """Extract image references from markdown text."""
    images = []
    for match in re.finditer(r'!\[([^\]]*)\]\(([^)]+)\)', text):
        images.append({
            "alt_text": match.group(1),
            "filename": match.group(2),
        })
    return images


def clean_option_text(text):
    """Clean up an option's text content."""
    # Remove leading/trailing whitespace
    text = text.strip()
    # Remove markdown list markers, but NOT negative signs before numbers
    # e.g., "- 6" is a list marker, but "-6" is a negative number
    text = re.sub(r'^[-*]\s+', '', text)  # Only strip if followed by whitespace
    # Clean up sup tags that indicate garbled fractions
    text = re.sub(r'<sup>([^<]+)</sup>', r'^\1', text)
    return text


def detect_issues(question):
    """Flag potential extraction issues in a question."""
    flags = []
    full_text = question.get("question_text", "") + " ".join(
        opt.get("text", "") for opt in question.get("options", [])
    )

    # Check for garbled sup tags (indicates broken fraction rendering)
    if re.search(r'<sup>', full_text):
        flags.append("GARBLED_SUPERSCRIPT: Contains <sup> tags suggesting broken fraction/exponent rendering")

    # Check for stray $ signs (broken math)
    if re.search(r'(?<!\$)\$(?!\$)', full_text):
        if not re.search(r'\$[^$]+\$', full_text):  # Not valid inline math
            flags.append("STRAY_DOLLAR_SIGN: Contains stray $ suggesting broken math notation")

    # Check for missing operators between terms (e.g., "4x 4y" should be "4x - 4y")
    if re.search(r'\d+[a-z]\s+\d+[a-z](?!\w)', full_text):
        flags.append("POSSIBLE_MISSING_OPERATOR: Adjacent terms may be missing an operator")

    # Check for mangled equation-like text
    if re.search(r'[a-z]\s*<sup>\d+</sup>\s*=\s*[-+]?\s*<sup>', full_text):
        flags.append("GARBLED_EQUATION: Equation appears mangled")

    # Check for Cyrillic В/С that should be B/C (common OCR issue)
    if re.search(r'[ВС](?=\s)', full_text):
        flags.append("CYRILLIC_LETTERS: May contain Cyrillic В/С instead of Latin B/C")

    # Check if options seem incomplete or garbled
    options = question.get("options", [])
    if options:
        option_texts = [opt.get("text", "") for opt in options]
        # Very short options that aren't numbers might be garbled
        for i, opt_text in enumerate(option_texts):
            cleaned = re.sub(r'[\s\$\\]', '', opt_text)
            if len(cleaned) == 0:
                flags.append(f"EMPTY_OPTION: Option {options[i].get('letter', '?')} appears empty")

    return flags


def split_into_questions(md_text):
    """Split markdown text into individual question blocks."""
    # Pattern to match question numbers in various formats:
    # "1. ", "**1.** ", "- 1. ", "- **1.** ", "## **15.** "
    question_pattern = re.compile(
        r'^(?:[-*]\s*)?(?:#{1,4}\s*)?(?:\*\*)?(\d{1,2})\.(?:\*\*)?\s',
        re.MULTILINE
    )

    matches = list(question_pattern.finditer(md_text))

    if not matches:
        return []

    blocks = []
    for i, match in enumerate(matches):
        q_num = int(match.group(1))
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        block_text = md_text[start:end].strip()
        blocks.append((q_num, block_text))

    return blocks


def normalize_letter(letter):
    """Normalize Cyrillic/Greek lookalikes to Latin A-E."""
    # Map of common lookalikes to Latin letters
    mapping = {
        'А': 'A', 'а': 'A',   # Cyrillic A (U+0410)
        'Б': 'B',              # Cyrillic Be (U+0411) - rare but possible
        'В': 'B',              # Cyrillic Ve (U+0412)
        'С': 'C', 'с': 'C',   # Cyrillic Es (U+0421)
        'Д': 'D',              # Cyrillic De (U+0414)
        'Е': 'E', 'е': 'E',   # Cyrillic Ye (U+0415)
        'Α': 'A',              # Greek Alpha
        'Β': 'B',              # Greek Beta
        'Ε': 'E',              # Greek Epsilon
    }
    return mapping.get(letter, letter)


# All characters that could represent option letters A-E
# Includes Latin A-E, Cyrillic А(0410),В(0412),С(0421),Д(0414),Е(0415), Greek Α,Β,Ε
OPTION_LETTER_CHARS = r'A-EАБВСДЕΑΒΕ'


def parse_options_from_block(block_text, section):
    """Extract answer options from a question block."""
    options = []
    expected_letters = SECTIONS.get(section, {}).get("options", "ABCD")
    found_letters = set()

    # Strategy 1: "- A text" or "  - A  text" (list items, possibly indented)
    # Must NOT match "- A" when it's part of question text like "AD = r/2"
    list_option_pattern = re.compile(
        r'^\s*[-*]\s*(?:\*\*)?([' + OPTION_LETTER_CHARS + r'])(?:\*\*)?\s+'
        r'(.+?)$',
        re.MULTILINE
    )

    # Strategy 2: "A text" at start of line (no list marker)
    bare_option_pattern = re.compile(
        r'^(?:\*\*)?([' + OPTION_LETTER_CHARS + r'])(?:\*\*)?\s+'
        r'(.+?)$',
        re.MULTILINE
    )

    # Strategy 3: Letter on its own line followed by $$...$$ math block
    math_block_pattern = re.compile(
        r'(?:^|\n)\s*([' + OPTION_LETTER_CHARS + r'])\s*\n\s*\$\$(.*?)\$\$',
        re.DOTALL
    )

    # Strategy 4: Letter inside $$...$$ block like "$$B \qquad \frac{3}{4}...$$"
    inline_math_option = re.compile(
        r'\$\$\s*([' + OPTION_LETTER_CHARS + r'])\s+\\qquad\s+(.*?)\$\$'
    )

    # Strategy 5: Letter on its own line followed by image
    image_option_pattern = re.compile(
        r'(?:^|\n)\s*([' + OPTION_LETTER_CHARS + r'])\s*\n+\s*(!\[.*?\]\(.*?\))',
        re.DOTALL
    )

    def add_option(letter, text):
        letter = normalize_letter(letter)
        if letter in found_letters or letter not in expected_letters:
            return False
        found_letters.add(letter)
        options.append({
            "letter": letter,
            "text": clean_option_text(text),
        })
        return True

    # Try list-style options first (most common)
    for match in list_option_pattern.finditer(block_text):
        letter = match.group(1).strip()
        text = match.group(2).strip()
        # Avoid matching section headers like "A I är större..."  when letter is in question body
        add_option(letter, text)

    # If we didn't find enough, try bare options
    if len(found_letters) < len(expected_letters):
        for match in bare_option_pattern.finditer(block_text):
            letter = match.group(1).strip()
            text = match.group(2).strip()
            # Skip if this looks like it's part of question text (e.g. section headers)
            if any(skip in text for skip in ['problemlösning', 'jämförelser', 'resonemang']):
                continue
            add_option(letter, text)

    # Try math block options (letter on own line, then $$...$$)
    if len(found_letters) < len(expected_letters):
        for match in math_block_pattern.finditer(block_text):
            letter = match.group(1).strip()
            text = match.group(2).strip()
            add_option(letter, text)

    # Try inline math options ($$B \qquad ...$$)
    if len(found_letters) < len(expected_letters):
        for match in inline_math_option.finditer(block_text):
            letter = match.group(1).strip()
            text = match.group(2).strip()
            add_option(letter, text)

    # Try image options (letter then image on next line)
    if len(found_letters) < len(expected_letters):
        for match in image_option_pattern.finditer(block_text):
            letter = match.group(1).strip()
            text = match.group(2).strip()
            add_option(letter, text)

    # Strategy 6: Garbled <sup> tag options like "<sup>B</sup><sup>3</sup> 1"
    # which should be "B: 1/3"
    if len(found_letters) < len(expected_letters):
        sup_option_pattern = re.compile(
            r'<sup>([' + OPTION_LETTER_CHARS + r'])</sup>\s*<sup>(\d+)</sup>\s*(\d+)'
        )
        for match in sup_option_pattern.finditer(block_text):
            letter = match.group(1).strip()
            denom = match.group(2)
            numer = match.group(3)
            text = f"{numer}/{denom}"
            add_option(letter, text)

    # Sort by letter
    letter_order = {l: i for i, l in enumerate("ABCDE")}
    options.sort(key=lambda o: letter_order.get(o["letter"], 99))

    return options


def extract_question_text(block_text, options, q_num):
    """Extract the question text, removing the options portion."""
    # Remove the question number prefix
    text = re.sub(
        r'^(?:[-*]\s*)?(?:#{1,4}\s*)?(?:\*\*)?(\d{1,2})\.(?:\*\*)?\s*',
        '',
        block_text,
        count=1
    )

    # Find where the first option starts and take everything before it
    if options:
        first_letter = options[0]["letter"]

        # Build list of all characters that could represent this letter
        reverse_map = {
            'A': ['A', 'А', 'Α'],  # Latin, Cyrillic, Greek
            'B': ['B', 'В', 'Β'],
            'C': ['C', 'С'],
            'D': ['D'],
            'E': ['E', 'Ε'],
        }
        letter_variants = reverse_map.get(first_letter, [first_letter])

        earliest_pos = len(text)
        for lv in letter_variants:
            patterns = [
                rf'^\s*[-*]\s*(?:\*\*)?{re.escape(lv)}(?:\*\*)?\s',  # list item
                rf'^(?:\*\*)?{re.escape(lv)}(?:\*\*)?\s',            # bare letter
                rf'\$\$\s*{re.escape(lv)}\s+\\qquad',               # inside math block
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.MULTILINE)
                if match and match.start() < earliest_pos:
                    earliest_pos = match.start()

        question_text = text[:earliest_pos].strip()
    else:
        question_text = text.strip()

    # Clean up markdown artifacts
    question_text = re.sub(r'^#+\s*', '', question_text, flags=re.MULTILINE)

    return question_text


def parse_question_block(q_num, block_text, section, section_ranges):
    """Parse a single question block into a structured dict."""
    images = extract_images(block_text)
    options = parse_options_from_block(block_text, section)
    question_text = extract_question_text(block_text, options, q_num)

    # For KVA questions, try to extract Kvantitet I and II
    kvantitet_i = None
    kvantitet_ii = None
    if section == "KVA":
        ki_match = re.search(
            r'[Kk]vantitet\s*I[:\s]+(.+?)(?=\n|[Kk]vantitet\s*II)',
            question_text, re.DOTALL
        )
        kii_match = re.search(
            r'[Kk]vantitet\s*II[:\s]+(.+?)(?=\n\s*[-*]?\s*A\s|\n\n|$)',
            question_text, re.DOTALL
        )
        if ki_match:
            kvantitet_i = ki_match.group(1).strip()
        if kii_match:
            kvantitet_ii = kii_match.group(1).strip()

    # For NOG questions, try to extract statements (1) and (2)
    statements = []
    if section == "NOG":
        stmt_pattern = re.compile(r'\((\d)\)\s+(.+?)(?=\(\d\)|\n\s*#{1,4}|\n\s*Tillräcklig|$)', re.DOTALL)
        for match in stmt_pattern.finditer(question_text):
            statements.append({
                "number": int(match.group(1)),
                "text": match.group(2).strip(),
            })

    question = {
        "question_number": q_num,
        "section": section,
        "section_name": SECTIONS.get(section, {}).get("name", "Unknown"),
        "question_text": question_text,
        "options": options,
        "images": images,
        "flags": [],
    }

    if kvantitet_i:
        question["kvantitet_I"] = kvantitet_i
    if kvantitet_ii:
        question["kvantitet_II"] = kvantitet_ii
    if statements:
        question["statements"] = statements

    # Detect issues
    question["flags"] = detect_issues(question)

    # Check expected vs actual option count
    expected_count = len(SECTIONS.get(section, {}).get("options", "ABCD"))
    if len(options) != expected_count:
        question["flags"].append(
            f"OPTION_COUNT_MISMATCH: Expected {expected_count} options, found {len(options)}"
        )

    return question


def parse_markdown(md_text, exam_date=None, provpass=None, provpass_type="kvant"):
    """Parse the full markdown file into structured JSON."""
    # Try to extract exam metadata
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', md_text)
    if date_match and not exam_date:
        exam_date = date_match.group(1)

    provpass_match = re.search(r'Provpass\s*(\d+)', md_text)
    if provpass_match and not provpass:
        provpass = int(provpass_match.group(1))

    section_ranges = parse_section_ranges(md_text, provpass_type)
    question_blocks = split_into_questions(md_text)

    questions = []
    for q_num, block_text in question_blocks:
        section = determine_section(q_num, section_ranges)
        question = parse_question_block(q_num, block_text, section, section_ranges)
        questions.append(question)

    # Summary stats
    flagged = [q for q in questions if q["flags"]]
    with_images = [q for q in questions if q["images"]]

    sections_for_meta = SECTIONS_VERBAL if provpass_type == "verbal" else SECTIONS_KVANT

    result = {
        "metadata": {
            "exam_date": exam_date,
            "provpass": provpass,
            "provpass_type": provpass_type,
            "total_questions": len(questions),
            "questions_with_images": len(with_images),
            "questions_with_flags": len(flagged),
            "sections": {
                section: {
                    "name": info["name"],
                    "range": list(section_ranges.get(section, [])),
                    "question_count": len([
                        q for q in questions
                        if q["section"] == section
                    ]),
                }
                for section, info in sections_for_meta.items()
            },
        },
        "questions": questions,
    }

    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Parse Marker markdown output from Högskoleprovet into structured JSON"
    )
    parser.add_argument("markdown_file", help="Path to the markdown file")
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path (default: same name with .json extension)"
    )
    parser.add_argument("--exam-date", help="Override exam date (YYYY-MM-DD)")
    parser.add_argument("--provpass", type=int, help="Override provpass number")
    parser.add_argument(
        "--provpass-type", choices=["kvant", "verbal"], default="kvant",
        help="Type of provpass: kvant (default) or verbal"
    )
    parser.add_argument(
        "--pretty", action="store_true", default=True,
        help="Pretty-print JSON output (default: true)"
    )

    args = parser.parse_args()

    md_path = Path(args.markdown_file)
    if not md_path.exists():
        print(f"Error: File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    # Read markdown
    md_text = md_path.read_text(encoding="utf-8")

    # Parse
    result = parse_markdown(md_text, args.exam_date, args.provpass, args.provpass_type)

    # Output path
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = md_path.with_suffix(".json")

    # Write JSON
    indent = 2 if args.pretty else None
    out_path.write_text(
        json.dumps(result, indent=indent, ensure_ascii=False),
        encoding="utf-8"
    )

    # Print summary
    meta = result["metadata"]
    print(f"Parsed: {md_path.name}")
    print(f"  Exam date: {meta['exam_date']}")
    print(f"  Provpass: {meta['provpass']}")
    print(f"  Questions: {meta['total_questions']}")
    print(f"  With images: {meta['questions_with_images']}")
    print(f"  Flagged for review: {meta['questions_with_flags']}")
    print()

    # Print flagged questions
    flagged = [q for q in result["questions"] if q["flags"]]
    if flagged:
        print("Questions flagged for manual review:")
        for q in flagged:
            print(f"  Question {q['question_number']} ({q['section']}):")
            for flag in q["flags"]:
                print(f"    - {flag}")
        print()

    print(f"Output written to: {out_path}")


if __name__ == "__main__":
    main()
