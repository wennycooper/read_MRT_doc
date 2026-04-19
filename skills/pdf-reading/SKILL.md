---
name: pdf-reading
description: Best practices for reading PDFs — load this skill before extracting any content from a PDF. Covers page offset correction, reading completeness, and handling edge cases like tables, image-only pages, and multi-page sections.
tags: pdf
---

# PDF Reading Skill

Load this skill before extracting content from any PDF.

---

## Rule 1 — Dynamic Page Navigation

PDFs often have a gap between physical page numbers (position in file) and printed
page numbers (shown in header/footer). This gap may vary across sections
(e.g. Roman numerals for front matter, Arabic for main content).

**Do NOT assume a fixed offset. Navigate dynamically instead.**

### Strategy: Read ONE page → State printed number → Adjust

When TOC says target is at printed page N:

1. **Read ONE physical page** (just `read_pdf_pages("doc.pdf", "N")` — not a range).
2. **Explicitly state**: "The printed page number on this page is: X"
   Look at the very beginning or end of the extracted text for a standalone number
   like `"54"`, `"- 54 -"`, `"第54頁"`.
3. **Compare and adjust**:
   - X = N → you're there ✓
   - X < N → jump forward: new physical = current_physical + (N - X)
   - X > N → jump backward: new physical = current_physical - (X - N)
4. **Repeat** reading ONE page at a time until printed number = N.

> **Why one page at a time?** Reading ranges (e.g. 64-67) buries footer numbers
> in a wall of text. Reading a single page makes the printed number easy to spot.

### Example

TOC says tool list at printed p.64.

| Step | Physical page read | Footer shows | Action |
|------|--------------------|--------------|--------|
| 1 | 64 | 54 | Need +10 → jump to 74 |
| 2 | 74 | 64 | ✓ Found it |

### Why not a fixed offset?

- Front matter (cover, TOC) may have no printed number or Roman numerals
- Main content starts at a different printed page
- Offset may change at chapter or section boundaries

Always verify by checking the actual printed number on each page you read.

---

## Rule 2 — Reading Completeness

**After finding your target content, you MUST always read the next page before stopping.**

The decision to stop is made by reading the NEXT page, not the current one:

```
read page N  →  collect content
read page N+1  →  decision point:
    does page N+1 START with a new section/chapter heading?
    YES → stop (page N was the last page of the section)
    NO  → collect content from N+1, then read page N+2
read page N+2  →  decision point:
    ...repeat until a new heading appears
```

⛔ NEVER decide to stop based on the current page alone — you cannot know if the
   section continues until you have seen what comes next.

⛔ NEVER stop because:
- The current page "looks complete"
- A numbered list "seems done" (items 1-4 visible)
- You reached the bottom of a page

✅ Stop ONLY after reading a page that BEGINS with a new section/chapter heading
   (e.g. "4.2.2 ...", "第5章 ...", or any heading at the same or higher level).

---

## Rule 3 — Image-Only Pages

If `read_pdf_pages` returns `[No extractable text on this page]`, the page is likely
a scanned image or diagram. Options:
- Skip it and read the next page for context
- Note that a figure/diagram exists at that location
- Do NOT attempt OCR — `read_pdf_pages` only handles text layers

---

## Rule 4 — Multi-Column Tables

PDF text extraction may scramble multi-column table content (columns merged into
one stream). If extracted text looks garbled or out of order:
- Read a few rows and try to identify column boundaries manually
- Mention to the user that the table may need manual verification

---

## Rule 5 — Reading Strategy

For large PDFs (100+ pages):
1. `get_pdf_info` → know total pages
2. Read pages 1-15 → find Table of Contents
3. **MANDATORY: Write out the complete TOC before doing anything else.**

   After reading the TOC pages, your VERY NEXT output MUST be a list like this
   (include ALL levels — chapters AND subsections):
   ```
   TOC 目錄:
   第1章 系統概述 .......................... p.1
     1.1 適用範圍 .......................... p.2
     1.2 縮寫與定義 ........................ p.4
   第4章 系統元件 .......................... p.55
     4.1 自動滑門 .......................... p.56
     4.4 維修程序 .......................... p.98
       4.4.9 矯正性維修 .................... p.119
       4.4.9.9 特殊工具及測試設備 ......... p.128
   第5章 故障排除 .......................... p.140
   ```
   Copy ALL headings VERBATIM from the document (do not translate or summarize).
   Include subsections (1.1, 4.4.9, etc.) — the target content is often in a subsection,
   not at the chapter level.

   ⛔ NEVER mark a "read TOC" task as done without having written out this list.
   ⛔ NEVER jump to a section page before writing out this list.

   If the TOC spans multiple pages or is incomplete, read more pages before listing.

4. Identify which section contains your target content by scanning your written TOC list.
   Search for keywords in the document's language (e.g. for Chinese docs, search Chinese
   terms like "工具清單", "特殊工具", "維修設備" — NOT English translations).
5. Navigate to that section using dynamic page offset correction (Rule 1).
6. Read section start → read next page → stop only when section clearly ends (Rule 2).
7. Do NOT read sequentially page by page — use the TOC to jump to relevant sections.

---

## Quick Checklist

Before answering any question based on a PDF:
- [ ] Did I write out the TOC findings (chapter names + page numbers) before navigating?
- [ ] Did I verify the printed page number after landing on a new page (Rule 1)?
- [ ] Did I keep reading until I saw a NEW section/chapter heading (Rule 2)?
- [ ] Did I handle any image-only pages gracefully?
- [ ] Did I note if any table content looked garbled?
