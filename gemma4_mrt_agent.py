#!/usr/bin/env python3
"""
gemma4_mrt_agent.py — MRT 維修手冊 AI Agent (vLLM + Gemma 4)

Strategy:
  1. Use text layer (pdfplumber) to navigate TOC and find relevant pages.
     Handle physical vs printed page offset dynamically (one page at a time).
  2. Once correct pages confirmed, render them as JPEG (PyMuPDF).
  3. Open images with xdg-open so engineers see original tables/figures.

Tools: list_pdfs, get_pdf_info, read_pdf_pages, search_pdf_text,
       render_pdf_pages, open_files, web_search, bash, read_file,
       write_file, edit_file, load_skill, todo, task, compact
"""

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

load_dotenv(override=True)

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://10.22.100.103:8000/v1")
MODEL = os.getenv("VLLM_MODEL", "google/gemma-4-E4B-it")

client = OpenAI(base_url=VLLM_BASE_URL, api_key="none")

WORKDIR = Path(__file__).parent.resolve()
SKILLS_DIR = WORKDIR / "skills"
OUTPUT_DIR = WORKDIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 6
SESSION_READS_FILE = OUTPUT_DIR / "session_reads.md"
CONTENT_TOOLS = {"read_pdf_pages", "search_pdf_text"}


def _session_append(tool_name: str, args: dict, content: str):
    """Append a tool result to the session reads file for later retrieval."""
    with open(SESSION_READS_FILE, "a", encoding="utf-8") as f:
        label = ", ".join(f"{k}={v}" for k, v in args.items())
        f.write(f"\n\n---\n## [{tool_name}] {label}\n\n{content}\n")


# ── SkillLoader ────────────────────────────────────────────────────────────────
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

    def _load_all(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body}

    def _parse_frontmatter(self, text: str) -> tuple:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            import yaml
            meta = yaml.safe_load(match.group(1)) or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


# ── TodoManager ────────────────────────────────────────────────────────────────
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


SKILL_LOADER = SkillLoader(SKILLS_DIR)
TODO = TodoManager()

