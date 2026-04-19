---
name: pdf-page-offset
description: Handle mismatch between PDF physical page numbers and printed page numbers. Use when navigating to a specific printed page number in a PDF.
tags: pdf
---

# PDF Page Offset Skill

PDFs often have a page offset: the physical position of a page in the file
differs from the printed number shown on that page.

Common causes:
- Cover, copyright, foreword pages at the front (no printed number, or Roman numerals)
- The printed "p.1" starts at physical page 10, 12, etc.

## Step 1: Detect the offset

After calling `read_pdf_pages`, look at the text for a printed page number.
It usually appears in the header or footer, e.g. "141", "- 141 -", "第141頁".

Example:
- You called `read_pdf_pages("doc.pdf", "151")`
- The returned text shows header/footer: `141`
- offset = physical_page - printed_page = 151 - 141 = **10**

## Step 2: Correct the target

To reach printed page N:
```
correct_physical_page = N + offset
```

Example: want printed p.151 → physical page = 151 + 10 = **161**

## Step 3: Verify

Call `read_pdf_pages("doc.pdf", "161")` and confirm the printed number is 151.

## Step 4: Apply consistently

Once offset is known, use it for all subsequent page lookups in this session.
Write it down in your reasoning so you don't forget:
> "This PDF has offset=10. Printed p.N → physical page N+10."

## Edge cases

- **Front matter with Roman numerals** (i, ii, iii...): these pages have a
  different or no offset. Once past the introduction, the main body has its
  own consistent offset.
- **Offset is 0**: physical = printed. Confirm by checking one page first.
- **Unsure**: read a page near the target, check the printed number,
  then bracket inward (go higher or lower) until you land on the right page.

## Quick reference

| Situation | Action |
|-----------|--------|
| First time reading this PDF | Read page 1 to check if offset exists |
| TOC says "topic starts at p.X" | Read physical page X first, note printed number, calculate offset |
| Already know offset | Apply directly: physical = printed + offset |
| Offset seems wrong | Re-sample with a nearby page to re-confirm |
