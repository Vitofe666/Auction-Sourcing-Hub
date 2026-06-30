import os
import re
import time
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

# Live metal pricing for the scrap-value calculation. We scrape Cooksongold's
# "Trade Scrap Price" table (GBP per gram, by carat). The dealer scrap value is
# trade-scrap-per-gram x 100 / SCRAP_MARGIN_DIVISOR x net metal weight — the
# 135 divisor reproduces the ~74% of trade-scrap that the reseller works to.
GOLD_PRICE_URL = os.environ.get("GOLD_PRICE_URL", "https://www.cooksongold.com/metalprices/")
SCRAP_MARGIN_DIVISOR = float(os.environ.get("SCRAP_MARGIN_DIVISOR", "135"))
SCRAP_CACHE_TTL = 3 * 60 * 60  # seconds; trade scrap prices update at most daily

# Rapaport diamond price guide. Copyright + weekly updates mean we don't bake the
# sheet into the repo: drop your current guide into this file and the report
# injects it verbatim. Absent -> the AI falls back to general Rapaport-style tiers.
RAPAPORT_GUIDE_FILE = os.environ.get(
    "RAPAPORT_GUIDE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference", "rapaport_guide.txt"),
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class TargetURL(BaseModel):
    url: str


# Ported from extension/scrapers.js — runs inside the page context.
# Multi-site: dispatches on hostname.
#  - the-saleroom.com / lot-tissimo.com: same Auction Technology Group platform —
#    prefer the inline `baseProps` analytics object and the `.tinyMCEContent`
#    description block; fall back to generic selectors. (lot-tissimo has no
#    dedicated branch; it falls through to this default path.)
#  - gildings.co.uk: server-rendered markup — read `.lot-title`, `.lot-number`,
#    `.lot-desc` (catalogue text + a separate "Condition Report" sub-block),
#    `.date-title`, and the bidpath CDN gallery images.
#  - easyliveauction.com: server-rendered markup — read og:title (full catalogue
#    line), `.lot-no`, the `/auctioneers/` link, the `.lot-status` date and the
#    images_lots gallery. No condition report is published inline on this site.
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

  // ---- Gildings (gildings.co.uk) ----
  // Static HTML; the condition report is published inline as a .lot-desc block
  // whose first child is a "Condition Report" .lot-sub-heading.
  if (/(^|\.)gildings\.co\.uk$/i.test(location.hostname)) {
    const lotNumber = (() => {
      const el = document.querySelector(".lot-number");
      const src = (el ? el.textContent : "") || document.title || "";
      const m = src.match(/Lot\s+(\d+[A-Za-z]?)/i) || src.match(/(\d+[A-Za-z]?)/);
      return m ? m[1] : "";
    })();

    const title = (() => {
      const el = document.querySelector(".lot-title");
      if (el) return text(el);
      const og = document.querySelector("meta[property='og:title']");
      return og ? og.content : "";
    })();

    // e.g. "30th Jun, 2026 10:30" -> "2026-06-30" (built by hand to avoid TZ drift).
    const auctionDate = (() => {
      const el = document.querySelector(".date-title");
      const raw = el ? text(el) : "";
      const m = raw.match(/(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})/);
      if (!m) return "";
      const months = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
                       jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
      const mo = months[m[2].slice(0, 3).toLowerCase()];
      if (!mo) return "";
      return m[3] + "-" + String(mo).padStart(2, "0") + "-" + String(m[1]).padStart(2, "0");
    })();

    // The catalogue description and the condition report are both .lot-desc
    // blocks; the condition one is flagged by a "Condition Report" sub-heading.
    let description = "";
    let condition = "";
    for (const d of document.querySelectorAll(".lot-desc")) {
      const heading = d.querySelector(".lot-sub-heading");
      if (heading && /condition/i.test(heading.textContent)) {
        // Use innerText on the live node so <br> separators become line breaks,
        // then strip the heading label and the "Request a condition report" button.
        let t = d.innerText.replace(/\u00a0/g, " ");
        t = t.replace(heading.innerText, "");
        const btn = d.querySelector(".condition-request, a[href*='Condition']");
        if (btn) t = t.replace(btn.innerText, "");
        condition = t.replace(/^[\s:–-]+/, "").trim();
      } else if (!description) {
        description = text(d);
      }
    }

    // Bidpath CDN serves -small/-medium/full variants (125351-0-small.jpg etc).
    // Normalise to the full-size file and dedupe.
    const toFull = (u) => fullSize(u).replace(/-(?:small|medium)(\.jpg)/i, "$1");
    const imageUrls = (() => {
      const urls = [];
      const add = (u) => {
        if (!u || !/bidpath\.cloud\/stock\//i.test(u)) return;
        const clean = toFull(u);
        if (!urls.includes(clean)) urls.push(clean);
      };
      for (const el of document.querySelectorAll(
        "[data-zoom-image], [data-high-res-src], [data-image], .lot-gallery-wrapper img, .lot-image img"
      )) {
        add(
          el.getAttribute("data-zoom-image") ||
          el.getAttribute("data-high-res-src") ||
          el.getAttribute("data-image") ||
          el.currentSrc || el.src
        );
      }
      const og = document.querySelector("meta[property='og:image']");
      if (og && og.content) add(og.content);
      return urls.slice(0, 8);
    })();
    const imageUrl = imageUrls[0] || (() => {
      const og = document.querySelector("meta[property='og:image']");
      return og && og.content ? toFull(og.content) : "";
    })();

    return {
      lotNumber, auctionHouse: "Gildings", auctionDate,
      imageUrl, imageUrls, title, description, condition,
    };
  }

  // ---- easyliveauction.com ----
  // Static HTML. The catalogue line is the title (the on-page .lot-desc-h1 is
  // truncated, so read the full text from og:title). No condition report is
  // published inline on this site (buyers request it from the auctioneer).
  if (/(^|\.)easyliveauction\.com$/i.test(location.hostname)) {
    const og = (sel) => {
      const m = document.querySelector(sel);
      return m && m.content ? m.content.trim() : "";
    };
    const title = og("meta[property='og:title']") || (document.title || "").trim();

    const lotNumber = (() => {
      const el = document.querySelector(".lot-no");
      const src = (el ? el.textContent : "") || document.title || "";
      const m = src.match(/Lot\s*(\d+[A-Za-z]?)/i) || src.match(/(\d+[A-Za-z]?)/);
      return m ? m[1] : "";
    })();

    const auctionHouse = (() => {
      // Prefer the auctioneer-specific link (/auctioneers/<slug>/), not the bare
      // "/auctioneers/" nav link. Its text is "by <House>".
      for (const a of document.querySelectorAll("a[href*='/auctioneers/']")) {
        const href = a.getAttribute("href") || "";
        if (!/\/auctioneers\/[^/]+\/?$/.test(href)) continue;
        const t = text(a).replace(/^by\s+/i, "").trim();
        if (t && !/^auctioneers$/i.test(t)) return t;
      }
      // Fallback: og:description ends with "... by <House>".
      const m = og("meta[property='og:description']").match(/\bby\s+([^.]+?)\s*$/i);
      return m ? m[1].trim() : "";
    })();

    // .lot-status holds "Auction Date: 23rd Jun 26 at ..." (note the 2-digit year).
    const auctionDate = (() => {
      const status = document.querySelector(".lot-status");
      const raw = (status ? status.innerText : "").replace(/\u00a0/g, " ");
      const after = raw.split(/Auction\s*Date\s*:?/i)[1] || raw;
      const m = after.match(/(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]{3,})\.?\s+(\d{2,4})/);
      if (!m) return "";
      const months = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
                       jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
      const mo = months[m[2].slice(0, 3).toLowerCase()];
      if (!mo) return "";
      const yr = m[3].length === 2 ? "20" + m[3] : m[3];
      return yr + "-" + String(mo).padStart(2, "0") + "-" + String(m[1]).padStart(2, "0");
    })();

    // Lot photos come as <id>.JPG (full), _PREVIEW and _THUMB variants; the
    // _LIVE.JPG frames are auctioneer webcam snapshots, not lot images.
    const toFull = (u) => fullSize(u).replace(/_(?:PREVIEW|THUMB|LARGE)(\.JPG)/i, "$1");
    const imageUrls = (() => {
      const urls = [];
      const add = (u) => {
        if (!u || !/content\.easyliveauction\.com\/auctions\/images_lots\//i.test(u)) return;
        if (/_LIVE\.JPG/i.test(u)) return;
        const clean = toFull(u);
        if (!urls.includes(clean)) urls.push(clean);
      };
      for (const img of document.querySelectorAll(
        "img[id^='main-image'], .lot-image-container img, .lot-image, #lot-images-gallery img"
      )) {
        add(img.currentSrc || img.getAttribute("src"));
      }
      add(og("meta[property='og:image']"));
      return urls.slice(0, 8);
    })();
    const imageUrl = imageUrls[0] || toFull(og("meta[property='og:image']"));

    return {
      lotNumber, auctionHouse, auctionDate, imageUrl, imageUrls,
      title, description: title, condition: "",
    };
  }

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