SYSTEM = f"""You are an AI assistant specialized in answering questions about MRT maintenance manuals.
All PDF documents are located in: {WORKDIR}/MRT_docs/

Your goal: find the relevant pages across multiple PDFs, show the engineer the ORIGINAL document images.

=== MANDATORY WORKFLOW ===

Step 1 — load_skill("pdf-reading") FIRST.
Step 2 — list_pdfs() → see all available PDFs with filenames and sizes.
Step 3 — RANK the PDFs by relevance to the question:
         Read the filenames carefully. Based on keywords in the question, decide which PDF(s)
         are most likely to contain the answer. State your ranking and reasoning explicitly.
         Example: "問題關於月臺門維修 → 含'月臺門'或'PSD'的 PDF 優先"
Step 4 — For the top-ranked PDF: search_pdf_text(path, 關鍵字) to quickly confirm it has content.
         If no match → try the next ranked PDF. Repeat until a match is found.
Step 5 — Once confirmed: read_pdf_pages(path, "1-5") to find the Table of Contents.
         If the last page read still contains TOC entries (e.g. chapter/section lines with page numbers),
         keep reading in batches of 5 (read "6-10", then "11-15" if needed) until you reach a page
         that no longer has TOC content.
         Write out the FULL TOC (all chapters + subsections with printed page numbers).
Step 6 — search_pdf_text again with more specific keywords to get the exact physical page.
Step 7 — read_pdf_pages ONE physical page at a time to VERIFY correct location.
         ALWAYS state: "Physical {{N}} 的印刷頁碼是 {{X}}。"
         If X ≠ expected → adjust: new_physical = current + (expected - X), re-read.
Step 8 — Keep reading until a NEW section heading appears (Rule 2).
         MANDATORY: After finding content on page N, ALWAYS read page N+1.
         If N+1 starts new heading → section ends at N.
         If N+1 continues → collect, read N+2. Repeat.
         NEVER stop without reading the next page.
Step 9 — render_pdf_pages with confirmed physical page numbers → JPEG saved to output/.
Step 10 — open_files to display images.
Step 11 — Text summary in Traditional Chinese, with <AvailableImageFiles> tag.

=== PAGE NAVIGATION (critical) ===

Physical page != printed page. NEVER assume they match.

Each read_pdf_pages result includes:
  [HEADER LINES]  <- first 3 lines of the page
  [FOOTER LINES]  <- last 3 lines of the page

HOW TO FIND THE PRINTED PAGE NUMBER:
  Look at [HEADER LINES] and [FOOTER LINES] for a page number.
  Common formats (varies by document):
    - "SYL-TK01-OPM-ESN-0005-0A - 92 - OCT, 2025"  -> printed page is 92
    - "- 92 -"                                        -> printed page is 92
    - "92"  (standalone short line)                   -> printed page is 92
    - "2-13"  (chapter-page format)                   -> printed page is 2-13
  After reading each page, ALWAYS state: "Physical {{N}} 的印刷頁碼是 {{X}}。"

If the printed page X != expected page number:
  offset = physical - printed  (e.g. 110 - 92 = 18)
  To reach printed p.N: read physical page N + offset

ALWAYS read ONE page at a time when navigating — never a range.
ALWAYS use search_pdf_text first to get an approximate physical page before manual navigation.

=== RENDERING ===

After confirming physical page range:
  - render_pdf_pages(path, "physical_start-physical_end")
  - open_files with comma-separated absolute paths
  - State: section title, printed pages, image filenames

=== LANGUAGE ===

ALWAYS respond in Traditional Chinese. All explanations, summaries, and
status updates must be in Chinese. Section titles should be quoted verbatim from the document.

=== ANSWER FORMAT ===

Your final answer MUST follow this structure (in Chinese):

**章節**：（原文章節標題，中英文）
**印刷頁碼範圍**：（例：第91-92頁）
**重點摘要**：
- （條列關鍵資訊，如維修週期、元件名稱、工具清單等）

<AvailableImageFiles>filename1.jpg, filename2.jpg</AvailableImageFiles>

The <AvailableImageFiles> tag is MANDATORY in every final answer that includes images.
Use only the filename (not full path) inside the tag.
List ALL rendered image files, comma-separated.

=== SESSION MEMORY ===

Every read_pdf_pages and search_pdf_text result is automatically saved to:
  output/session_reads.md

If a tool result has been compacted and you see:
  "[Compacted — full content saved in session_reads.md ...]"
-> Call read_file("output/session_reads.md") to retrieve the full original content.

When answering a follow-up question that requires details from previously read pages,
ALWAYS check session_reads.md first before re-reading the PDF.

Use the todo tool to track multi-step tasks.
IMPORTANT: Every time you call todo, include ALL tasks in the list.
NEVER mark a task completed if it returned an error.
Use load_skill for specialized knowledge. Use task to delegate subtasks.

Skills available:
{SKILL_LOADER.get_descriptions()}"""

SUBAGENT_SYSTEM = f"""You are a subagent helping answer questions about an MRT maintenance manual.
Working directory: {WORKDIR}
Complete the given task and summarize your findings.
Think step by step and explain your reasoning."""


# ── Context compression ─────────────────────────────────────────────────────────
def estimate_tokens(messages: list) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(part.get("text", "")) // 4
    return total


