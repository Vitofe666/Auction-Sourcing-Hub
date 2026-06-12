import os
from urllib.parse import urlparse

import anthropic
from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel

app = FastAPI()

# Set SCRAPER_API_KEY on Render; n8n must send it as an X-API-Key header.
# If unset (e.g. local dev), auth is skipped.
API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

# Set ANTHROPIC_API_KEY on Render to enable the AI buy-analysis report.
# If unset, /scrape returns the raw lot data only.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
MAX_REPORT_IMAGES = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class TargetURL(BaseModel):
    url: str


# Ported from extension/scrapers.js — runs inside the page context.
# Strategy: prefer Saleroom's inline `baseProps` analytics object and the
# `.tinyMCEContent` description block; fall back to generic selectors.
SCRAPE_JS = r"""
() => {
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

  const firstMatch = (selectors) => {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  };

  // Strip the CDN resize query (?w=540&h=360) so Notion gets the full-size image.
  const fullSize = (url) => (url ? url.split("?")[0] : "");

  // Saleroom inlines a FLAT analytics object in a <script> tag:
  //   baseProps: {"Lot Number":"132","Auction House Name":"...",
  //   "Auction End Time UTC":"2026-06-10T08:00:00Z", ...}
  let props = {};
  try {
    for (const s of document.querySelectorAll("script:not([src])")) {
      const code = s.textContent;
      if (!code || code.indexOf("baseProps") === -1) continue;
      const m = code.match(/baseProps\s*:\s*(\{[^}]*\})/);
      if (m) { props = JSON.parse(m[1]); break; }
    }
  } catch (e) { /* fall through to selector-based fallbacks */ }

  const lotNumber = (() => {
    const p = props["Lot Number"];
    if (p) return (String(p).match(/\d+[A-Za-z]?/) || [String(p)])[0];
    const grab = (s) => {
      const m = (s || "").match(/\bLot\s+(\d+[A-Za-z]?)\b/i);
      return m ? m[1] : "";
    };
    for (const el of document.querySelectorAll("h1, h2, h3, h4, [class*='lot' i], [data-testid*='lot']")) {
      const v = grab(el.textContent);
      if (v) return v;
    }
    const fromTitle = grab(document.title);
    if (fromTitle) return fromTitle;
    // Derive from the prev/next navigation: "Prev lot: 32" → this lot is 33.
    const bodyText = document.body ? document.body.innerText : "";
    let m = bodyText.match(/Prev(?:ious)?\s*lot:?\s*(\d+)/i);
    if (m) return String(parseInt(m[1], 10) + 1);
    m = bodyText.match(/Next\s*lot:?\s*(\d+)/i);
    if (m) return String(parseInt(m[1], 10) - 1);
    return "";
  })();

  const auctionHouse = (() => {
    if (props["Auction House Name"]) return props["Auction House Name"];
    // Derive from the URL slug: .../auction-catalogues/<house-slug>/catalogue-id-...
    const m = location.pathname.match(/auction-catalogues\/([^/]+)\//);
    if (m) {
      return m[1].split("-").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
    }
    const el = firstMatch([
      "[data-testid='auction-house']",
      ".auction-house-name",
      "nav.breadcrumb a[href*='/auction-catalogues/']",
    ]);
    return el ? text(el) : "";
  })();

  const auctionDate = (() => {
    const utc = props["Auction End Time UTC"];
    if (utc) return String(utc).slice(0, 10);
    const timeEl = document.querySelector("time[datetime]");
    if (timeEl && timeEl.getAttribute("datetime")) {
      return timeEl.getAttribute("datetime").slice(0, 10);
    }
    return "";
  })();

  const imageUrl = (() => {
    // og:image is the canonical hero image; the first one is the lot itself.
    const og = document.querySelector("meta[property='og:image']");
    if (og && og.content && og.content.includes("globalauctionplatform")) {
      return fullSize(og.content);
    }
    const img = firstMatch([
      ".image img[data-lazy]",
      ".lot-image img",
      ".main-image img",
      "[data-testid='lot-image'] img",
    ]);
    if (img) return fullSize(img.getAttribute("data-lazy") || img.currentSrc || img.src);
    return og && og.content ? fullSize(og.content) : "";
  })();

  // All gallery photos (front, back, hallmarks, box...) — the AI condition
  // analysis needs more than the hero shot.
  const imageUrls = (() => {
    const urls = [];
    // The page also shows "related lots" from OTHER auctions — only keep
    // images from this lot's own CDN folder / catalogue.
    const heroDir = imageUrl ? imageUrl.slice(0, imageUrl.lastIndexOf("/") + 1) : "";
    const catMatch = location.pathname.match(/catalogue-id-([^/]+)/);
    const catId = catMatch ? catMatch[1] : "";
    const belongs = (u) =>
      (heroDir && u.startsWith(heroDir)) || (catId && u.includes("/" + catId + "/"));
    const add = (u) => {
      if (!u || !u.startsWith("http")) return;
      if (/placeholder|spacer|logo|favicon|blank-image/i.test(u)) return;
      const clean = fullSize(u);
      if (!belongs(clean)) return;
      if (!urls.includes(clean)) urls.push(clean);
    };
    add(imageUrl);
    for (const img of document.querySelectorAll(
      ".image img[data-lazy], .lot-image img, .image-gallery img, .thumbnail-image img, [class*='gallery' i] img, [class*='thumb' i] img"
    )) {
      add(img.getAttribute("data-lazy") || img.currentSrc || img.src);
    }
    return urls.slice(0, 8);
  })();

  const title = (() => {
    const h1 = document.querySelector("h1");
    if (h1) return text(h1);
    const og = document.querySelector("meta[property='og:title']");
    return og ? og.content : "";
  })();

  // Catalogue description AND condition notes both live in `.tinyMCEContent`,
  // separated by a "Condition:" or "Condition report" label.
  let description = "";
  let condition = "";
  const tiny = document.querySelector(".tinyMCEContent");
  if (tiny) {
    const raw = tiny.innerText.replace(/\u00a0/g, " ").trim();
    const idx = raw.search(/condition\s+report\s*:?|condition\s*:/i);
    if (idx !== -1) {
      description = raw.slice(0, idx).trim();
      condition = raw.slice(idx).replace(/^condition(\s+report)?\s*:?/i, "").trim();
    } else {
      description = raw;
    }
  }

  return { lotNumber, auctionHouse, auctionDate, imageUrl, imageUrls, title, description, condition };
}
"""