# ---- Live metal scrap pricing (Cooksongold "Trade Scrap Price") ----
_scrap_cache = {"at": 0.0, "data": None}


def parse_cookson_scrap(html: str) -> dict:
    """Parse the 'Trade Scrap Price' table -> {date, prices: [{name, perGram, unit}]}.

    The GBP/Unit column is per gram for gold/platinum/palladium and per kg for
    silver; we keep the unit so the report can convert if needed.
    """
    start = html.find("Trade Scrap Price")
    if start == -1:
        return {}
    end = html.find("</table>", start)
    seg = html[start: end if end != -1 else len(html)]
    date_m = re.search(r"(\w{3}\s+\d{1,2}\s+\w{3}\s+\d{4})", seg)
    rows = re.findall(
        r'indx_2"><span>\s*([^<]+?)\s*</span>.*?indx_4">\s*([\d.]+)\s*(GM|KG|OZ)?',
        seg, re.S,
    )
    prices = [{"name": n.strip(), "perGram": float(v), "unit": (u or "GM")} for n, v, u in rows]
    return {"date": date_m.group(1) if date_m else "", "prices": prices} if prices else {}


async def fetch_scrap_prices(browser) -> dict:
    """Best-effort scrape of the trade scrap table, cached for SCRAP_CACHE_TTL."""
    now = time.time()
    if _scrap_cache["data"] and now - _scrap_cache["at"] < SCRAP_CACHE_TTL:
        return _scrap_cache["data"]
    page = await browser.new_page(user_agent=USER_AGENT)
    try:
        await page.goto(GOLD_PRICE_URL, wait_until="domcontentloaded", timeout=45000)
        data = parse_cookson_scrap(await page.content())
        if data:
            _scrap_cache.update(at=now, data=data)
        return data
    finally:
        await page.close()


