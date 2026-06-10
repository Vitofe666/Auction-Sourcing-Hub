JavaScript
document.addEventListener('DOMContentLoaded', () => {
    const tokenInput = document.getElementById('notionToken');
    const dbInput = document.getElementById('dbId');
    const saveBtn = document.getElementById('saveBtn');
    const scrapeBtn = document.getElementById('scrapeBtn');
    const statusMsg = document.getElementById('statusMessage');
    const actionMsg = document.getElementById('actionMessage');

    // 1. Load your saved settings as soon as you click the extension icon
    chrome.storage.local.get(['notionToken', 'notionDbId'], (result) => {
        if (result.notionToken) tokenInput.value = result.notionToken;
        if (result.notionDbId) dbInput.value = result.notionDbId;
    });

    // 2. Save the settings when you click "Save Settings"
    saveBtn.addEventListener('click', () => {
        chrome.storage.local.set({
            notionToken: tokenInput.value.trim(),
            notionDbId: dbInput.value.trim()
        }, () => {
            statusMsg.style.display = 'block';
            setTimeout(() => statusMsg.style.display = 'none', 2000); // Hide after 2 seconds
        });
    });

    // 3. The "Send to Notion" action cascade
    scrapeBtn.addEventListener('click', async () => {
        const token = tokenInput.value.trim();
        const dbId = dbInput.value.trim();

        if (!token || !dbId) {
            showMessage(actionMsg, 'Please save your Token and DB ID first.', 'red');
            return;
        }

        showMessage(actionMsg, 'Scraping page...', '#333');

        // Find the active browser tab
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

        // Tell content.js to extract the data
        chrome.tabs.sendMessage(tab.id, { action: "scrape_page" }, (response) => {
            if (chrome.runtime.lastError || !response || !response.data) {
                showMessage(actionMsg, 'Error scraping. Are you on a lot page?', 'red');
                return;
            }

            showMessage(actionMsg, 'Sending to Notion...', '#333');

            // Pass the scraped data + your saved Notion credentials to background.js
            chrome.runtime.sendMessage({
                action: "send_to_notion",
                data: response.data,
                token: token,
                databaseId: dbId
            }, (apiResponse) => {
                if (apiResponse && apiResponse.success) {
                    showMessage(actionMsg, 'Success! Item added to Notion.', 'green');
                } else {
                    showMessage(actionMsg, 'API Error: ' + (apiResponse ? apiResponse.error : 'Unknown error'), 'red');
                }
            });
        });
    });

    // Helper to show messages
    function showMessage(element, text, color) {
        element.style.color = color;
        element.innerText = text;
        element.style.display = 'block';
    }
});
