import { createLotPage } from "./notion.js";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "ASH_CREATE_LOT") return;

  (async () => {
    try {
      const { notionToken, notionDbId } = await chrome.storage.local.get([
        "notionToken",
        "notionDbId",
      ]);
      if (!notionToken || !notionDbId) {
        throw new Error("Set your Notion token and database ID in the extension popup first.");
      }
      const page = await createLotPage({
        token: notionToken,
        databaseId: notionDbId,
        data: msg.payload,
      });
      sendResponse({ ok: true, pageId: page.id, url: page.url });
    } catch (err) {
      sendResponse({ ok: false, error: err.message });
    }
  })();

  return true; // keep the channel open for async sendResponse
});