def micro_compact(messages: list) -> tuple:
    """Returns (messages, content_was_cleared)."""
    tool_indices = [i for i, m in enumerate(messages)
                    if m.get("role") == "tool" and m.get("name") != "load_skill"]
    if len(tool_indices) <= KEEP_RECENT:
        return messages, False
    to_clear = tool_indices[:-KEEP_RECENT]
    cleared = 0
    content_cleared = False
    for idx in to_clear:
        msg = messages[idx]
        if isinstance(msg.get("content"), str) and len(msg["content"]) > 100:
            name = msg.get("name", "unknown")
            if name in CONTENT_TOOLS:
                messages[idx]["content"] = "[Compacted — full content saved in output/session_reads.md]"
                content_cleared = True
            else:
                messages[idx]["content"] = f"[Previous result: {name}]"
            cleared += 1
    if cleared:
        print(f"[micro_compact: cleared {cleared} old tool results]")
    return messages, content_cleared


def auto_compact(messages: list) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")
    conversation_text = json.dumps(messages, default=str)[:80000]
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made, "
            "4) Which PDF pages were read and what was found (include physical AND printed page numbers), "
            "5) Which JPEG files were rendered and opened. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = response.choices[0].message.content
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
    ]


# ── PDF tools ───────────────────────────────────────────────────────────────────
def parse_pages(pages_str: str) -> list:
    result = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            result.extend(range(int(start), int(end) + 1))
        elif part:
            result.append(int(part))
    return sorted(set(result))


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_list_pdfs() -> str:
    pdf_dir = WORKDIR / "MRT_docs"
    if not pdf_dir.exists():
        return "Error: MRT_docs/ directory not found."
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        return "No PDF files found in MRT_docs/."
    lines = [f"Found {len(pdfs)} PDF(s) in MRT_docs/:\n"]
    for p in pdfs:
        size_mb = p.stat().st_size / (1024 * 1024)
        lines.append(f"  [{size_mb:.1f} MB] MRT_docs/{p.name}")
    return "\n".join(lines)


def run_get_pdf_info(path: str) -> str:
    if not PDFPLUMBER_AVAILABLE:
        return "Error: pdfplumber not installed. Run: pip install pdfplumber"
    fp = safe_path(path)
    if not fp.exists():
        return f"Error: File not found: {path}"
    try:
        with pdfplumber.open(fp) as pdf:
            pages = len(pdf.pages)
        size_mb = fp.stat().st_size / (1024 * 1024)
        return f"pages: {pages}, size: {size_mb:.1f} MB, path: {fp}"
    except Exception as e:
        return f"Error reading PDF: {e}"


def run_read_pdf_pages(path: str, pages: str) -> str:
    if not PDFPLUMBER_AVAILABLE:
        return "Error: pdfplumber not installed. Run: pip install pdfplumber"
    fp = safe_path(path)
    if not fp.exists():
        return f"Error: File not found: {path}"
    page_list = parse_pages(pages)
    if not page_list:
        return "Error: No valid pages specified."
    if len(page_list) > 30:
        return "Error: Max 30 pages per call."
    try:
        results = []
        with pdfplumber.open(fp) as pdf:
            total = len(pdf.pages)
            for page_num in page_list:
                if page_num < 1 or page_num > total:
                    results.append(f"[Page {page_num}: out of range (total: {total})]")
                    continue
                page = pdf.pages[page_num - 1]
                text = page.extract_text() or ""
                if text.strip():
                    lines = [l for l in text.strip().splitlines() if l.strip()]
                    header_lines = "\n".join(lines[:3])
                    footer_lines = "\n".join(lines[-3:]) if len(lines) > 3 else ""
                    block = (
                        f"--- Physical Page {page_num} ---\n"
                        f"[HEADER LINES]\n{header_lines}\n"
                        f"...\n"
                        f"{text.strip()}\n"
                        f"...\n"
                        f"[FOOTER LINES]\n{footer_lines}"
                    )
                    results.append(block)
                else:
                    results.append(
                        f"--- Physical Page {page_num} ---\n"
                        f"[No extractable text — likely scanned image or diagram]"
                    )
        output = "\n\n".join(results)
        _session_append("read_pdf_pages", {"path": path, "pages": pages}, output)
        return output
    except Exception as e:
        return f"Error reading PDF pages: {e}"


