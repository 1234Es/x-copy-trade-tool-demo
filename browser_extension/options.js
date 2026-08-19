const baseUrlInput = document.getElementById("baseUrl");
const webhookSecretInput = document.getElementById("webhookSecret");
const statusEl = document.getElementById("status");

chrome.storage.local.get(["baseUrl", "webhookSecret"], (stored) => {
  baseUrlInput.value = stored.baseUrl || "http://127.0.0.1:8000";
  webhookSecretInput.value = stored.webhookSecret || "";
});

document.getElementById("save").addEventListener("click", () => {
  chrome.storage.local.set(
    { baseUrl: baseUrlInput.value.trim(), webhookSecret: webhookSecretInput.value.trim() },
    () => {
      statusEl.textContent = "Saved.";
      setTimeout(() => (statusEl.textContent = ""), 2000);
    }
  );
});
