# MRT 維修手冊 AI Agent

針對新北捷運維修手冊（多份 PDF）的 AI 問答系統。根據用戶問題自動挑選最相關的文件，定位正確頁面，並輸出原始頁面圖像供工程師參考。

## 功能特色

- **多 PDF 支援**：自動掃描 `MRT_docs/` 下所有 PDF，根據問題關鍵字與檔名排序，優先查詢最相關的文件
- **PDF 文字導航**：用 pdfplumber 讀取文字層，快速定位 TOC 與相關章節
- **自動頁碼校正**：自動偵測 footer 的印刷頁碼（格式如 `- 92 -`），動態修正 physical page 與 printed page 的偏移差異
- **關鍵字搜尋**：`search_pdf_text` 直接全文搜尋，不靠猜頁碼
- **原始頁面圖像**：確認正確頁面後，用 PyMuPDF 渲染成 JPEG，讓工程師看到原始表格與圖面
- **結構化輸出**：回答包含章節標題、印刷頁碼範圍、重點摘要，以及 `<AvailableImageFiles>` 標籤供 UI 整合
- **Context 壓縮**：自動 micro-compact + auto-compact，支援長對話
- **TodoManager**：追蹤多步驟任務進度
- **Subagent**：可委派子任務給獨立 agent 處理
- **Web Search**：透過 SerpAPI 補充查詢外部技術資訊

## 目錄結構

```
read_MRT_doc/
├── mrt_agent.py          # 主程式
├── MRT_docs/             # 放置 PDF 維修手冊（支援多份）
├── output/               # 渲染後的 JPEG 頁面圖像
├── skills/
│   ├── pdf-reading/      # PDF 閱讀最佳實踐 skill
│   └── pdf-page-offset/  # 頁碼偏移處理 skill
├── .transcripts/         # 自動壓縮時儲存的對話記錄
├── .env                  # API 金鑰設定（不進 git）
├── .env_template         # API 金鑰範本
└── README.md
```

## 安裝

```bash
pip install pdfplumber pymupdf python-dotenv openai pyyaml
```

## 設定

```bash
cp .env_template .env
# 填寫 .env 內的 API 金鑰
```

```env
AZURE_OPENAI_ENDPOINT=https://your-resource-name.openai.azure.com/
AZURE_OPENAI_API_KEY=your-azure-openai-api-key
AZURE_OPENAI_API_VERSION=2025-01-01-preview
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o
SERP_API_KEY=your-serpapi-key   # 選用，web search 功能需要
```

## 新增 PDF

直接把 PDF 放入 `MRT_docs/`，Agent 下次啟動時會自動偵測。

```bash
cp 新手冊.pdf MRT_docs/
```

## 使用方式

```bash
python3 mrt_agent.py
```

輸入問題，例如：

```
mrt >> ASD 自動滑門的預防性維修週期是什麼？
mrt >> 告訴我緊急門/月臺端門預防性維修計畫
mrt >> LCB 閉鎖機構的矯正性維修步驟為何？
```

## 工作流程

Agent 每次回答的標準流程：

1. 載入 `pdf-reading` skill（頁碼偏移規則）
2. `list_pdfs()` 列出 `MRT_docs/` 下所有 PDF
3. 根據問題關鍵字與檔名，排出最相關 PDF 的優先順序，並說明理由
4. 對排序第一的 PDF 執行 `search_pdf_text(關鍵字)` 快速確認有內容；若無則換下一個
5. 讀取 TOC（前 15 頁），列出完整目錄
6. 再次 `search_pdf_text` 取得精確 physical page
7. `read_pdf_pages` 逐頁確認，每頁 header 顯示 `Printed page number detected: [N]`
8. 讀到下一個章節標題出現，確認節尾（必讀下一頁才能停）
9. `render_pdf_pages` 輸出 JPEG 到 `output/`
10. 回答（繁體中文）+ `<AvailableImageFiles>` 標籤

## 輸出格式範例

```
**章節**：4.4.4 緊急門/月臺端門預防性維修計畫 PREVENTIVE MAINTENANCE PLAN - EED / PED
**印刷頁碼範圍**：第92-93頁
**重點摘要**：
- 緊急推桿功能檢查：每年一次，使用扳手、螺絲起子
- 毛刷檢查：每年一次，使用扳手、螺絲起子

<AvailableImageFiles>系統概述(PSDS-M6)_號誌系統_-_月臺門操作及維修手_p0102.jpg, 系統概述(PSDS-M6)_號誌系統_-_月臺門操作及維修手_p0103.jpg</AvailableImageFiles>
```

## 工具清單

| 工具 | 用途 |
|------|------|
| `list_pdfs` | 列出 MRT_docs/ 下所有 PDF 檔名與大小 |
| `get_pdf_info` | 取得單一 PDF 的總頁數與檔案大小 |
| `read_pdf_pages` | 讀取文字層（TOC 導航、頁碼確認用） |
| `search_pdf_text` | 全文關鍵字搜尋，回傳 physical + printed page |
| `render_pdf_pages` | 渲染頁面為 JPEG（DPI 可調，預設 150） |
| `open_files` | 用系統檢視器開啟圖片（有 display 時） |
| `web_search` | SerpAPI 網路搜尋（補充外部資訊） |
| `load_skill` | 載入專業知識 skill |
| `todo` | 多步驟任務追蹤 |
| `task` | 委派子任務給 subagent |
| `compact` | 手動觸發 context 壓縮 |

## 技術說明

### 頁碼偵測

文件 footer 格式：`SYL-TK01-OPM-ESN-0005-0A - 92 - OCT, 2025`

使用 regex ` - (\d+) - `（要求兩側有空格），避免誤抓文件編號中的 `0005`。

### Context 壓縮策略

| 層次 | 觸發條件 | 動作 |
|------|----------|------|
| micro_compact | 超過 6 個舊 tool result | 清除舊 tool 結果（保留 load_skill） |
| auto_compact | 估計 token > 50,000 | 摘要整段對話，存 transcript |
| manual compact | 呼叫 `compact` 工具 | 同 auto_compact |

## 注意事項

- 僅支援有文字層的 PDF（非純掃描圖）
- 圖像渲染預設 DPI 150，需要更高解析度可指定 `dpi=200`
- Docker／無顯示器環境：JPEG 存至 `output/`，由 UI 或使用者從 host 端開啟