def run_search_pdf_text(path: str, query: str, max_results: int = 10) -> str:
    if not PDFPLUMBER_AVAILABLE:
        return "Error: pdfplumber not installed."
    fp = safe_path(path)
    if not fp.exists():
        return f"Error: File not found: {path}"
    query_lower = query.lower()
    results = []
    try:
        with pdfplumber.open(fp) as pdf:
            total = len(pdf.pages)
            for page_num in range(1, total + 1):
                page = pdf.pages[page_num - 1]
                text = page.extract_text() or ""
                if query_lower in text.lower():
                    lines = [l for l in text.strip().splitlines() if l.strip()]
                    footer = " | ".join(lines[-3:]) if lines else ""
                    idx = text.lower().find(query_lower)
                    start = max(0, idx - 80)
                    end = min(len(text), idx + len(query) + 120)
                    snippet = text[start:end].replace("\n", " ").strip()
                    results.append(
                        f"Physical page {page_num} "
                        f"(footer: [{footer}]): ...{snippet}..."
                    )
                    if len(results) >= max_results:
                        break
        if not results:
            return f"No matches found for: {query!r}"
        output = f"Found {len(results)} match(es) for {query!r}:\n\n" + "\n\n".join(results)
        _session_append("search_pdf_text", {"path": path, "query": query}, output)
        return output
    except Exception as e:
        return f"Error searching PDF: {e}"


def run_render_pdf_pages(path: str, pages: str, dpi: int = 150) -> str:
    if not FITZ_AVAILABLE:
        return "Error: PyMuPDF not installed. Run: pip install pymupdf"
    fp = safe_path(path)
    if not fp.exists():
        return f"Error: File not found: {path}"
    page_list = parse_pages(pages)
    if not page_list:
        return "Error: No valid pages specified."
    if len(page_list) > 20:
        return "Error: Max 20 pages per render call to keep file sizes manageable."
    try:
        doc = fitz.open(fp)
        total = len(doc)
        saved = []
        pdf_stem = fp.stem[:30].replace(" ", "_")
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for page_num in page_list:
            if page_num < 1 or page_num > total:
                saved.append(f"[Page {page_num}: out of range (total: {total})]")
                continue
            page = doc[page_num - 1]
            pix = page.get_pixmap(matrix=mat)
            out_name = f"{pdf_stem}_p{page_num:04d}.jpg"
            out_path = OUTPUT_DIR / out_name
            pix.save(str(out_path))
            saved.append(str(out_path))
        doc.close()
        return "Rendered pages:\n" + "\n".join(saved) if saved else "No pages rendered."
    except Exception as e:
        return f"Error rendering PDF pages: {e}"


