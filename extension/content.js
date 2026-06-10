(function init() {
  if (document.getElementById("ash-send-btn")) return;

  const btn = document.createElement("button");
  btn.id = "ash-send-btn";
  btn.type = "button";
  btn.textContent = "Send to Notion";
  document.body.appendChild(btn);

  const toast = (msg, kind = "info") => {
    const t = document.createElement("div");
    t.className = `ash-toast ash-toast--${kind}`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  };

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Scraping…";

    const payload = window.ASH_Scrapers.scrapeAll();

    if (!payload.lotNumber) {
      toast("Couldn't find lot number — check selectors in scrapers.js.", "error");
      btn.disabled = false;
      btn.textContent = "Send to Notion";
      return;
    }

    btn.textContent = "Sending…";
    chrome.runtime.sendMessage({ type: "ASH_CREATE_LOT", payload }, (res) => {
      btn.disabled = false;
      btn.textContent = "Send to Notion";
      if (chrome.runtime.lastError) {
        toast("Extension error: " + chrome.runtime.lastError.message, "error");
        return;
      }
      if (res?.ok) {
        toast("Sent to Notion ✓", "success");
      } else {
        toast("Notion error: " + (res?.error || "unknown"), "error");
        console.error("[ASH] Notion error", res);
      }
    });
  });

  // Debug helper — open devtools console on the lot page and run: __ashDebug()
  window.__ashDebug = () => {
    const data = window.ASH_Scrapers.scrapeAll();
    console.table(data);
    return data;
  };
})();