REPORT_SYSTEM_PROMPT = """You are an expert jewellery, watch and antiques auction analyst producing pre-bid buy reports for a professional reseller sourcing from UK auction houses.

You receive a lot's catalogue title, description, the auction house's published condition text, auction metadata, and photographs. Analyse the photographs carefully — front, reverse, clasps, hallmarks, settings, damage. Published condition text is often minimal or just a reference code, so your own visual assessment is the core of the report. Note anything visible: cracks, chips, repairs, lead solder, replaced parts, wear to high points, missing stones.

Write the report in Markdown using EXACTLY this structure:

## RECOMMENDATION
**<score> / 10 — <BUY | CONDITIONAL BUY | PASS>**
- + <key strength> (one bullet per strength)
- – <key risk / warning> (one bullet per risk)

Auction estimate: £X–£Y (€X–€Y)
Retail estimate: £X–£Y (€X–€Y)

## BASIC ITEM OVERVIEW
## GEMSTONE / ARTWORK ANALYSIS
(include an Artistic/Quality grade out of 10 and, where attribution is claimed, probability estimates such as "Workshop/circle: 35%")
## DIAMOND DETAILS
(type, estimated total carat weight, cut, colour/clarity ranges, % piqué — or "N/A" line if no diamonds)
## METAL & SCRAP VALUE
(estimated gross/net weights, carat/fineness probabilities, scrap calculation showing the per-gram rates used)
## PERIOD & WORKMANSHIP
## CONDITION REPORT
(bullets; end with **Condition grade: X / 10**)
## WARNINGS
(bullets — every material risk a bidder must know)
## COLLECTABILITY & INVESTMENT
(Positives and Negatives bullet lists)
## OVERALL SUMMARY
## FINAL RECOMMENDATION
**Buy score: X / 10** — Maximum hammer price: £X (€Y)
(one short paragraph of rationale)

Rules:
- All valuations in GBP first with EUR in parentheses; assume £1 = €1.15 and state any rate you use.
- For metal value use approximate current spot prices and explicitly state the per-gram rates assumed.
- Never present unverifiable facts as certain. Metal is "untested", attribution is a probability, treatments are estimated likelihoods.
- Be commercially blunt: the reader is deciding whether to bid and how much. Account for buyer's premium ~30% incl. VAT on top of hammer when setting the maximum hammer price.
- If the lot is not jewellery (furniture, art, ceramics...), adapt the GEMSTONE/DIAMOND sections to the relevant material analysis and keep every other section.
- Output ONLY the markdown report — no preamble, no closing remarks."""


