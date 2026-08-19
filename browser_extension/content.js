/**
 * Runs only on x.com/twitter.com pages you already have open in your own,
 * already-logged-in browser session. It does not log in, does not run on
 * a timer, and does not navigate anywhere on its own -- it only reads the
 * DOM of posts already rendered on your screen, and only acts when you
 * click the button it adds. That is the deliberate line between this and
 * an automated scraper: nothing here happens without you looking at a
 * specific post and clicking "Send" on it.
 *
 * DOM selectors here (data-testid attributes) are best-effort against X's
 * current markup and WILL break if X changes their page structure --
 * that's a disclosed, expected limitation (see README), not a hidden bug.
 * When extraction is uncertain, fields are left blank for you to fill in
 * rather than guessed at.
 */

const PROCESSED_ATTR = "data-xcopytrade-processed";

function extractPostData(article) {
  const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
  const text = tweetTextEl ? tweetTextEl.innerText : "";

  let author = null;
  let postId = null;
  let postedAt = null;

  const timeEl = article.querySelector("time");
  if (timeEl) {
    postedAt = timeEl.getAttribute("datetime");
    const link = timeEl.closest("a");
    if (link) {
      const href = link.getAttribute("href") || "";
      const match = href.match(/^\/([^/]+)\/status\/(\d+)/);
      if (match) {
        author = match[1];
        postId = match[2];
      }
    }
  }

  let isRepost = false;
  const socialContext = article.querySelector('[data-testid="socialContext"]');
  if (socialContext && /repost|retweet/i.test(socialContext.innerText || "")) {
    isRepost = true;
  }

  return {
    author: author || "",
    post_id: postId || "",
    text: text || "",
    posted_at: postedAt || "",
    is_repost: isRepost,
  };
}

function buildModal(draft, onSend) {
  const overlay = document.createElement("div");
  overlay.className = "xcopytrade-overlay";

  const box = document.createElement("div");
  box.className = "xcopytrade-modal";
  box.innerHTML = `
    <h3>Send to Copy-Trade Tool</h3>
    <label>Author (handle, no @)</label>
    <input type="text" class="xcopytrade-author" value="${escapeHtml(draft.author)}">
    <label>Post text</label>
    <textarea class="xcopytrade-text" rows="4">${escapeHtml(draft.text)}</textarea>
    <label>Post ID (leave blank to auto-generate)</label>
    <input type="text" class="xcopytrade-postid" value="${escapeHtml(draft.post_id)}">
    <label>Posted at (ISO-8601, leave blank for "now")</label>
    <input type="text" class="xcopytrade-postedat" value="${escapeHtml(draft.posted_at)}">
    <div class="xcopytrade-actions">
      <button class="xcopytrade-cancel">Cancel</button>
      <button class="xcopytrade-send">Send</button>
    </div>
    <div class="xcopytrade-status"></div>
  `;
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  box.querySelector(".xcopytrade-cancel").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  box.querySelector(".xcopytrade-send").addEventListener("click", () => {
    const payload = {
      author: box.querySelector(".xcopytrade-author").value.trim(),
      text: box.querySelector(".xcopytrade-text").value.trim(),
      post_id: box.querySelector(".xcopytrade-postid").value.trim() || undefined,
      posted_at: box.querySelector(".xcopytrade-postedat").value.trim() || undefined,
      is_repost: draft.is_repost,
    };
    const statusEl = box.querySelector(".xcopytrade-status");
    statusEl.textContent = "Sending...";
    onSend(payload, (result) => {
      if (result.ok) {
        statusEl.textContent = `Sent -- status: ${result.status}`;
        statusEl.className = "xcopytrade-status xcopytrade-ok";
        setTimeout(() => overlay.remove(), 1500);
      } else {
        statusEl.textContent = `Failed: ${result.error}`;
        statusEl.className = "xcopytrade-status xcopytrade-error";
      }
    });
  });
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value || "";
  return div.innerHTML;
}

function injectButton(article) {
  if (article.getAttribute(PROCESSED_ATTR)) return;
  article.setAttribute(PROCESSED_ATTR, "1");

  const button = document.createElement("button");
  button.className = "xcopytrade-send-button";
  button.type = "button";
  button.textContent = "→ Copy-Trade";
  button.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const draft = extractPostData(article);
    buildModal(draft, (payload, callback) => {
      chrome.runtime.sendMessage({ type: "SEND_POST", payload }, (response) => {
        callback(response || { ok: false, error: "no response from extension background" });
      });
    });
  });

  article.style.position = article.style.position || "relative";
  article.appendChild(button);
}

function scanForTweets() {
  document.querySelectorAll('article[data-testid="tweet"]').forEach(injectButton);
}

const observer = new MutationObserver(() => scanForTweets());
observer.observe(document.body, { childList: true, subtree: true });
scanForTweets();