def format_scrap_prices(scrap: dict) -> str:
    if not scrap or not scrap.get("prices"):
        return "(live metal prices unavailable — estimate from general spot prices and say so)"
    lines = [f"Cooksongold Trade Scrap Prices (as of {scrap.get('date') or 'today'}):"]
    for p in scrap["prices"]:
        per = "per gram" if p["unit"] == "GM" else f"per {p['unit'].lower()}"
        lines.append(f"- {p['name']}: £{p['perGram']:.3f} {per}")
    return "\n".join(lines)


def load_rapaport_guide() -> str:
    try:
        with open(RAPAPORT_GUIDE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


REPORT_SYSTEM_PROMPT = """You are an expert jewellery appraiser and valuation AI producing pre-bid buy reports for a professional reseller sourcing from UK auction houses. Your reports follow a fixed "ficha" master format.

You receive a lot's catalogue title, description, the auction house's published condition text, auction metadata, photographs, the LIVE trade scrap metal prices, and a Rapaport diamond price guide. Analyse the photographs carefully — front, reverse, clasps, hallmarks, settings, damage. Published condition text is often minimal, so your own visual assessment is the core of the report. Note anything visible: cracks, chips, repairs, lead solder, replaced parts, wear to high points, missing stones.

OUTPUT: GitHub-flavoured Markdown only — no preamble, no closing remarks. Follow the structure, headings, field labels and order below EXACTLY. Keep every section even when it does not apply (write N/A). Use GIA scales for diamond colour and clarity ALWAYS, and ALWAYS include % PIQUÉ in the gemstone table. Be commercially conservative — UK auction reality, never optimistic.

Start with one bold title line: **Metal + Main Stones + Object Type + Period** (e.g. **18ct Gold Diamond Cluster Ring, Edwardian**).

PART 1 — DECISION GRID: output these FOUR markdown tables, in this exact order, each a 2-column table. Inside a table cell put each field on its own line using `<br>` (never a real newline), and use `**bold**` for labels. Do not put prose paragraphs between the tables.

TABLE 1:
| RECOMMENDATION | WARNINGS |
| --- | --- |
| **<score> / 10 — <STRONG BUY / BUY / CONDITIONAL BUY / SELECTIVE BUY / PASS>**<br>+ <strength><br>+ <strength><br>– <risk><br>– <risk><br><br>**Auction:** £X–£Y (€X–€Y)<br>**Retail:** £X–£Y (€X–€Y) | <risk phrase><br><condition note><br><market limitation> |

TABLE 2 (financial core — be precise):
| GEMSTONE | DIAMOND DETAILS |
| --- | --- |
| **Type:** Natural/Synthetic<br>**Total Carat:** ~Xct (est.)<br>**Cut:** ...<br>**Colour:** <GIA><br>**Clarity:** <GIA><br>**% Piqué:** X%<br>**Cut Quality:** ~X%<br>**Origin:** <value or probabilities><br>**Treatment:** <probabilities> | **Type:** ... (or N/A)<br>**Quantity:** ...<br>**Total Weight:** ~Xct<br>**Cut:** ...<br>**Colour:** <GIA><br>**Clarity:** <GIA><br>**% Piqué:** X%<br>**Cut Quality:** ~X%<br>**Treatment:** ...<br>**Est. Rapaport Value:** £... |

TABLE 3:
| METAL / VALUE | PERIOD / WORKMANSHIP |
| --- | --- |
| **Metal:** ... (tested / hallmarked / untested — state which)<br>**Gross Weight:** Xg<br>**Net Metal Weight:** Xg<br>**Scrap Value:** £X (€Y)<br>**Value Basis:** scrap-led / gem-led / design-led | **Period:** ...<br>**Construction:** ...<br>**Quality:** ...<br>**Style / Maker:** ... |

TABLE 4:
| SIZE / NOTES | CONDITION |
| --- | --- |
| **Dimensions:** ...<br>**Design:** ... | **Overall:** ...<br><stones present/missing><br><wear / repairs><br><structural integrity> |

PART 2 — DETAILED ANALYSIS: then output these sections as normal markdown (## headings, `- ` bullets, `### ` per-stone sub-headings, `**bold**`).

## WARNINGS
- <every material risk: treatments, structural issues, replacements, market limitations — no repetition, risks only>

## BASIC ITEM OVERVIEW
<short paragraph: what it is, style, construction, era context — no fluff>

## GEMSTONE ANALYSIS
### <Stone name>
For diamonds: **Colour** (GIA, with comment), **Clarity** (GIA, with comment), **% Piqué** explanation, **Cut Quality %**, **Total Carat**, plus observations (old vs modern cuts, matching, brilliance). For coloured stones: **Dimensions (mm)**, **Estimated Carat**, **Colour /10**, **Clarity /10**, **Origin probability %**, **Treatment probability %**. Repeat a `### ` block per stone type.

## METAL VALUE CALCULATION
Show the full workings using the LIVE trade scrap price for the item's carat (provided below):
1. Gross weight = Xg
2. Less estimated stone weight ≈ Xg
3. Net metal weight ≈ Xg
4. Trade scrap price for <carat> = £P/g (from the live prices below)
5. Scrap value = P ÷ 135 × 100 × net weight = £X (€Y)
**Scrap Value: £X (€Y)**
(If the gross weight is not supplied, state that the scrap value cannot be finalised and list these steps to complete once it is. Always keep the maths visible.)

## CRAFTSMANSHIP & PERIOD
- <manufacturing quality, construction method, era justification>

## CONDITION REPORT
- <bullets: stones present/missing, wear, repairs, structural integrity>
**Condition grade: X / 10**

## COLLECTABILITY & INVESTMENT
**Positives**
- <design / era / stones / wearability>
**Negatives**
- <colour / clarity / damage / market demand>

## OVERALL SUMMARY
<short paragraph: weight, scrap baseline, commercial positioning>

## RECOMMENDATION RATIONALE
**Positives**
- ... (no repetition of earlier sections)
**Negatives**
- ...

## FINAL RECOMMENDATION
**Buy – X / 10**
**Maximum Hammer Price: £X (€Y)**
<one line of clear reasoning>

VALUATION RULES (non-negotiable):
- All valuations GBP first with EUR in parentheses; use £1 = €1.16 unless you state otherwise.
- METAL: use the LIVE trade scrap prices supplied below and the formula scrap = (trade scrap £/g for that carat) ÷ 135 × 100 × net metal weight. State the carat/fineness assumed and whether the metal is tested/hallmarked/untested.
- DIAMONDS: cross-reference Cut, Colour (GIA), Clarity (GIA) and Carat against the Rapaport guide supplied below and estimate value from it; Rapaport list prices are in hundreds of US$ per carat — apply realistic trade discounts and convert to GBP. If the guide is absent, apply general Rapaport-style tiers and say so.
- Anchor every valuation to the scrap floor, then classify the piece as scrap-led, gem-led or design-led.
- Separate Auction (liquidity reality) from Retail (achievable, not inflated). Account for buyer's premium (~28–32% incl. VAT) on top of the hammer when setting the maximum hammer price.
- Never present unverifiable facts as certain: metal is "untested" until assayed, attribution/origin are probabilities, treatments are estimated likelihoods. No vague wording ("good diamonds").
- If the lot is not jewellery (furniture, art, ceramics, watches...), keep every heading and table but adapt the GEMSTONE/DIAMOND/METAL content to the relevant material analysis, writing N/A where a field has no equivalent."""


async def generate_ai_report(data: dict, scrap: dict | None = None) -> str:
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    rapaport = load_rapaport_guide()
    rapaport_block = rapaport or "(no Rapaport guide file configured — apply general Rapaport-style tiers and say so)"

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

=== LIVE TRADE SCRAP METAL PRICES (use for the METAL VALUE CALCULATION) ===
{format_scrap_prices(scrap or {})}

=== RAPAPORT DIAMOND PRICE GUIDE (use for diamond valuation) ===
{rapaport_block}

The attached photographs are the lot's gallery images. Produce the buy report in the ficha master format."""

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
        "service": "auction-scraper",
        "version": "1.8",
        "aiReportEnabled": bool(ANTHROPIC_API_KEY),
    }


