const tokenEl = document.getElementById("token");
const dbEl = document.getElementById("dbid");
const statusEl = document.getElementById("status");

const extractDbId = (input) => {
  const cleaned = (input || "").trim().replace(/-/g, "");
  const m = cleaned.match(/[0-9a-f]{32}/i);
  return m ? m[0] : "";
};

chrome.storage.local.get(["notionToken", "notionDbId"], (cfg) => {
  if (cfg.notionToken) tokenEl.value = cfg.notionToken;
  if (cfg.notionDbId) dbEl.value = cfg.notionDbId;
});

document.getElementById("save").addEventListener("click", () => {
  const notionToken = tokenEl.value.trim();
  const notionDbId = extractDbId(dbEl.value);
  if (!notionToken || !notionDbId) {
    statusEl.textContent = "Both fields required (database ID must be 32 hex chars).";
    statusEl.className = "status err";
    return;
  }
  chrome.storage.local.set({ notionToken, notionDbId }, () => {
    statusEl.textContent = "Saved ✓";
    statusEl.className = "status ok";
  });
});