def run_open_files(paths: str) -> str:
    file_list = [p.strip() for p in paths.split(",") if p.strip()]
    if not file_list:
        return "Error: No file paths provided."
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    found = []
    missing = []
    for p in file_list:
        fp = Path(p)
        if not fp.is_absolute():
            fp = WORKDIR / fp
        if fp.exists():
            found.append(str(fp))
        else:
            missing.append(p)
    if not found:
        return f"Files not found: {', '.join(missing)}"
    if has_display:
        for fp in found:
            try:
                subprocess.Popen(["xdg-open", fp],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    msg = f"Images saved ({len(found)} file(s)):\n" + "\n".join(f"  {f}" for f in found)
    if missing:
        msg += f"\nNot found: {', '.join(missing)}"
    return msg


# ── Filesystem tools ─────────────────────────────────────────────────────────────
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {fp}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# ── Web search ────────────────────────────────────────────────────────────────────
def run_web_search(query: str, num_results: int = 5) -> str:
    api_key = os.environ.get("SERP_API_KEY", "")
    if not api_key:
        return "Error: SERP_API_KEY not set in .env"
    params = urllib.parse.urlencode({
        "q": query,
        "api_key": api_key,
        "num": min(num_results, 10),
        "hl": "zh-tw",
    })
    req = urllib.request.Request(
        f"https://serpapi.com/search.json?{params}",
        headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for r in data.get("organic_results", [])[:num_results]:
            results.append(f"**{r.get('title','')}**\n{r.get('link','')}\n{r.get('snippet','')}")
        return "\n\n".join(results) if results else f"No results for: {query}"
    except Exception as e:
        return f"Error: {e}"


# ── Tool dispatch ─────────────────────────────────────────────────────────────────
TOOL_HANDLERS = {
    "list_pdfs":         lambda **kw: run_list_pdfs(),
    "bash":              lambda **kw: run_bash(kw["command"]),
    "read_file":         lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":              lambda **kw: TODO.update(kw["items"]),
    "load_skill":        lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "get_pdf_info":      lambda **kw: run_get_pdf_info(kw["path"]),
    "read_pdf_pages":    lambda **kw: run_read_pdf_pages(kw["path"], kw["pages"]),
    "search_pdf_text":   lambda **kw: run_search_pdf_text(kw["path"], kw["query"], kw.get("max_results", 10)),
    "render_pdf_pages":  lambda **kw: run_render_pdf_pages(kw["path"], kw["pages"], kw.get("dpi", 150)),
    "open_files":        lambda **kw: run_open_files(kw["paths"]),
    "web_search":        lambda **kw: run_web_search(kw["query"], kw.get("num_results", 5)),
    "compact":           lambda **kw: "Manual compression requested.",
}

CHILD_TOOLS = [
    {"type": "function", "function": {
        "name": "list_pdfs",
        "description": (
            "List all PDF files available in MRT_docs/. "
            "Always call this FIRST (after load_skill) to discover available documents."
        ),
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "limit": {"type": "integer"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace exact text in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}
        }, "required": ["path", "old_text", "new_text"]}
    }},
    {"type": "function", "function": {
        "name": "load_skill", "description": "Load specialized knowledge by name.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    }},
    {"type": "function", "function": {
        "name": "get_pdf_info",
        "description": "Get PDF metadata: total page count and file size.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path to PDF (e.g. MRT_docs/filename.pdf)"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "read_pdf_pages",
        "description": (
            "Extract TEXT LAYER from PDF pages — for navigation and TOC reading only. "
            "Use this to find printed page numbers and locate relevant sections. "
            "NEVER use this as the final output — use render_pdf_pages for that. "
            "Supports '1-5', '1,3,10', or '42'. Max 30 pages per call."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path to PDF"},
            "pages": {"type": "string", "description": "Physical page numbers: '1-5', '42', '10,15,20'"}
        }, "required": ["path", "pages"]}
    }},
    {"type": "function", "function": {
        "name": "search_pdf_text",
        "description": (
            "Search for a keyword or phrase across ALL pages of a PDF. "
            "Returns physical page numbers and footer context. "
            "Always prefer this over guessing page numbers from the TOC alone."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path to PDF"},
            "query": {"type": "string", "description": "Text to search for (case-insensitive)"},
            "max_results": {"type": "integer", "description": "Max matches to return (default 10)"}
        }, "required": ["path", "query"]}
    }},
    {"type": "function", "function": {
        "name": "render_pdf_pages",
        "description": (
            "Render PDF pages as JPEG images. Images saved to output/ directory. "
            "Max 20 pages per call. Use physical page numbers (not printed page numbers). "
            "MANDATORY: You MUST have called read_pdf_pages on EVERY page in this range before rendering."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path to PDF"},
            "pages": {"type": "string", "description": "Physical page numbers to render: '42-45', '10,12'"},
            "dpi": {"type": "integer", "description": "Resolution (default 150, use 200 for better quality)"}
        }, "required": ["path", "pages"]}
    }},
    {"type": "function", "function": {
        "name": "open_files",
        "description": "Open files with the system viewer. Use after render_pdf_pages to display images.",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "string", "description": "Comma-separated absolute file paths to open"}
        }, "required": ["paths"]}
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for supplemental technical information not in the manual.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "num_results": {"type": "integer", "description": "1-10, default 5"}
        }, "required": ["query"]}
    }},
]

