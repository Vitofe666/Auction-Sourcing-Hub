// "Build Notion Payloads" Code node (n8n, Run Once for All Items).
// Input: the Scrape Lot response. Output: one item PER BLOCK BATCH (Notion
// caps block appends at 100/request), each carrying the same pageId and
// propertiesPayload. The "Update Properties" node must have Settings ->
// "Execute Once" = ON; "Write Page Body" runs once per item (per batch).

const scraped = $input.first().json;
const pageId = $('Notion Trigger').first().json.id;

// Notion caps each rich_text item at 2000 chars — chunk long text.
const chunk = (s, size = 1900) => {
  const out = [];
  for (let i = 0; i < s.length; i += size) out.push(s.slice(i, i + size));
  return out;
};
const rt = (s) => ({ type: 'text', text: { content: s } });

// Inline markdown: supports **bold** only (enough for the report format).
const rich = (s) => {
  const parts = [];
  s.split('**').forEach((seg, i) => {
    if (!seg) return;
    for (const piece of chunk(seg)) {
      const item = { type: 'text', text: { content: piece } };
      if (i % 2 === 1) item.annotations = { bold: true };
      parts.push(item);
    }
  });
  return parts.length ? parts : [rt(' ')];
};

const paragraphs = (s) => chunk(s).map((c) => ({
  object: 'block', type: 'paragraph', paragraph: { rich_text: [rt(c)] },
}));
const divider = () => ({ object: 'block', type: 'divider', divider: {} });
const heading = (level, s) => ({
  object: 'block', type: `heading_${level}`, [`heading_${level}`]: { rich_text: rich(s) },
});

// Line-based markdown -> Notion blocks for the AI report.
const mdToBlocks = (md) => {
  const blocks = [];
  for (const raw of md.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (/^-{3,}$/.test(line)) blocks.push(divider());
    else if (line.startsWith('### ')) blocks.push(heading(3, line.slice(4)));
    else if (line.startsWith('## ')) blocks.push(heading(2, line.slice(3)));
    else if (line.startsWith('# ')) blocks.push(heading(1, line.slice(2)));
    else if (/^[-•*] /.test(line)) blocks.push({
      object: 'block', type: 'bulleted_list_item',
      bulleted_list_item: { rich_text: rich(line.slice(2)) },
    });
    else if (line.startsWith('> ')) blocks.push({
      object: 'block', type: 'quote', quote: { rich_text: rich(line.slice(2)) },
    });
    else blocks.push({
      object: 'block', type: 'paragraph', paragraph: { rich_text: rich(line) },
    });
  }
  return blocks;
};

// ---------- Database properties ----------
const properties = {
  Name: { title: [rt(scraped.title || `Lot ${scraped.lotNumber || '?'} — ${scraped.auctionHouse || 'Unknown'}`)] },
  Status: { select: { name: 'Done' } },
};
if (scraped.lotNumber) {
  properties['Lot Number'] = { rich_text: [rt(String(scraped.lotNumber))] };
}
if (scraped.auctionHouse) {
  // Select option names cannot contain commas
  properties['Auction House'] = { select: { name: scraped.auctionHouse.replace(/,/g, ' ').slice(0, 100) } };
}
if (/^\d{4}-\d{2}-\d{2}$/.test(scraped.auctionDate || '')) {
  properties['Auction Date'] = { date: { start: scraped.auctionDate } };
}
if (scraped.imageUrl) {
  properties['Photo'] = { files: [{ type: 'external', name: 'Lot photo', external: { url: scraped.imageUrl } }] };
}

// ---------- Page body blocks ----------
const blocks = [];

blocks.push({
  object: 'block', type: 'callout',
  callout: {
    icon: { type: 'emoji', emoji: '🔨' },
    color: 'gray_background',
    rich_text: [rt([
      `Lot ${scraped.lotNumber || '—'}`,
      scraped.auctionHouse,
      scraped.auctionDate,
    ].filter(Boolean).join('   •   '))],
  },
});
blocks.push(divider());

// --- AI buy-analysis report (the main dossier) ---
if (scraped.aiReport) {
  blocks.push(heading(1, '🤖 AI Buy Analysis'));
  blocks.push(...mdToBlocks(scraped.aiReport));
  blocks.push(divider());
} else if (scraped.aiReportError) {
  blocks.push({
    object: 'block', type: 'callout',
    callout: { icon: { type: 'emoji', emoji: '⚠️' }, color: 'red_background', rich_text: [rt(scraped.aiReportError)] },
  });
  blocks.push(divider());
}

// --- Photos ---
const gallery = (scraped.imageUrls && scraped.imageUrls.length ? scraped.imageUrls : [scraped.imageUrl])
  .filter(Boolean).slice(0, 4);
if (gallery.length) {
  blocks.push(heading(2, '🖼️ Lot Photos'));
  for (const url of gallery) {
    blocks.push({ object: 'block', type: 'image', image: { type: 'external', external: { url } } });
  }
  blocks.push(divider());
}

// --- Original catalogue text ---
if (scraped.description) {
  blocks.push(heading(2, '📝 Catalogue Description'));
  blocks.push(...paragraphs(scraped.description));
}
blocks.push(heading(2, '🔍 Published Condition Text'));
if (scraped.condition) {
  const parts = chunk(scraped.condition);
  blocks.push({
    object: 'block', type: 'callout',
    callout: { icon: { type: 'emoji', emoji: '⚠️' }, color: 'yellow_background', rich_text: [rt(parts[0])] },
  });
  for (const extra of parts.slice(1)) {
    blocks.push({ object: 'block', type: 'paragraph', paragraph: { rich_text: [rt(extra)] } });
  }
} else {
  blocks.push({
    object: 'block', type: 'callout',
    callout: { icon: { type: 'emoji', emoji: 'ℹ️' }, color: 'gray_background', rich_text: [rt('No condition report published for this lot.')] },
  });
}
blocks.push(divider());
blocks.push({ object: 'block', type: 'bookmark', bookmark: { url: scraped.sourceUrl } });

// ---------- Batch (Notion: max 100 blocks per append request) ----------
const BATCH = 90;
const items = [];
for (let i = 0; i < blocks.length; i += BATCH) {
  items.push({ json: {
    pageId,
    propertiesPayload: { properties },
    blocksPayload: { children: blocks.slice(i, i + BATCH) },
  } });
}
return items;
