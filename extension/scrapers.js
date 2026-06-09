// Attached to window so content.js can use it (no ES modules in classic content scripts).
window.ASH_Scrapers = (() => {
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

  const firstMatch = (selectors) => {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  };

  // Find a value next to a label like "Lot:" or "Auction:" — useful when DOM has no stable class names.
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

  const scrapeLotNumber = () => {
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
    // Fallback: "Lot 132" anywhere in an h1/h2
    for (const h of document.querySelectorAll("h1, h2")) {
      const m = (h.textContent || "").match(/Lot\s+(\d+)/i);
      if (m) return m[1];
    }
    return findByLabel(/^\s*Lot\b/i).match(/\d+/)?.[0] || "";
  };

  const scrapeAuctionHouse = () => {
    const el = firstMatch([
      "[data-testid='auction-house']",
      ".auction-house-name",
      "a[href*='/auction-catalogues/']",
      "nav.breadcrumb a[href*='/auction-catalogues/']",
    ]);
    if (el) return text(el);
    return findByLabel(/Auction(eer)?(\s*House)?/i);
  };

  const scrapeAuctionDate = () => {
    // Prefer <time datetime="..."> when present — gives ISO directly.
    const timeEl = document.querySelector("time[datetime]");
    if (timeEl) {
      const iso = timeEl.getAttribute("datetime");
      if (iso) return iso.slice(0, 10);
    }
    const candidate =
      firstMatch([
        "[data-testid='auction-date']",
        ".auction-date",
        ".sale-date",
      ]) || null;
    const raw = candidate ? text(candidate) : findByLabel(/^\s*(Sale|Auction)\s*Date/i);
    if (!raw) return "";
    const d = new Date(raw);
    return isNaN(d) ? raw : d.toISOString().slice(0, 10);
  };

  const scrapeImageUrl = () => {
    // Prefer og:image — most reliable, absolute URL, picks the hero image.
    const og = document.querySelector("meta[property='og:image']");
    if (og?.content) return og.content;

    const img = firstMatch([
      ".lot-image img",
      ".main-image img",
      "[data-testid='lot-image'] img",
      "img[src*='/lot-images/']",
    ]);
    if (img) return img.currentSrc || img.src;
    return "";
  };

  const scrapeConditionReport = () => {
    // Common patterns: a labelled section or a tab panel.
    const headerCandidates = Array.from(
      document.querySelectorAll("h2, h3, h4, [role='tab'], button, summary")
    ).filter((el) => /condition\s*report/i.test(el.textContent || ""));

    for (const h of headerCandidates) {
      // Sibling block after the header
      let sib = h.nextElementSibling;
      while (sib) {
        const t = text(sib);
        if (t && t.length > 20) return t;
        sib = sib.nextElementSibling;
      }
      // Or a parent container holding both header and body
      const parent = h.closest("section, article, div");
      if (parent) {
        const clone = parent.cloneNode(true);
        clone.querySelectorAll("h1,h2,h3,h4,button,[role='tab']").forEach((n) => n.remove());
        const t = text(clone);
        if (t && t.length > 20) return t;
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