PARENT_TOOLS = CHILD_TOOLS + [
    {"type": "function", "function": {
        "name": "task",
        "description": "Spawn a subagent with fresh context to handle an independent subtask.",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string"},
            "description": {"type": "string"}
        }, "required": ["prompt"]}
    }},
    {"type": "function", "function": {
        "name": "todo",
        "description": "Update task list. Include ALL tasks every time — never partial.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array",
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
                }, "required": ["id", "text", "status"]}
            }
        }, "required": ["items"]}
    }},
    {"type": "function", "function": {
        "name": "compact",
        "description": "Trigger manual conversation compression when context is getting long.",
        "parameters": {"type": "object", "properties": {"focus": {"type": "string"}}}
    }},
]


# ── Subagent ──────────────────────────────────────────────────────────────────────
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(30):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SUBAGENT_SYSTEM}] + sub_messages,
                tools=CHILD_TOOLS,
                max_tokens=8000,
            )
        except Exception as e:
            return f"Subagent failed: {str(e)[:500]}"
        msg = response.choices[0].message
        sub_messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})
        if msg.content:
            print(f"  \033[34m[subagent] {msg.content}\033[0m")
        if not msg.tool_calls:
            break
        for block in msg.tool_calls:
            args = json.loads(block.function.arguments)
            handler = TOOL_HANDLERS.get(block.function.name)
            try:
                output = handler(**args) if handler else f"Unknown tool: {block.function.name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"  [subagent] > {block.function.name}: {str(output)[:200]}")
            sub_messages.append({
                "role": "tool", "tool_call_id": block.id,
                "name": block.function.name, "content": str(output)[:50000]
            })
    return response.choices[0].message.content or "(no summary)" if response else "(no summary)"


