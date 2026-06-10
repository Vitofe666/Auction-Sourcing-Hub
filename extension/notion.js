// ESM module — imported by background.js.

const NOTION_VERSION = "2022-06-28";

export async function createLotPage({ token, databaseId, data }) {
  const body = buildCreatePagePayload(databaseId, data);

  const res = await fetch("https://api.notion.com/v1/pages", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Notion-Version": NOTION_VERSION,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  const json = await res.json();
  if (!res.ok) {
    const msg = json?.message || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return json;
}

function buildCreatePagePayload(databaseId, d) {
  const properties = {
    // Title = Lot Number (page name in Notion)
    "Lot Number": {
      title: [{ type: "text", text: { content: d.lotNumber || "Untitled lot" } }],
    },
  };

  if (d.auctionHouse) {
    properties["Auction House"] = { select: { name: d.auctionHouse } };
  }
  if (d.auctionDate) {
    properties["Date"] = { date: { start: d.auctionDate } };
  }
  if (d.imageUrl) {
    properties["Lot Photo"] = {
      files: [
        {
          type: "external",
          name: `lot-${d.lotNumber || "image"}.jpg`,
          external: { url: d.imageUrl },
        },
      ],
    };
  }

  return {
    parent: { database_id: databaseId },
    properties,
    children: buildConditionReportBlocks(d),
  };
}

// PDF-style structured body
function buildConditionReportBlocks(d) {
  const header = `Lot ${d.lotNumber || "—"} · ${d.auctionHouse || "Unknown house"}`;
  const subtitle = d.auctionDate ? `Auction date: ${d.auctionDate}` : "";

  const blocks = [
    h1(header),
    subtitle ? paragraph(subtitle, { italic: true, color: "gray" }) : null,
    divider(),
  ];

  if (d.imageUrl) {
    blocks.push(image(d.imageUrl, `Lot ${d.lotNumber || ""}`.trim()));
    blocks.push(divider());
  }

  blocks.push(h2("Condition Report"));

  if (d.conditionReport && d.conditionReport.trim()) {
    // Chunk into ~1800-char blocks (Notion's rich_text limit is 2000).
    for (const chunk of chunkText(d.conditionReport.trim(), 1800)) {
      blocks.push(callout(chunk, "📝"));
    }
  } else {
    blocks.push(callout("No condition report was found on the source page.", "⚠️"));
  }

  blocks.push(divider());
  blocks.push(paragraph("Source", { bold: true }));
  blocks.push(bookmark(d.sourceUrl));

  return blocks.filter(Boolean);
}

/* ---------- block helpers ---------- */
const rt = (content, ann = {}) => ({
  type: "text",
  text: { content },
  annotations: { bold: false, italic: false, color: "default", ...ann },
});

const h1 = (txt) => ({ object: "block", type: "heading_1", heading_1: { rich_text: [rt(txt, { bold: true })] } });
const h2 = (txt) => ({ object: "block", type: "heading_2", heading_2: { rich_text: [rt(txt, { bold: true })] } });
const paragraph = (txt, ann = {}) => ({
  object: "block", type: "paragraph",
  paragraph: { rich_text: [rt(txt, ann)] },
});
const divider = () => ({ object: "block", type: "divider", divider: {} });
const callout = (txt, emoji = "💬") => ({
  object: "block", type: "callout",
  callout: {
    rich_text: [rt(txt)],
    icon: { type: "emoji", emoji },
    color: "gray_background",
  },
});
const image = (url, caption = "") => ({
  object: "block", type: "image",
  image: { type: "external", external: { url }, caption: caption ? [rt(caption)] : [] },
});
const bookmark = (url) => ({ object: "block", type: "bookmark", bookmark: { url } });

function chunkText(s, n) {
  const out = [];
  for (let i = 0; i < s.length; i += n) out.push(s.slice(i, i + n));
  return out;
}
