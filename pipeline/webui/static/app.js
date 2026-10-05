let reworkTargetKey = null;
let allTickets = [];

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function toast(msg, isErr) {
  const el = document.createElement("div");
  el.className = "toast-item" + (isErr ? " err" : "");
  el.textContent = msg;
  document.getElementById("toast").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

async function callAction(action, key, note) {
  const res = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, key, note: note || "" }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    toast(`${key}: ${data.error || "action failed"}`, true);
    throw new Error(data.error || "action failed");
  }
  toast(data.message);
  await load(true);
}

function confirmDanger(msg) {
  return window.confirm(msg);
}

function openReworkDialog(key) {
  reworkTargetKey = key;
  document.getElementById("reworkTitle").textContent = `Send ${key} to rework`;
  document.getElementById("reworkNote").value = "";
  document.getElementById("reworkDialog").showModal();
}

document.getElementById("reworkCancel").onclick = () => document.getElementById("reworkDialog").close();
document.getElementById("reworkConfirm").onclick = async () => {
  const note = document.getElementById("reworkNote").value.trim();
  document.getElementById("reworkDialog").close();
  if (reworkTargetKey) await callAction("send_to_rework", reworkTargetKey, note);
};

function labelBadgeClass(label) {
  if (label === "aidev-done") return "green";
  if (label === "aidev-stuck") return "red";
  if (label === "aidev-auto-merge") return "purple";
  if (label === "aidev-picked") return "amber";
  if (label === "aidev-self-review" || label === "aidev-codex-review") return "amber";
  return "";
}

function prNumber(prUrl) {
  if (!prUrl) return null;
  const m = prUrl.match(/\/pull\/(\d+)/);
  return m ? m[1] : null;
}

function repoName(repoPath) {
  if (!repoPath) return "";
  return repoPath.split("/").pop();
}

function renderBlockers(blockers) {
  if (!blockers || !blockers.length) return "";
  const chips = blockers.map(b =>
    `<span class="blocker-chip ${b.hard ? "hard" : "soft"}" title="${b.hard ? "hard blocker — must wait" : "soft blocker — stacking may apply"}">
      ${b.hard ? "⛔" : "⏳"} ${esc(b.key)} (${esc(b.status)})
    </span>`
  ).join("");
  return `<div class="blockers">Blocked by: ${chips}</div>`;
}

function classifyColumn(t) {
  const jiraStatus = (t.jira && t.jira.status || "").toLowerCase();
  if (jiraStatus === "in review" || t.dbState === "DONE") return "review";
  if (t.dbState === "RUNNING" || t.dbState === "POSTPROCESS" || t.dbState === "FAILED" || t.dbState === "STUCK") return "doing";
  return "todo";
}

function renderStatusStrip(t) {
  if (t.needsYou) {
    return `<div class="status-strip needs-you"><span class="dot needs-you"></span>NEEDS YOU</div>`;
  }
  if (t.dbState === "RUNNING" || t.dbState === "POSTPROCESS") {
    return `<div class="status-strip running"><span class="dot running pulse"></span>RUNNING</div>`;
  }
  const jiraStatus = (t.jira && t.jira.status || "").toLowerCase();
  if (jiraStatus === "in review" || t.dbState === "DONE") {
    return `<div class="status-strip needs-you"><span class="dot needs-you"></span>WAITING ON YOU</div>`;
  }
  return "";
}

