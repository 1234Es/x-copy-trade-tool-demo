const resultEl = document.getElementById("result");

chrome.runtime.sendMessage({ type: "PING" }, (response) => {
  if (!response || !response.ok) {
    resultEl.innerHTML = `<div class="row"><span>local app</span><span class="bad">unreachable</span></div>`;
    return;
  }
  const data = response.data;
  let html = `<div class="row"><span>mode</span><span>${data.app_mode}</span></div>`;
  for (const c of data.connections) {
    html += `<div class="row"><span>${c.name}</span><span class="${c.connected ? "ok" : "bad"}">${c.connected ? "ok" : "down"}</span></div>`;
  }
  html += `<div class="row"><span>circuit breaker</span><span class="${data.circuit_breaker.tripped ? "bad" : "ok"}">${data.circuit_breaker.tripped ? "tripped" : "clear"}</span></div>`;
  resultEl.innerHTML = html;
});

document.getElementById("open-options").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});
