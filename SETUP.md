# Auction Sourcing Hub — Automated Notion Pipeline

Paste a lot URL into Notion → n8n picks it up → the Render-hosted scraper
extracts the lot data → n8n writes the properties back and builds a formatted
Condition Report inside the page body. Supported sites: **the-saleroom.com**,
**lot-tissimo.com**, **gildings.co.uk** and **easyliveauction.com**.

```
┌─────────┐  poll (1 min)  ┌─────────────┐  POST /scrape   ┌──────────────────┐
│ Notion  │ ─────────────▶ │ n8n (Render)│ ──────────────▶ │ FastAPI+Playwright│
│ database│ ◀───────────── │  workflow   │ ◀────────────── │ scraper (Render)  │
└─────────┘  PATCH page    └─────────────┘   lot JSON      └──────────────────┘
```

The browser extension in `extension/` is now legacy — you can delete the folder
once this pipeline is running.

---

## 1. Notion setup

### 1.1 Create the integration
1. Go to https://www.notion.so/my-integrations → **New integration**.
2. Capabilities: Read content, Update content, Insert content.
3. Copy the **Internal Integration Secret** (starts with `ntn_` or `secret_`).

### 1.2 Create the database
Create a database with **exactly these property names** (the workflow maps by name):

| Property        | Type           | Notes                                              |
|-----------------|----------------|----------------------------------------------------|
| `Name`          | Title          | Auto-filled with the lot title                     |
| `Lot URL`       | URL            | **You paste the saleroom URL here** — the trigger  |
| `Lot Number`    | Text           | Filled by the workflow                             |
| `Auction House` | Select         | Filled by the workflow (options auto-created)      |
| `Auction Date`  | Date           | Filled by the workflow                             |
| `Photo`         | Files & media  | Filled with the full-size lot image (external URL) |
| `Status`        | Select         | Options: `Pending`, `Done`, `Error`                |

