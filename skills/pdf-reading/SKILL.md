---
name: pdf-reading
description: Best practices for reading PDFs — load this skill before extracting any content from a PDF. Covers PDF ranking, note-taking, page offset correction, reading completeness, and edge cases.
tags: pdf
---

# PDF Reading Skill

Load this skill before extracting content from any PDF.

---

## Step 0 — Note Files

Note files (`pdf_reading_note_N.txt`) are cleared at system startup automatically.
Each PDF you read gets its own note file. Assign `note_id` sequentially:
- First PDF read → `note_id=1` → `output/pdf_reading_note_1.txt`
- Second PDF read → `note_id=2` → `output/pdf_reading_note_2.txt`
- etc.

---

## Step 1 — Rank PDFs by Relevance

After `list_pdfs()`, output an **explicit ranked list** before doing anything else.
Each entry must include a numeric relevance score (0.0 = unrelated, 1.0 = perfect match).

Format (MANDATORY — write this out exactly):
```
PDF Relevance List:
[score=0.95] note_id=1  MRT_docs/系統概述_月臺門操作及維修手冊.pdf
             理由：檔名含「月臺門」「操作及維修」，與問題高度相關
[score=0.30] note_id=2  MRT_docs/113電巴資料報告.pdf
             理由：電動巴士資料，與月臺門維修無直接關聯
[score=0.05] note_id=3  MRT_docs/Spec_IB9387.pdf
             理由：英文規格書，非維修手冊

Decision: Start with note_id=1 (score=0.95). Threshold to skip: score < 0.15.
```

⛔ NEVER jump to reading a PDF before writing out this list.
⛔ NEVER omit the score or reason for any file.

---

## Step 2 — Reading Workflow (one PDF at a time)

Start with the highest-scored PDF (note_id=1).

### 2.1 TOC Scan

```
read_pdf_pages(path, "1-5")
```
- If the last page still has TOC entries, keep reading in batches of 5 until TOC ends.
- Write a TOC summary to the note file:
  ```
  write_note("[TOC summary]\n第1章 系統概述 ... p.1\n  1.1 適用範圍 ... p.2\n...", note_id)
  ```
- From the TOC, identify which chapter/section likely contains the answer.
- State explicitly: "問題關於 X，對應章節為 Y.Z，印刷頁約在 p.N。"

### 2.2 Write Relevant Content

If a page contains content **relevant to the question**:
```
write_note("[Physical p.N | Printed p.X]\n<excerpt of relevant content>", note_id)
```
Be selective — copy the exact sentences/tables/numbers that answer the question.
Do NOT dump entire pages into the note.

### 2.3 Skip Irrelevant Content

If a page is NOT relevant to the question: do NOT write it to the note. Simply move on.

### 2.4 Read Multiple Sections if Needed

A complete answer may require reading several chapters or subsections.
After finishing one section, check: is there another section in the TOC that might
also contain relevant information? If yes, jump there and continue.

### 2.5 Read Until Section Clearly Ends

Once you find relevant content on page N:
- ALWAYS read page N+1 before stopping.
- If N+1 starts a new unrelated section → section ends at N. Stop.
- If N+1 continues the same topic → write relevant parts, read N+2. Repeat.

⛔ NEVER stop on the current page alone — you cannot know if the section
   continues until you have seen the next page.

---

## Step 3 — Decide: Enough or Continue to Next PDF?

After finishing the current PDF, assess your notes:

```
Note Assessment:
- Question: <restate the question>
- Notes so far cover: <list what you found>
- Missing: <list what's still unanswered, or "nothing">
- Decision: SUFFICIENT → go to Step 4 / INSUFFICIENT → read next PDF (note_id=N+1)
```

⛔ NEVER skip this assessment step.
If INSUFFICIENT: repeat Step 2 for the next PDF in the ranked list.

---

## Step 4 — Answer from Notes

Read the note file(s) and answer the question faithfully based on their content.
```
read_file("output/pdf_reading_note_1.txt")
```
Do NOT re-read PDFs if the notes are sufficient.
If notes turn out to be incomplete, go back to Step 2 for the relevant PDF.

---

## Rule A — Dynamic Page Navigation

PDFs often have a gap between physical page numbers (position in file) and printed
page numbers (shown in header/footer). **Do NOT assume a fixed offset. Navigate dynamically.**

Each `read_pdf_pages` result includes:
```
[HEADER LINES]  ← first 3 lines of the page
[FOOTER LINES]  ← last 3 lines of the page
```

After reading each page, ALWAYS state:
> "Physical N 的印刷頁碼是 X。"

Common footer formats (varies by document):
- `SYL-TK01-OPM-ESN-0005-0A - 92 - OCT, 2025` → printed page = **92** (number between spaces)
- `- 92 -` → printed page = **92**
- `2-13` (standalone last line) → printed page = **2-13** (chapter-page format)
- `IX` → Roman numeral front matter

If printed X ≠ expected page number:
```
offset = physical − printed   (e.g. physical 110, printed 92 → offset = 18)
target_physical = desired_printed_page + offset
```
Re-read at corrected physical page and verify.

Strategy:
1. Use `search_pdf_text` first to get an approximate physical page.
2. Read that physical page → state the printed number → calculate offset if needed.
3. Jump to corrected physical page → verify → proceed.

Always read ONE page at a time when navigating — never a range.

---

## Rule B — Image-Only Pages

If `read_pdf_pages` returns `[No extractable text — likely scanned image or diagram]`:
- Note its existence: `write_note("[Physical p.N] 圖表頁，無文字層", note_id)`
- Skip and read the next page for context.

---

## Rule C — Multi-Column Tables

PDF text extraction may scramble multi-column tables. If extracted text looks garbled:
- Write a flag: `write_note("[Physical p.N] 表格內容可能亂序，需人工核對原始圖像", note_id)`
- Mention to the user that the table may need manual verification.

---

## Quick Checklist

Before answering:
- [ ] Did I write the PDF Relevance List with explicit scores (Step 1)?
- [ ] Did I write a TOC summary to the note file (Step 2.1)?
- [ ] Did I write ONLY relevant content to the note (Step 2.2/2.3)?
- [ ] Did I state "Physical N 的印刷頁碼是 X" after each page (Rule A)?
- [ ] Did I read the next page before stopping (Step 2.5)?
- [ ] Did I do the Note Assessment before deciding to stop (Step 3)?
- [ ] Am I answering from the note file, not from memory (Step 4)?