# ── Agent loop ────────────────────────────────────────────────────────────────────
def agent_loop(messages: list):
    rounds_since_todo = 0
    compaction_reminder_sent = False
    while True:
        tokens_before = estimate_tokens(messages)
        messages, content_cleared = micro_compact(messages)
        tokens_after = estimate_tokens(messages)
        if tokens_after != tokens_before:
            print(f"[context: {tokens_before:,} → {tokens_after:,} tokens after micro_compact]")
        else:
            print(f"[context: {tokens_before:,} tokens]")
        if estimate_tokens(messages) > THRESHOLD:
            print(f"[auto_compact triggered: {estimate_tokens(messages):,} tokens > threshold {THRESHOLD:,}]")
            messages[:] = auto_compact(messages)
            print(f"[context after auto_compact: {estimate_tokens(messages):,} tokens]")

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM}] + messages,
                tools=PARENT_TOOLS,
                max_tokens=8000,
            )
        except Exception as e:
            print(f"\033[31m[API Error] {str(e)[:400]}\033[0m")
            messages.append({"role": "user", "content": f"<error>API error: {str(e)[:300]}. Adjust and continue.</error>"})
            continue

        msg = response.choices[0].message
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})
        if msg.content:
            print(f"\033[32m{msg.content}\033[0m\n")

        if not msg.tool_calls:
            incomplete = [t for t in TODO.items if t["status"] != "completed"]
            if incomplete:
                messages.append({"role": "user", "content": "<reminder>Mark all completed tasks in your todo list before finishing.</reminder>"})
                continue
            if not compaction_reminder_sent:
                has_compacted = any(
                    "[Compacted — full content saved in output/session_reads.md]" in str(m.get("content", ""))
                    for m in messages if m.get("role") == "tool"
                )
                if has_compacted:
                    messages.append({"role": "user", "content":
                        "<reminder>部分 PDF 頁面內容已被壓縮。"
                        "請呼叫 read_file('output/session_reads.md') 取回完整內容後再給出最終答案。</reminder>"
                    })
                    compaction_reminder_sent = True
                    continue
            return

        results = []
        used_todo = False
        manual_compact = False

        for block in msg.tool_calls:
            args = json.loads(block.function.arguments)
            name = block.function.name

            if name == "task":
                desc = args.get("description", "subtask")
                print(f"> task ({desc}): {args['prompt'][:80]}")
                output = run_subagent(args["prompt"])
            elif name == "compact":
                manual_compact = True
                output = "Compressing..."
            else:
                if name == "bash":
                    print(f"\033[33m$ {args['command']}\033[0m")
                handler = TOOL_HANDLERS.get(name)
                try:
                    output = handler(**args) if handler else f"Unknown tool: {name}"
                except Exception as e:
                    output = f"Error: {e}"

            if name == "todo":
                print(f"> todo:\n{output}")
                used_todo = True
            elif name == "list_pdfs":
                print(f"> list_pdfs:\n{str(output)}")
            elif name == "read_pdf_pages":
                preview = str(output)[:1500]
                print(f"> read_pdf_pages (p.{args.get('pages', '?')}):\n{preview}...")
            elif name == "search_pdf_text":
                q = args.get("query", "")
                preview = str(output)[:800]
                print(f"> search_pdf_text ({q!r}):\n{preview}...")
            elif name == "render_pdf_pages":
                print(f"> render_pdf_pages (p.{args.get('pages', '?')}): {str(output)[:300]}")
            elif name == "open_files":
                print(f"> open_files: {str(output)[:200]}")
            elif name == "web_search":
                print(f"> web_search ({args.get('query', '')!r}): {str(output)[:200]}...")
            else:
                print(f"> {name}: {str(output)[:200]}")

            results.append({
                "role": "tool", "tool_call_id": block.id,
                "name": name, "content": str(output)[:50000]
            })

        for result in results:
            messages.append(result)

        compaction_reminder_sent = False
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})

        tool_names_this_round = [b.function.name for b in (msg.tool_calls or [])]
        if "render_pdf_pages" in tool_names_this_round and "read_pdf_pages" not in tool_names_this_round:
            messages.append({"role": "user", "content":
                "<reminder>你是否已讀過該章節最後一頁的下一頁，確認章節已結束？"
                "如果還沒，請先讀下一頁再決定是否完成。</reminder>"
            })

        if manual_compact:
            print(f"[manual compact: {estimate_tokens(messages):,} tokens before]")
            messages[:] = auto_compact(messages)
            print(f"[manual compact done: {estimate_tokens(messages):,} tokens after]")
            content_cleared = False


# ── Main ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    SESSION_READS_FILE.write_text("# Session Reads\n\nAll read_pdf_pages and search_pdf_text results are logged here.\n")

    missing = []
    if not PDFPLUMBER_AVAILABLE:
        missing.append("pdfplumber")
    if not FITZ_AVAILABLE:
        missing.append("pymupdf")
    if missing:
        print(f"\033[33m警告：缺少套件：{', '.join(missing)}\033[0m")
        print(f"請執行：pip install {' '.join(missing)}")

    print("\033[32m=== MRT 維修手冊 AI Agent (Gemma 4) ===\033[0m")
    print(f"vLLM 伺服器: {VLLM_BASE_URL}")
    print(f"模型: {MODEL}")
    print(f"PDF 目錄: {WORKDIR}/MRT_docs/")
    print(f"輸出目錄: {OUTPUT_DIR}/")
    print("輸入 'q' 或 Ctrl+C 離開\n")

    history = []
    while True:
        try:
            query = input("\033[36mmrt >> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if query.lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