@app.post("/scrape")
async def scrape_auction(target: TargetURL, x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    # the-saleroom.com (accept the no-hyphen variant too), gildings.co.uk,
    # easyliveauction.com and lot-tissimo.com. lot-tissimo shares the-saleroom's
    # Auction Technology Group platform, so it uses the default scraper branch.
    host = urlparse(target.url).hostname or ""
    allowed = ("the-saleroom.com", "thesaleroom.com", "gildings.co.uk",
               "easyliveauction.com", "lot-tissimo.com")
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise HTTPException(
            status_code=400,
            detail="Only the-saleroom.com, gildings.co.uk, easyliveauction.com and lot-tissimo.com lot URLs are supported",
        )

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

            # Some sites (e.g. lot-tissimo) sit behind an AWS WAF JavaScript
            # challenge that auto-solves and reloads to the real lot page. Detect
            # the interstitial and wait for it to clear before scraping.
            try:
                on_challenge = await page.evaluate(
                    "() => !!(window.gokuProps || document.getElementById('challenge-container'))"
                )
            except Exception:
                on_challenge = False
            if on_challenge:
                for waiter in (
                    lambda: page.wait_for_function(
                        "() => !(window.gokuProps || document.getElementById('challenge-container'))",
                        timeout=30000,
                    ),
                    lambda: page.wait_for_load_state("networkidle", timeout=15000),
                ):
                    try:
                        await waiter()
                    except Exception:
                        pass

            # Best-effort wait for the description block; the rest still works
            # without it. Each site renders its lot text in a different element.
            if host.endswith("gildings.co.uk"):
                desc_selector = ".lot-desc"
            elif host.endswith("easyliveauction.com"):
                desc_selector = ".lot-desc-h1"
            else:
                desc_selector = ".tinyMCEContent"
            try:
                await page.wait_for_selector(desc_selector, timeout=8000)
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
                # Live trade scrap prices for the metal calculation (best-effort).
                scrap = {}
                try:
                    scrap = await fetch_scrap_prices(browser)
                except Exception as e:
                    print(f"[scrap] price fetch failed: {e}")
                data["scrapPrices"] = scrap
                try:
                    data["aiReport"] = await generate_ai_report(data, scrap)
                except Exception as e:
                    data["aiReportError"] = f"AI report failed: {e}"

            return data

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scraping failed: {e}")
        finally:
            await browser.close()
