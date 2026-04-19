#!/usr/bin/env python3
"""
mrt_agent.py — MRT 維修手冊 AI Agent (Azure OpenAI)

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

from openai import AzureOpenAI
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

client = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
)

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
SKILLS_DIR = WORKDIR / "skills"
OUTPUT_DIR = WORKDIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

THRESHOLD = 50000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 6


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
Step 5 — Once confirmed: read_pdf_pages(path, "1-15") to find the Table of Contents.
         Write out the FULL TOC (all chapters + subsections with printed page numbers).
Step 6 — search_pdf_text again with more specific keywords to get the exact physical page.
Step 7 — read_pdf_pages ONE physical page at a time to VERIFY correct location.
         Each result header shows: "Physical Page N | Printed page number detected: [X]"
         ALWAYS state: "Physical {{N}} 的印刷頁碼是 {{X}}。"
         If X ≠ expected → adjust: new_physical = current + (expected - X), re-read.
Step 8 — Keep reading until a NEW section heading appears (Rule 2).
         ⚠️  MANDATORY: After finding content on page N, ALWAYS read page N+1.
         If N+1 starts new heading → section ends at N.
         If N+1 continues → collect, read N+2. Repeat.
         NEVER stop without reading the next page.
Step 9 — render_pdf_pages with confirmed physical page numbers → JPEG saved to output/.
Step 10 — open_files to display images.
Step 11 — Text summary in Traditional Chinese, with <AvailableImageFiles> tag.

=== PAGE NAVIGATION (critical) ===

Physical page ≠ printed page. NEVER assume they match.

The read_pdf_pages header now tells you the printed page number automatically:
  "--- Physical Page 110 | Printed page number detected: [101] ---"
  → This page's printed number is 101. If you wanted printed p.101, you found it ✓

If the detected printed number is wrong:
  offset = physical - printed  (e.g. 110 - 101 = 9)
  To reach printed p.N: read physical page N + offset

ALWAYS read ONE page at a time when navigating — never a range.
ALWAYS use search_pdf_text first to get an approximate physical page before manual navigation.

=== RENDERING ===

After confirming physical page range:
  - render_pdf_pages(path, "physical_start-physical_end")
  - open_files with comma-separated absolute paths
  - State: section title, printed pages, image filenames

=== LANGUAGE ===

ALWAYS respond in Traditional Chinese (繁體中文). All explanations, summaries, and
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


def micro_compact(messages: list) -> list:
    tool_indices = [i for i, m in enumerate(messages)
                    if m.get("role") == "tool" and m.get("name") != "load_skill"]
    if len(tool_indices) <= KEEP_RECENT:
        return messages
    to_clear = tool_indices[:-KEEP_RECENT]
    cleared = 0
    for idx in to_clear:
        msg = messages[idx]
        if isinstance(msg.get("content"), str) and len(msg["content"]) > 100:
            messages[idx]["content"] = f"[Previous result: {msg.get('name', 'unknown')}]"
            cleared += 1
    if cleared:
        print(f"[micro_compact: cleared {cleared} old tool results]")
    return messages


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
    """List all PDF files in MRT_docs/ with sizes."""
    pdf_dir = WORKDIR / "MRT_docs"
    if not pdf_dir.exists():
        return "Error: MRT_docs/ directory not found."
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        return "No PDF files found in MRT_docs/."
    lines = [f"Found {len(pdfs)} PDF(s) in MRT_docs/:\n"]
    for p in pdfs:
        size_mb = p.stat().st_size / (1024 * 1024)
        rel = f"MRT_docs/{p.name}"
        lines.append(f"  [{size_mb:.1f} MB] {rel}")
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


def _extract_printed_page_number(text: str) -> str:
    """Extract the printed page number from header/footer.

    Footer format in this document:
      SYL-TK01-OPM-ESN-0005-0A - 92 - OCT, 2025
    The page number sits between ' - ' (space-dash-space) separators.
    We must NOT match the '0005' inside 'ESN-0005-0A' (no surrounding spaces).
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    candidates = []
    for line in lines[:3] + lines[-3:]:
        # Match " - NUMBER - " with explicit spaces (avoids ESN-0005-0A false positives)
        matches = re.findall(r' - (\d+) - ', line)
        candidates.extend(matches)
        # Also catch Roman numeral pages: " - vi - "
        rom = re.findall(r' - ([ivxlcdmIVXLCDM]+) - ', line)
        candidates.extend(rom)
        # Short standalone number line (e.g. bare "92" in some PDFs)
        if len(line) < 10 and re.fullmatch(r'\d+', line):
            candidates.append(line)
    return candidates[-1] if candidates else "unknown"


def run_read_pdf_pages(path: str, pages: str) -> str:
    """Extract text layer for navigation. NOT for final output — use render_pdf_pages for images."""
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
                    printed = _extract_printed_page_number(text)
                    header = (
                        f"--- Physical Page {page_num} | "
                        f"Printed page number detected: [{printed}] ---"
                    )
                    results.append(f"{header}\n{text.strip()}")
                else:
                    results.append(
                        f"--- Physical Page {page_num} | Printed page: [unknown] ---\n"
                        f"[No extractable text — likely scanned image or diagram]"
                    )
        return "\n\n".join(results)
    except Exception as e:
        return f"Error reading PDF pages: {e}"


