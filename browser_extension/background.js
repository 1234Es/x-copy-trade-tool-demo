/**
 * The only place a network request happens. Runs as the extension itself
 * (not in the page's context), so it is not subject to x.com's page CSP,
 * and it only ever fires in direct response to a message from content.js
 * -- there is no polling, no alarm, no background timer here.
 */

const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

async function getConfig() {
  const stored = await chrome.storage.local.get(["baseUrl", "webhookSecret"]);
  return {
    baseUrl: stored.baseUrl || DEFAULT_BASE_URL,
    webhookSecret: stored.webhookSecret || "",
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "SEND_POST") {
    (async () => {
      const config = await getConfig();
      if (!config.webhookSecret) {
        sendResponse({ ok: false, error: "Set the webhook secret in the extension's Options page first." });
        return;
      }
      if (!message.payload.author || !message.payload.text) {
        sendResponse({ ok: false, error: "Author and text are required -- extraction may have failed; fill them in manually." });
        return;
      }
      try {
        const response = await fetch(`${config.baseUrl}/api/posts/webhook`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Webhook-Secret": config.webhookSecret,
          },
          body: JSON.stringify(message.payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          sendResponse({ ok: false, error: `HTTP ${response.status}: ${data.detail || response.statusText}` });
          return;
        }
        sendResponse({ ok: true, status: data.status || "submitted" });
      } catch (err) {
        sendResponse({ ok: false, error: `Could not reach local app at ${config.baseUrl} -- is it running? (${err})` });
      }
    })();
    return true; // keep the message channel open for the async sendResponse
  }
  if (message.type === "PING") {
    (async () => {
      const config = await getConfig();
      try {
        const response = await fetch(`${config.baseUrl}/api/status`);
        const data = await response.json();
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: String(err) });
      }
    })();
    return true;
  }
});