function renderCard(t) {
  const jira = t.jira || {};
  const pr = t.pr || {};
  const labels = jira.labels || [];
  const isDone = t.dbState === "DONE" || (jira.status || "").toLowerCase() === "in review";
  const isFailed = t.dbState === "FAILED" || t.dbState === "STUCK";
  const hasAutoMerge = labels.includes("aidev-auto-merge");
  const hasReview = labels.includes("aidev-self-review");
  const hasCodex = labels.includes("aidev-codex-review");
  const prRed = (pr.redChecks || []).length > 0;
  const mergeApproved = pr.mergeStateStatus === "CLEAN" || pr.approved === true;

  let cardClass = "card";
  if (t.needsYou) cardClass += " needs-you";
  else if (t.dbState === "RUNNING" || t.dbState === "POSTPROCESS") cardClass += " running";

  let badges = "";
  if (jira.status) badges += `<span class="badge status">${esc(jira.status)}</span>`;
  if (t.stage) badges += `<span class="badge">${esc(t.stage)}</span>`;
  for (const l of labels) if (l !== "aidev") badges += `<span class="badge ${labelBadgeClass(l)}">${esc(l)}</span>`;
  if (prRed) badges += `<span class="badge red">CI red: ${esc(pr.redChecks.join(", "))}</span>`;
  if (t.crashRetryCount) badges += `<span class="badge amber">crash x${t.crashRetryCount}</span>`;

  const pnum = prNumber(t.prUrl);
  const epicLink = jira.epicKey ? `<a href="${esc(jira.epicLink)}" target="_blank">Epic ${esc(jira.epicKey)}</a>` : "";
  let links = "";
  if (jira.link) links += `<a href="${esc(jira.link)}" target="_blank">Jira ↗</a>`;
  if (t.prUrl) links += `<a href="${esc(t.prUrl)}" target="_blank">PR ↗</a>`;
  if (epicLink) links += epicLink;

  let actions = "";
  if (!hasReview) actions += `<button onclick="callAction('tag_review','${t.key}')">Tag review</button>`;
  if (!hasCodex) actions += `<button onclick="callAction('tag_codex_review','${t.key}')">Tag codex review</button>`;
  actions += `<button class="warn" onclick="openReworkDialog('${t.key}')">Rework</button>`;
  if (!hasAutoMerge) actions += `<button onclick="callAction('tag_auto_merge','${t.key}')">Tag auto-merge</button>`;
  else actions += `<button onclick="callAction('untag_auto_merge','${t.key}')">Untag auto-merge</button>`;
  if (t.prUrl && isDone) {
    if (mergeApproved) {
      actions += `<button class="primary" onclick="if(confirmDanger('Merge ${t.key}\\'s PR now?')) callAction('merge_pr','${t.key}')">Merge now</button>`;
    } else {
      const why = prRed ? "CI red" : "not approved yet";
      actions += `<button disabled title="Not mergeable: ${esc(why)}">Merge now</button>`;
    }
  }
  if (isFailed) actions += `<button class="primary" onclick="callAction('recover_crash','${t.key}')">Recover</button>`;
  actions += `<button class="danger" onclick="if(confirmDanger('Archive ${t.key}? This stops tracking it.')) callAction('archive','${t.key}')">Archive</button>`;

  const stuckNote = t.stuckQuestion ? `<div class="stuck-note">${esc(t.stuckQuestion)}</div>` : "";

  return `
    <div class="${cardClass}">
      ${renderStatusStrip(t)}
      <div class="key-row">
        <span class="key">${esc(t.key)}</span>
        ${pnum ? `<span class="pr-num">// PR-${esc(pnum)}</span>` : ""}
      </div>
      <div class="repo">repo: ${esc(repoName(t.repoPath))}</div>
      <div class="summary">${esc(jira.summary || jira.error || "")}</div>
      <div class="badges">${badges}</div>
      ${renderBlockers(t.blockers)}
      <div class="links">${links}</div>
      ${stuckNote}
      <div class="actions">${actions}</div>
    </div>
  `;
}

function render(tickets) {
  const cols = { todo: [], doing: [], review: [] };
  for (const t of tickets) cols[classifyColumn(t)].push(t);

  const colDefs = [
    { key: "todo", label: "To Do" },
    { key: "doing", label: "Doing" },
    { key: "review", label: "Review" },
  ];

  const html = `<div class="board">` + colDefs.map(({key, label}) => {
    const list = cols[key];
    const body = list.length ? list.map(renderCard).join("") : `<div class="empty">Nothing here.</div>`;
    return `<div class="col">
      <h2>${label} <span class="col-count">${list.length}</span></h2>
      ${body}
    </div>`;
  }).join("") + `</div>`;

  document.getElementById("main").innerHTML = html;
}

function applyFilter(tickets, q) {
  if (!q) return tickets;
  q = q.toLowerCase();
  return tickets.filter(t => {
    const jira = t.jira || {};
    const hay = [t.key, jira.summary, (jira.labels || []).join(" ")].join(" ").toLowerCase();
    return hay.includes(q);
  });
}

async function load(force) {
  try {
    const res = await fetch(`/api/tickets${force ? "?force=1" : ""}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    allTickets = data.tickets || [];
    const runningCount = allTickets.filter(t => t.dbState === "RUNNING" || t.dbState === "POSTPROCESS").length;
    const needsYouCount = allTickets.filter(t => t.needsYou || (t.jira && (t.jira.status||"").toLowerCase()==="in review") || t.dbState === "DONE").length;
    document.getElementById("meta").textContent =
      `${allTickets.length} tracked · 🏃 ${runningCount} running · 🔴 ${needsYouCount} need you · ${new Date().toLocaleTimeString()}`;
    const q = document.getElementById("searchBox").value.trim();
    render(applyFilter(allTickets, q));
  } catch (e) {
    document.getElementById("meta").textContent = "error: " + e.message;
  }
}

document.getElementById("searchBox").addEventListener("input", (e) => {
  render(applyFilter(allTickets, e.target.value.trim()));
});

load(true);
setInterval(() => load(false), 6000);