def run_search_pdf_text(path: str, query: str, max_results: int = 10) -> str:
    """Search for text across all PDF pages. Returns physical page numbers and context."""
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
                    printed = _extract_printed_page_number(text)
                    # Find context around match
                    idx = text.lower().find(query_lower)
                    start = max(0, idx - 80)
                    end = min(len(text), idx + len(query) + 120)
                    snippet = text[start:end].replace("\n", " ").strip()
                    results.append(
                        f"Physical page {page_num} (printed: {printed}): ...{snippet}..."
                    )
                    if len(results) >= max_results:
                        break
        if not results:
            return f"No matches found for: {query!r}"
        return f"Found {len(results)} match(es) for {query!r}:\n\n" + "\n\n".join(results)
    except Exception as e:
        return f"Error searching PDF: {e}"


def run_render_pdf_pages(path: str, pages: str, dpi: int = 150) -> str:
    """Render PDF pages as JPEG images using PyMuPDF. Returns list of saved file paths."""
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
        if saved:
            return "Rendered pages:\n" + "\n".join(saved)
        return "No pages rendered."
    except Exception as e:
        return f"Error rendering PDF pages: {e}"


def run_open_files(paths: str) -> str:
    """Open files with xdg-open if display is available; otherwise report paths for manual access."""
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
                subprocess.Popen(
                    ["xdg-open", fp],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
    msg = (
        f"Images saved ({len(found)} file(s)):\n" +
        "\n".join(f"  {f}" for f in found)
    )
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
            title   = r.get("title", "")
            link    = r.get("link", "")
            snippet = r.get("snippet", "")
            results.append(f"**{title}**\n{link}\n{snippet}")
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
            "Always call this FIRST (after load_skill) to discover available documents "
            "before deciding which PDF to search."
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
        "description": "Get PDF metadata: total page count and file size. Call this before reading any PDF.",
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
            "Returns physical page numbers AND detected printed page numbers where the text appears. "
            "Use this to DIRECTLY locate section headings, terms, or table labels "
            "without manual page-offset arithmetic. "
            "Example queries: '4.4.2', '自動滑門', 'ASD Preventive Maintenance', '預防性維修計畫'. "
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
            "Render PDF pages as JPEG images. Use AFTER confirming the correct physical "
            "page range via read_pdf_pages or search_pdf_text. Images saved to output/ directory. "
            "Max 20 pages per call. Use physical page numbers (not printed page numbers)."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path to PDF"},
            "pages": {"type": "string", "description": "Physical page numbers to render: '42-45', '10,12'"},
            "dpi": {"type": "integer", "description": "Resolution (default 150, use 200 for better quality)"}
        }, "required": ["path", "pages"]}
    }},
    {"type": "function", "function": {
        "name": "open_files",
        "description": "Open files with the system viewer (xdg-open). Use after render_pdf_pages to display images.",
        "parameters": {"type": "object", "properties": {
            "paths": {"type": "string", "description": "Comma-separated absolute file paths to open"}
        }, "required": ["paths"]}
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Use for supplemental technical information not in the manual.",
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
    from openai import BadRequestError
    sub_messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(30):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SUBAGENT_SYSTEM}] + sub_messages,
                tools=CHILD_TOOLS,
                max_tokens=16000,
            )
        except BadRequestError as e:
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
    while True:
        micro_compact(messages)
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)

        from openai import BadRequestError
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM}] + messages,
                tools=PARENT_TOOLS,
                max_tokens=16000,
            )
        except BadRequestError as e:
            print(f"\033[31m[BadRequestError] {str(e)[:400]}\033[0m")
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
                preview = str(output)[:300]
                print(f"> read_pdf_pages (p.{args.get('pages', '?')}): {preview}...")
            elif name == "search_pdf_text":
                q = args.get("query", "")
                preview = str(output)[:400]
                print(f"> search_pdf_text ({q!r}): {preview}...")
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

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})

        # Warn if agent rendered without having read the next page to confirm section end
        tool_names_this_round = [b.function.name for b in (msg.tool_calls or [])]
        if "render_pdf_pages" in tool_names_this_round and "read_pdf_pages" not in tool_names_this_round:
            messages.append({"role": "user", "content":
                "<reminder>你是否已讀過該章節最後一頁的下一頁，確認章節已結束？"
                "如果還沒，請先讀下一頁再決定是否完成。</reminder>"
            })

        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)


# ── Main ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    missing = []
    if not PDFPLUMBER_AVAILABLE:
        missing.append("pdfplumber")
    if not FITZ_AVAILABLE:
        missing.append("pymupdf")
    if missing:
        print(f"\033[33m警告：缺少套件：{', '.join(missing)}\033[0m")
        print(f"請執行：pip install {' '.join(missing)}")

    print("\033[32m=== MRT 維修手冊 AI Agent ===\033[0m")
    print(f"PDF 目錄: {WORKDIR}/MRT_docs/")
    print(f"輸出目錄: {OUTPUT_DIR}/")
    print("功能：PDF 文字導航 + 頁面圖像渲染 + 自動開啟")
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