> ⚠️ Name the URL property **`Lot URL`**, not `URL`. n8n's simplified trigger
> output already contains a `url` field (the Notion page's own link), and a
> property literally named "URL" can collide with it.

### 1.3 Share the database with the integration
Open the database → `•••` menu → **Connections** → add your integration.
(Without this, every API call returns `object_not_found`.)

### 1.4 Get the database ID
From the database URL `https://notion.so/myworkspace/abc123def456...?v=...`,
the ID is the 32-char hex string before `?v=`.

---

## 2. Render — scraper service

1. Push this repo to GitHub.
2. Render dashboard → **New → Web Service** → connect the GitHub repo.
3. Settings:
   - **Runtime:** Docker (Render auto-detects the `Dockerfile`).
   - **Instance type:** Free works, but it sleeps after 15 min idle — the first
     scrape after a sleep takes ~60–90 s (the n8n HTTP node is configured with a
     120 s timeout and one retry to absorb this). Starter ($7/mo) removes the cold start.
   - **Environment variables:**
     - `SCRAPER_API_KEY` = a long random string (e.g. run `openssl rand -hex 32`).
       n8n must send this as the `X-API-Key` header.
     - `ANTHROPIC_API_KEY` = your Claude API key (enables the AI buy report).
     - *(optional)* `GOLD_PRICE_URL` — the live trade-scrap price source for the
       metal calculation. Defaults to `https://www.cooksongold.com/metalprices/`
       (the "Trade Scrap Price" table, GBP per gram by carat).
     - *(optional)* `SCRAP_MARGIN_DIVISOR` — defaults to `135`; scrap value =
       trade-scrap £/g ÷ 135 × 100 × net metal weight (i.e. ~74% of trade scrap).
     - *(optional)* `RAPAPORT_GUIDE_FILE` — path to the diamond price guide the
       AI uses. Defaults to `reference/rapaport_guide.txt` in the repo. Paste your
       current licensed Rapaport data into that file and refresh it weekly; if it
       is left as the placeholder the AI falls back to general Rapaport-style tiers.
4. Deploy, then smoke-test:

   ```bash
   curl https://YOUR-SCRAPER.onrender.com/          # → {"status":"healthy",...}

   curl -X POST https://YOUR-SCRAPER.onrender.com/scrape \
     -H "Content-Type: application/json" \
     -H "X-API-Key: YOUR_KEY" \
     -d '{"url":"https://www.thesaleroom.com/en-gb/auction-catalogues/.../lot-..."}'
   ```

   Expected response shape:

   ```json
   {
     "lotNumber": "132",
     "auctionHouse": "Fellows",
     "auctionDate": "2026-06-10",
     "imageUrl": "https://portal-images.azureedge.net/...jpg",
     "title": "A diamond ring...",
     "description": "...",
     "condition": "...",
     "sourceUrl": "https://www.thesaleroom.com/..."
   }
   ```

5. Auto-deploy is on by default: every push to `main` redeploys the scraper.

---

## 3. Render — n8n service

If you don't already have n8n running:

1. **New → Web Service** → choose **Existing image** → `docker.n8n.io/n8nio/n8n`.
2. Add a **Persistent Disk** (1 GB) mounted at `/home/node/.n8n` — without it,
   your workflows and credentials are wiped on every restart/deploy.
3. Environment variables:
   - `N8N_ENCRYPTION_KEY` = long random string (back it up — losing it bricks saved credentials)
   - `WEBHOOK_URL` = `https://YOUR-N8N.onrender.com/`
   - `GENERIC_TIMEZONE` = `Europe/London` (or yours)
   - `N8N_PORT` = `10000` (and set the service port to 10000)
4. **Important:** the Notion trigger is *polling* — it only fires while n8n is
   awake. On Render's free tier the service sleeps and your automation silently
   stops. Use a paid always-on instance for n8n (or an external uptime pinger,
   though Render discourages that).

---

## 4. n8n workflow

### Option A — import (fast)
1. n8n → **Workflows → Import from file** → [n8n/saleroom_workflow.json](n8n/saleroom_workflow.json).
2. Create a **Notion credential** (paste the integration secret) and select it
   on the four Notion-touching nodes.
3. Replace placeholders:
   - Notion Trigger → your **database ID**
   - "Scrape Lot" node → your Render scraper URL and `X-API-Key` value
4. Run once manually (paste a URL in a test row, click **Execute workflow**),
   then toggle **Active**.

### Option B — node-by-node (what each node does)

**① Notion Trigger** — event *Page Updated in Database*, poll every minute,
your database, "Simplify output" ON. *Updated* (not *Added*) is deliberate:
you usually create the row first and paste the URL a moment later — an
*Added* trigger would fire before the URL exists and never retry.

**② IF "Needs Scraping?"** — all of:
- `{{ $json['Lot URL'] }}` *is not empty*
- `{{ $json.Status }}` *not equals* `Done`
- `{{ $json.Status }}` *not equals* `Error`

This is the loop-breaker: when the workflow later updates the page, the trigger
fires again, but Status is now `Done` so the run stops here.

**③ HTTP Request "Scrape Lot"** — POST `https://YOUR-SCRAPER.onrender.com/scrape`,
JSON body `{"url": "{{ $json['Lot URL'] }}"}`, header `X-API-Key`, timeout
120 000 ms, retry on fail ×2. Error output (`On Error → Continue using error
output`) routes to ⑦.

**④ Code "Build Notion Payloads"** — builds two JSON payloads from the scraper
response: the database `properties` object and the page-body `children` blocks
array. It chunks text into ≤1900-char pieces (Notion caps rich_text items at
2000 chars), strips commas from select names (Notion rejects them), and omits
any property the scraper couldn't fill so you never overwrite data with blanks.
It also renders the AI report's Markdown — including the ficha decision-grid
**tables** (a `| … |` row followed by a `| --- |` separator → a Notion table
block; `<br>` inside a cell becomes a line break). If you imported the workflow
before this change, re-paste [n8n/build_notion_payloads.js](n8n/build_notion_payloads.js)
into this node so the tables render.

**⑤ HTTP Request "Update Properties"** — `PATCH https://api.notion.com/v1/pages/{{pageId}}`
with the properties payload, authenticated with the Notion credential
(*Predefined Credential Type → Notion API*), header `Notion-Version: 2022-06-28`.
Sets Title, Lot Number, Auction House (select), Auction Date, Photo (external
file), and Status = `Done`.

**⑥ HTTP Request "Write Page Body"** — `PATCH https://api.notion.com/v1/blocks/{{pageId}}/children`
appends the formatted report:

- 🔨 gray callout — `Lot 132 • Fellows • 2026-06-10`
- divider
- `🖼️ Lot Photo` heading + full-size image block
- divider
- `📝 Description` heading + paragraphs
- divider
- `🔍 Condition Report` heading + ⚠️ yellow callout with the condition text
- divider + bookmark of the source lot URL

**⑦ HTTP Request "Mark Error"** — on scraper failure, sets Status = `Error`
so the row is visibly flagged and won't be retried in a loop. To retry a failed
row, clear its Status and touch any property.

> The native n8n Notion node can update simple properties, but it can't build
> callouts/dividers/image blocks — that's why ⑤–⑦ use the HTTP Request node
> against the Notion API directly (same credential, full block support).

---

## 5. End-to-end test

1. Both Render services deployed and healthy; n8n workflow **Active**.
2. Add a row in Notion, paste a lot URL into **Lot URL**.
3. Within ~1 minute the row's Status flips to `Done`, the properties fill in,
   and the page body contains the formatted report.
4. If Status goes `Error`, check the n8n execution log — most common causes:
   - scraper cold-starting (free tier) → just retry, or upgrade the instance
   - database not shared with the integration (`object_not_found`)
   - property name mismatch (`validation_error` mentions the property)

## 6. Deployment checklist

- [ ] Repo pushed to GitHub; Render scraper service connected with auto-deploy
- [ ] `SCRAPER_API_KEY` set on the scraper; same value in the n8n "Scrape Lot" header
- [ ] `curl /scrape` smoke test returns lot JSON
- [ ] n8n on an always-on instance with a persistent disk and `N8N_ENCRYPTION_KEY`
- [ ] Notion integration created, secret stored in an n8n credential
- [ ] Database shared with the integration; property names match the table above
- [ ] Workflow imported, placeholders replaced, tested manually, then activated
