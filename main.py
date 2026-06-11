import os
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel

app = FastAPI()

# Set SCRAPER_API_KEY on Render; n8n must send it as an X-API-Key header.
# If unset (e.g. local dev), auth is skipped.
API_KEY = os.environ.get("SCRAPER_API_KEY", "")

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

  return { lotNumber, auctionHouse, auctionDate, imageUrl, title, description, condition };
}
"""


# Render needs a simple health check to confirm the server successfully started
@app.get("/")
async def health_check():
    return {"status": "healthy", "service": "saleroom-scraper", "version": "1.1"}


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
            return data

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scraping failed: {e}")
        finally:
            await browser.close()