async def generate_ai_report(data: dict) -> str:
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    facts = f"""Lot metadata:
- Title: {data.get('title') or 'unknown'}
- Lot number: {data.get('lotNumber') or 'unknown'}
- Auction house: {data.get('auctionHouse') or 'unknown'}
- Auction date: {data.get('auctionDate') or 'unknown'}
- Lot URL: {data.get('sourceUrl')}

Catalogue description:
{data.get('description') or '(none published)'}

Published condition text:
{data.get('condition') or '(none published)'}

The attached photographs are the lot's gallery images. Produce the buy report."""

    content = []
    for url in (data.get("imageUrls") or [data.get("imageUrl")])[:MAX_REPORT_IMAGES]:
        if url:
            content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({"type": "text", "text": facts})

    async with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=REPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        message = await stream.get_final_message()

    return "".join(block.text for block in message.content if block.type == "text").strip()


# Render needs a simple health check to confirm the server successfully started
@app.get("/")
async def health_check():
    return {
        "status": "healthy",
        "service": "saleroom-scraper",
        "version": "1.4",
        "aiReportEnabled": bool(ANTHROPIC_API_KEY),
    }


@app.post("/scrape")
async def scrape_auction(target: TargetURL, x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    # The site's real domain is the-saleroom.com; accept the no-hyphen variant too.
    host = urlparse(target.url).hostname or ""
    allowed = ("the-saleroom.com", "thesaleroom.com")
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise HTTPException(status_code=400, detail="Only the-saleroom.com lot URLs are supported")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            # baseProps and og: tags are in the initial HTML, so domcontentloaded
            # is enough — much faster than waiting for networkidle.
            await page.goto(target.url, wait_until="domcontentloaded", timeout=60000)

            # Best-effort wait for the description block; baseProps still works without it.
            try:
                await page.wait_for_selector(".tinyMCEContent", timeout=8000)
            except Exception:
                pass

            data = await page.evaluate(SCRAPE_JS)

            # Parts of the page can hydrate after domcontentloaded — if the first
            # pass is incomplete, give it a moment and try once more.
            if not data["lotNumber"] or not (data["description"] or data["condition"]):
                await page.wait_for_timeout(2000)
                data = await page.evaluate(SCRAPE_JS)

            data["sourceUrl"] = target.url

            if not any([data["lotNumber"], data["title"], data["description"]]):
                raise HTTPException(
                    status_code=422,
                    detail="Page loaded but no lot data found — check that the URL is a lot page",
                )

            # AI buy-analysis report (optional — requires ANTHROPIC_API_KEY).
            # A failure here must not lose the scraped data, so report errors
            # in-band instead of raising.
            data["aiReport"] = ""
            data["aiReportError"] = ""
            if ANTHROPIC_API_KEY:
                try:
                    data["aiReport"] = await generate_ai_report(data)
                except Exception as e:
                    data["aiReportError"] = f"AI report failed: {e}"

            return data

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scraping failed: {e}")
        finally:
            await browser.close()
