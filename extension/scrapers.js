// Attached to window so content.js can use it (no ES modules in classic content scripts).
// Strategy: prefer Saleroom's own structured data (the inline `baseProps` analytics
// object + the `.tinyMCEContent` description block), which are verified against real
// lot pages. Fall back to generic selectors for resilience / other layouts.
window.ASH_Scrapers = (() => {
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

  // Find a value next to a label like "Lot:" or "Auction:" — last-resort fallback.
  const findByLabel = (labelRegex) => {
    const nodes = document.querySelectorAll("dt, th, span, strong, b, label, div");
    for (const node of nodes) {
      if (labelRegex.test(node.textContent || "")) {
        const sibling =
          node.nextElementSibling ||
          (node.parentElement && node.parentElement.querySelector("dd, td, span:not(:first-child)"));
        if (sibling) return text(sibling);
      }
    }
    return "";
  };

  // ---- PRIMARY SOURCE: Saleroom inlines an analytics object in a <script> tag:
  //   var cfg = { ... baseProps: {"Lot Number":"132","Auction House Name":"...",
  //   "Auction End Time UTC":"2026-06-10T08:00:00Z", ...} ... }
  // baseProps is a FLAT object (no nested braces), so a non-greedy {...} capture is safe.
  let _propsCache = null;
  const baseProps = () => {
    if (_propsCache) return _propsCache;
    _propsCache = {};
    try {
      for (const s of document.querySelectorAll("script:not([src])")) {
        const code = s.textContent;
        if (!code || code.indexOf("baseProps") === -1) continue;
        const m = code.match(/baseProps\s*:\s*(\{[^}]*\})/);
        if (m) { _propsCache = JSON.parse(m[1]); break; }
      }
    } catch (e) {
      console.warn("[ASH] baseProps parse failed:", e);
    }
    return _propsCache;
  };

  const scrapeLotNumber = () => {
    const p = baseProps()["Lot Number"];
    if (p) return String(p).match(/\d+/)?.[0] || String(p);

    const el = firstMatch([
      "[data-testid='lot-number']",
      ".lot-number",
      "h1 .lot-number",
      "h1 [class*='lot']",
    ]);
    if (el) {
      const m = text(el).match(/\d+/);
      if (m) return m[0];
    }
    for (const h of document.querySelectorAll("h1, h2")) {
      const m = (h.textContent || "").match(/Lot\s+(\d+)/i);
      if (m) return m[1];
    }
    return findByLabel(/^\s*Lot\b/i).match(/\d+/)?.[0] || "";
  };

  const scrapeAuctionHouse = () => {
    const p = baseProps()["Auction House Name"];
    if (p) return p;

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
    if (el) return text(el);
    return findByLabel(/Auction(eer)?(\s*House)?/i);
  };

  const scrapeAuctionDate = () => {
    const utc = baseProps()["Auction End Time UTC"];
    if (utc) return String(utc).slice(0, 10);

    const timeEl = document.querySelector("time[datetime]");
    if (timeEl) {
      const iso = timeEl.getAttribute("datetime");
      if (iso) return iso.slice(0, 10);
    }
    const candidate = firstMatch([
      "[data-testid='auction-date']",
      ".auction-date",
      ".sale-date",
    ]);
    const raw = candidate ? text(candidate) : findByLabel(/^\s*(Sale|Auction)\s*Date/i);
    if (!raw) return "";
    const d = new Date(raw);
    return isNaN(d) ? raw : d.toISOString().slice(0, 10);
  };

  const scrapeImageUrl = () => {
    // og:image is the canonical hero image; the first one is the lot (the 2nd is the site logo).
    const og = document.querySelector("meta[property='og:image']");
    if (og?.content && og.content.includes("globalauctionplatform")) return fullSize(og.content);

    const img = firstMatch([
      ".image img[data-lazy]",
      ".lot-image img",
      ".main-image img",
      "[data-testid='lot-image'] img",
    ]);
    if (img) return fullSize(img.getAttribute("data-lazy") || img.currentSrc || img.src);
    if (og?.content) return fullSize(og.content);
    return "";
  };

  // The catalogue description AND condition notes both live in `.tinyMCEContent`:
  //   <p>...catalogue description...</p>  Condition: ...condition lines...
  // We return both, labelled, so the Notion callout shows the full picture.
  const scrapeConditionReport = () => {
    const el = document.querySelector(".tinyMCEContent");
    if (el) {
      const raw = el.innerText.replace(/\u00a0/g, " ").trim();
      const idx = raw.search(/condition\s*:/i);
      if (idx !== -1) {
        const description = raw.slice(0, idx).trim();
        const condition = raw.slice(idx).replace(/^condition\s*:/i, "").trim();
        return [
          description && `Description\n${description}`,
          condition && `Condition\n${condition}`,
        ].filter(Boolean).join("\n\n");
      }
      if (raw.length > 20) return raw; // description only, no explicit Condition section
    }

    // Generic fallback: a labelled "Condition Report" section / tab on other layouts.
    const headers = Array.from(
      document.querySelectorAll("h2, h3, h4, [role='tab'], button, summary")
    ).filter((e) => /condition\s*report/i.test(e.textContent || ""));
    for (const h of headers) {
      let sib = h.nextElementSibling;
      while (sib) {
        const t = text(sib);
        if (t && t.length > 20) return t;
        sib = sib.nextElementSibling;
      }
    }
    const direct = firstMatch([
      "[data-testid='condition-report']",
      ".condition-report",
      "#condition-report",
    ]);
    return direct ? text(direct) : "";
  };

  const scrapeAll = () => ({
    lotNumber: scrapeLotNumber(),
    auctionHouse: scrapeAuctionHouse(),
    auctionDate: scrapeAuctionDate(),
    imageUrl: scrapeImageUrl(),
    conditionReport: scrapeConditionReport(),
    sourceUrl: location.href,
  });

  return { scrapeAll };
})();
