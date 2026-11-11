/* Faculty review console. No framework, no build: opens from disk or any static host. */
(() => {
  const DATA = window.CONSOLE_DATA;
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const LOG_KEY = "acadza-review-log-v1";
  const OVR_KEY = "acadza-review-overrides-v1";

  // ---------------- storage (wrapped: storage can be unavailable)
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* in-memory only */ } },
  };
  let log = store.get(LOG_KEY, []);
  let overrides = store.get(OVR_KEY, {}); // {caseId: {edit, replies:{rid:text}}}

  if (!DATA) {
    $("#case").innerHTML = `<div class="empty"><h2>No results loaded</h2><p>Run <kbd>python run.py</kbd> to generate <code>web/data.js</code>, then reload this page.</p></div>`;
    return;
  }

  // ---------------- model
  const ROUTE_GROUPS = [
    { route: "faculty_review", title: "Needs your decision", hint: "The AI won't change these on its own." },
    { route: "auto_apply", title: "Fixed automatically", hint: "Already live for students. Spot-check and undo if wrong." },
    { route: "no_change", title: "No change needed", hint: "Student mistaken, app or display issue." },
  ];
  const cases = DATA.cases.map((c) => ({ ...c, id: c.questionId, route: c.primary.route }));
  const ordered = ROUTE_GROUPS.flatMap((g) => cases.filter((c) => c.route === g.route).sort((a, b) => b.urgency - a.urgency));
  let current = ordered[0]?.id;
  let editing = false;

  const decisionOf = (id) => [...log].reverse().find((e) => e.caseId === id && ["approved", "rejected", "edited"].includes(e.action));
  const editOf = (c) => overrides[c.id]?.edit ?? c.primary.edit ?? c.primary.suggestion ?? null;

  const ACTIONS = {
    faculty_review: { ok: "Approve fix", no: "Reject", okDone: "Approved", noDone: "Rejected" },
    auto_apply: { ok: "Keep fix", no: "Undo fix", okDone: "Kept", noDone: "Undone" },
    no_change: { ok: "Confirm no change", no: "Reopen as a defect", okDone: "Confirmed", noDone: "Reopened" },
  };

  const SEV = (c) => (c.urgency >= 90 ? "sev-hi" : c.urgency >= 30 ? "sev-mid" : c.urgency > 0 ? "sev-lo" : "ok");

  // ---------------- helpers for the plain-English header
  const optText = (c, L) => (c.options && c.options[L] ? `(${L}) ${c.options[L]}` : `(${L})`);
  const asList = (e) => (!e ? [] : Array.isArray(e) ? e : [e]);
  const answerEdit = (c) => asList(editOf(c)).find((e) => e.field === "answer");
  function parseAns(v) { try { const j = JSON.parse(v); return Array.isArray(j) ? j : [String(j)]; } catch { return [String(v)]; } }

  function title(c) {
    const p = c.primary, ae = answerEdit(c);
    if (p.verdict === "defect" && p.repair === "answer_key" && ae && c.qtype === "scq")
      return `Key says ${optText(c, ae.find)}. The correct answer is ${optText(c, ae.replace)}.`;
    if (p.verdict === "defect" && p.repair === "answer_key" && ae && c.qtype === "integerQuestion")
      return `Key says ${ae.find}. The correct answer is ${ae.replace}.`;
    if (p.repair === "type_mismatch") return "Single-correct question stored as multi-correct, so right answers lose marks.";
    if (p.repair === "answer_key" && ae && c.qtype === "mcq") return "Answer key is stored in a broken format, so correct attempts are marked wrong.";
    if (p.verdict === "no_error" && c.solves) return `The key ${c.answer_display} is right. Both blind solves agree with it.`;
    const first = (p.evidence || "").split(/(?<=[.!?])\s/)[0];
    return first || c.headline;
  }

  // ---------------- diff on rendered HTML: wrap the changed span in <del>/<ins>
  function trimCommon(a, b) {
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    let j = 0;
    while (j < a.length - i && j < b.length - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
    // highlight whole words: back off to the previous/next space so 'Epithellial tisue' isn't cut mid-word
    if (/\S/.test(a[i] || "") && i > 0) { const k = a.lastIndexOf(" ", i - 1); i = k < 0 ? 0 : k + 1; }
    if (j > 0) { const end = a.length - j; const k = a.indexOf(" ", end); j = k < 0 ? 0 : a.length - k; }
    const bj = j;
    // never split inside a tag or entity
    const pre = a.slice(0, i), sufA = a.slice(a.length - j);
    if (/<[^>]*$/.test(pre) || /&[a-z#0-9]*$/i.test(pre) || /^[^<]*>/.test(sufA)) return { pre: "", a, b, suf: "" };
    return { pre, a: a.slice(i, a.length - j), b: b.slice(i, b.length - bj), suf: sufA };
  }
  function markup(html, find, replace) {
    if (!find || html.split(find).length !== 2) return { before: html, after: html };
    const t = trimCommon(find, replace ?? "");
    const delHTML = t.a ? `<del>${t.a}</del>` : "";
    const insHTML = t.b ? `<ins>${t.b}</ins>` : "";
    const before = html.replace(find, t.pre + (t.a ? delHTML : "") + t.suf);
    const after = html.replace(find, t.pre + insHTML + t.suf);
    return { before, after: t.b ? after : html.replace(find, t.pre + t.suf) };
  }

  const snippet = (t) => { t = String(t || "").replace(/\s+/g, " ").replace(/^\(?(NEET|JEE)[^)]*\)\s*/i, ""); return t.length > 58 ? t.slice(0, 56).replace(/\s\S*$/, "") + "…" : t; };
  function queueTitle(c) {
    const ae = answerEdit(c);
    if (c.primary.repair === "answer_key" && ae && c.qtype !== "mcq") return `Answer key wrong, ${ae.find} → ${ae.replace}`;
    return c.headline;
  }

  // ---------------- render: queue
  function renderQueue() {
    const q = $("#queue");
    q.innerHTML = ROUTE_GROUPS.map((g) => {
      const items = ordered.filter((c) => c.route === g.route);
      if (!items.length) return "";
      const open = items.filter((c) => !decisionOf(c.id)).length;
      return `<div class="q-group"><h2><b>${g.title}</b> · ${open} of ${items.length} left</h2><p>${g.hint}</p></div>` +
        items.map((c) => {
          const d = decisionOf(c.id);
          const side = d ? `<span class="q-done ${d.action === "rejected" ? "rejected" : ""}">${d.action === "rejected" ? ACTIONS[c.route].noDone : ACTIONS[c.route].okDone}</span>`
                         : `${c.reports.length} report${c.reports.length > 1 ? "s" : ""}`;
          return `<button class="q-item ${d ? "done" : ""}" data-id="${c.id}" aria-current="${c.id === current}">
            <span class="dot ${SEV(c)}" aria-hidden="true"></span>
            <span><span class="q-title">${esc(queueTitle(c))}</span><span class="q-meta">${esc(snippet(c.stem))}</span></span>
            <span class="q-side">${side}</span></button>`;
        }).join("");
    }).join("");
    q.querySelectorAll(".q-item").forEach((b) => b.addEventListener("click", () => select(b.dataset.id)));
    const cur = q.querySelector('[aria-current="true"]');
    if (cur) cur.scrollIntoView({ block: "nearest" });
  }

  function renderProgress() {
    const done = cases.filter((c) => decisionOf(c.id)).length;
    const need = cases.filter((c) => c.route === "faculty_review" && !decisionOf(c.id)).length;
    $("#progress").innerHTML = `<strong>${done}</strong> of ${cases.length} reviewed · <strong>${need}</strong> waiting for you
      <div class="bar"><span style="width:${(100 * done) / Math.max(cases.length, 1)}%"></span></div>`;
    const m = DATA.meta || {};
    $("#runinfo").textContent = `${cases.reduce((n, c) => n + c.reports.length, 0)} student reports on ${cases.length} questions`;
  }

  // ---------------- render: OMR answer change (the signature element)
  function renderOMR(c) {
    const ae = answerEdit(c);
    const te = asList(editOf(c)).find((e) => e.field === "questionType");
    if (!ae && !te) return "";
    let body = "";
    if (c.qtype === "integerQuestion") {
      body = `<div class="omr-row"><span class="lbl">Answer key</span>
        <div class="bubbles"><span class="omr-val old">${esc(ae.find)}</span><span class="omr-arrow" aria-hidden="true">→</span><span class="omr-val new">${esc(ae.replace)}</span></div></div>`;
    } else {
      const oldL = parseAns(ae ? ae.find : "").flatMap((x) => x.match(/[A-D]/g) || []);
      const newL = parseAns(ae ? ae.replace : "").flatMap((x) => x.match(/[A-D]/g) || []);
      const sameSet = oldL.slice().sort().join() === newL.slice().sort().join();
      const row = (letters, kind) => ["A", "B", "C", "D"].map((L) => {
        const on = letters.includes(L);
        const cls = on ? (kind === "old" ? (sameSet ? "same" : "old") : "new") : "";
        return `<span class="bubble ${cls}" aria-label="${L}${on ? " marked" : ""}">${L}</span>`;
      }).join("");
      const oldRaw = ae ? esc(ae.find) : "";
      body = `<div class="omr-row"><span class="lbl">Stored key${sameSet ? ` <code>${oldRaw}</code>` : ""}</span><div class="bubbles">${row(oldL, "old")}</div></div>
        <span class="omr-arrow" aria-hidden="true">→</span>
        <div class="omr-row"><span class="lbl">After fix${te ? `, as ${te.replace === "scq" ? "single-correct" : te.replace}` : ""}</span><div class="bubbles">${row(newL, "new")}</div></div>`;
      if (sameSet) body += `<p class="omr-cap">Same letters, fixed format. Students who marked ${newL.join(", ")} will now get credit.</p>`;
      else if (c.qtype === "scq" && newL.length) body += `<p class="omr-cap">Students who marked <span class="opt">${esc(optText(c, newL[0]))}</span> were being marked wrong.</p>`;
    }
    if (te && c.qtype !== "integerQuestion") body += `<p class="omr-cap">Question type changes from multi-correct to single-correct; only one option is right.</p>`;
    return `<div class="block"><h3>What changes for students</h3><div class="omr">${body}</div></div>`;
  }

  // ---------------- render: content diff on question papers
  function renderPapers(c) {
    const edits = asList(editOf(c)).filter((e) => e.field === "question" || e.field === "solution");
    if (!edits.length) {
      return `<div class="block"><h3>The question as students see it</h3>
        <div class="papers single"><div class="paper">${c.question_html}</div></div>
        <details class="raw" style="margin-top:10px"><summary>Show stored solution. Key: ${esc(c.answer_display)}</summary>
        <div class="paper solution" style="margin-top:8px">${c.solution_html || "<em>No solution stored</em>"}</div></details></div>`;
    }
    return edits.map((e) => {
      const html = e.field === "question" ? c.question_html : c.solution_html;
      const m = markup(html, e.find, e.replace);
      const where = e.field === "question" ? "question" : "solution";
      return `<div class="block"><h3>What changes in the ${where}</h3><div class="papers">
        <div class="paper ${where}"><span class="tag before">Now</span>${m.before}</div>
        <div class="paper ${where}"><span class="tag after">After fix</span>${m.after}</div></div></div>`;
    }).join("");
  }

  // ---------------- render: evidence
  function verdictMark(v) { return v === true ? `<span class="ic y">✓</span>` : v === false ? `<span class="ic n">✗</span>` : `<span class="ic i">·</span>`; }
  const keyWord = (v) => (v === true ? "Agrees with the key." : v === false ? "Disagrees with the key." : "");
  function solveVal(s) {
    if (!s) return "—";
    const ch = s.choices && s.choices.length ? s.choices.join(", ") : s.choice;
    return [ch && ch !== "null" ? `(${ch})` : "", s.final_value ?? ""].filter(Boolean).join(" ");
  }
  function renderEvidence(c) {
    const rows = [];
    if (c.solves) {
      rows.push(`<li>${verdictMark(c.solves.s1_matches_key)}<span class="k">Blind solve 1</span><span class="v">${esc(solveVal(c.solves.s1))}<small>${keyWord(c.solves.s1_matches_key)} Solved from scratch without seeing the key, the solution or the student's comment.</small></span></li>`);
      rows.push(`<li>${verdictMark(c.solves.s2_matches_key)}<span class="k">Blind solve 2</span><span class="v">${esc(solveVal(c.solves.s2))}<small>${keyWord(c.solves.s2_matches_key)} Second examiner, checks each option separately.</small></span></li>`);
      const sf = c.audit && c.audit.solution_final;
      if (sf && (sf.value || sf.option)) rows.push(`<li><span class="ic i">·</span><span class="k">Stored solution ends at</span><span class="v">${esc([sf.option ? `(${sf.option})` : "", sf.value].filter(Boolean).join(" "))}</span></li>`);
      rows.push(`<li><span class="ic i">·</span><span class="k">Stored key</span><span class="v">${esc(c.answer_display)}</span></li>`);
    }
    (c.findings || []).forEach((f) => rows.push(`<li><span class="ic y">✓</span><span class="k">Code check</span><span class="v">${esc(f.evidence)}</span></li>`));
    const notes = (c.primary.guard_notes || []).filter(Boolean);
    const p = c.primary;
    return `<div class="block"><h3>Why the AI thinks so</h3>
      ${rows.length ? `<ul class="checks">${rows.join("")}</ul>` : ""}
      <p class="why">${esc(p.evidence)}</p>
      ${notes.length ? `<div class="guard"><b>Held back for you by the safety rules</b><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    </div>`;
  }

  // ---------------- render: reports + replies
  function ago(iso) {
    const d = new Date(iso);
    return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  }
  function renderReports(c) {
    const ov = overrides[c.id]?.replies || {};
    return `<div class="block"><h3>What students wrote, and what they'll see back</h3><div class="reports">` +
      c.reports.map((r) => `<div class="report">
        <q>${esc(r.comment)}</q>
        <span class="who">${esc(r.source)}, ${ago(r.reportedAt)}</span>
        ${r.injection ? `<span class="inj">Tried to give the system instructions. Ignored; only the question was checked.</span>` : ""}
        <div class="reply"><label for="rep-${r.reportId}">Reply to this student (click to edit)</label>
          <div class="bubble-msg" id="rep-${r.reportId}" contenteditable="true" data-rid="${r.reportId}" role="textbox" aria-multiline="true">${esc(ov[r.reportId] ?? r.student_reply)}</div>
        </div></div>`).join("") + `</div></div>`;
  }

  // ---------------- render: editor
  function renderEditor(c) {
    const e = asList(editOf(c))[0];
    if (!e) return `<div class="block editor"><label>No field edit is proposed for this case. Add a note for the audit log:</label>
      <textarea id="ed-note" placeholder="e.g. Distractor (D) should read 5 m/s"></textarea>
      <div class="row"><button class="btn primary" id="ed-save" type="button">Save note and approve</button><button class="btn secondary" id="ed-cancel" type="button">Cancel</button></div></div>`;
    return `<div class="block editor">
      <label for="ed-rep">Change <b>${esc(e.field)}</b>: replace the highlighted text with</label>
      <textarea id="ed-rep">${esc(e.replace)}</textarea>
      <label for="ed-note">Note for the audit log (optional)</label><input id="ed-note">
      <div class="row"><button class="btn primary" id="ed-save" type="button">Save and approve</button><button class="btn secondary" id="ed-cancel" type="button">Cancel</button></div></div>`;
  }

  // ---------------- render: case
  function renderCase() {
    const c = cases.find((x) => x.id === current);
    const el = $("#case");
    if (!c) { el.innerHTML = `<div class="empty"><h2>Nothing to review</h2><p>The queue is empty.</p></div>`; return; }
    const p = c.primary, d = decisionOf(c.id), A = ACTIONS[c.route];
    const injected = c.reports.some((r) => r.injection);
    const routeLabel = { faculty_review: "Waiting for you", auto_apply: "Fixed automatically", no_change: "No change" }[c.route];
    const conf = p.confidence != null ? `${Math.round(p.confidence * 100)}% confident` : "";
    el.innerHTML = `
      <header class="case-head">
        <p class="crumb">${esc(c.subject)}, ${esc(c.chapter)}, ${c.qtype === "integerQuestion" ? "integer" : c.qtype === "mcq" ? "multi-correct" : "single-correct"}</p>
        <h2>${esc(title(c))}</h2>
        <ul class="facts">
          <li class="route-${c.route}">${routeLabel}</li>
          <li>${esc(c.headline)}</li>
          <li>${c.reports.length} student${c.reports.length > 1 ? "s" : ""} reported</li>
          ${conf ? `<li>${conf}</li>` : ""}
          ${injected ? `<li class="inj">Prompt-injection attempt ignored</li>` : ""}
        </ul>
      </header>
      ${editing ? renderEditor(c) : ""}
      ${renderOMR(c)}
      ${renderPapers(c)}
      ${renderEvidence(c)}
      ${renderReports(c)}
      <details class="raw block"><summary>Raw engine output for this case</summary><pre>${esc(JSON.stringify({ primary: p, reports: c.reports.map(({ student_reply, ...r }) => r) }, null, 1))}</pre></details>
      <p class="foot">Results precomputed by the engine (${esc(DATA.meta?.provider || "")} ${esc(DATA.meta?.model || "")}, ${esc(DATA.meta?.generated_at || "")}). This page does not call an AI; your actions are saved in this browser's audit log.</p>
      <div class="actions">
        ${d ? `<span class="decided">${d.action === "rejected" ? `<b class="no">${A.noDone}</b>` : `<b class="ok">${d.action === "edited" ? "Edited and approved" : A.okDone}</b>`} ${new Date(d.at).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" })}</span>` : ""}
        <button class="btn primary" id="act-ok" type="button">${A.ok} <kbd>A</kbd></button>
        ${c.route !== "no_change" ? `<button class="btn secondary" id="act-edit" type="button">Edit <kbd>E</kbd></button>` : ""}
        <button class="btn danger" id="act-no" type="button">${A.no} <kbd>R</kbd></button>
        <span class="spacer"></span>
        <span class="nav"><kbd>K</kbd> previous · <kbd>J</kbd> next</span>
      </div>`;
    $("#act-ok").onclick = () => act("approved");
    $("#act-no").onclick = () => act("rejected");
    const ed = $("#act-edit"); if (ed) ed.onclick = () => { editing = true; renderCase(); $("#ed-rep, #ed-note")?.focus(); };
    if (editing) {
      $("#ed-cancel").onclick = () => { editing = false; renderCase(); };
      $("#ed-save").onclick = () => {
        const rep = $("#ed-rep"), note = $("#ed-note")?.value || "";
        if (rep) {
          const edits = asList(editOf(c)).map((x, i) => (i === 0 ? { ...x, replace: rep.value } : x));
          overrides[c.id] = { ...(overrides[c.id] || {}), edit: Array.isArray(editOf(c)) ? edits : edits[0] };
          store.set(OVR_KEY, overrides);
        }
        editing = false;
        act("edited", note);
      };
    }
    el.querySelectorAll(".bubble-msg").forEach((b) => b.addEventListener("blur", () => {
      const o = overrides[c.id] || {}; o.replies = { ...(o.replies || {}), [b.dataset.rid]: b.textContent.trim() };
      overrides[c.id] = o; store.set(OVR_KEY, overrides);
    }));
    el.scrollTop = 0;
  }

  // ---------------- actions + audit
  function act(action, note = "") {
    const c = cases.find((x) => x.id === current);
    const entry = {
      at: new Date().toISOString(), actor: "faculty", caseId: c.id, reportIds: c.reports.map((r) => r.reportId), action,
      route_before: c.route, repair: c.primary.repair, ai_edit: c.primary.edit ?? null, final_edit: action === "rejected" ? null : editOf(c), note,
    };
    log.push(entry); store.set(LOG_KEY, log);
    const A = ACTIONS[c.route];
    toast(action === "rejected" ? A.noDone : action === "edited" ? "Edited and approved" : A.okDone);
    next(true);
  }
  function toast(t) { const el = $("#toast"); el.textContent = t; el.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (el.hidden = true), 1600); }

  function select(id) { current = id; editing = false; renderQueue(); renderCase(); }
  function next(skipDone = false) {
    const i = ordered.findIndex((c) => c.id === current);
    let n = ordered.slice(i + 1).find((c) => !skipDone || !decisionOf(c.id)) || ordered.find((c) => !decisionOf(c.id));
    if (!n) n = ordered[Math.min(i + 1, ordered.length - 1)];
    select(n.id); renderProgress();
  }
  function prev() { const i = ordered.findIndex((c) => c.id === current); select(ordered[Math.max(i - 1, 0)].id); }

  const PLAIN = { answer_key: "answer key", type_mismatch: "question type", unsound: "no valid answer", stem_data: "missing value",
    option_repair: "option", solution_fix: "solution", diagram: "figure", identical_options: "duplicate options", answer_form: "unit",
    notation: "notation", markup_leak: "raw code visible", wrong_chapter: "chapter", syllabus: "syllabus", spelling: "spelling",
    noise: "stray text", exam_reference: "exam tag" };

  // ---------------- drawer: audit log, unreported issues, help
  function openDrawer(t, html) { $("#drawer-title").textContent = t; $("#drawer-body").innerHTML = html; $("#drawer").hidden = false; $("#drawer-close").focus(); }
  $("#drawer-close").onclick = () => ($("#drawer").hidden = true);
  $("#btn-log").onclick = () => {
    const items = [...log].reverse().map((e) => `<div class="log-item"><div><b>${esc(e.action)}</b> · ${esc(e.repair || "no repair")} · ${esc(e.reportIds.join(", "))}</div>
      <div class="when">${new Date(e.at).toLocaleString("en-IN")} · question ${esc(e.caseId)}${e.note ? ` · “${esc(e.note)}”` : ""}</div></div>`).join("");
    openDrawer("Audit log", `<p><button class="btn secondary" id="log-export" type="button">Download as JSON</button></p>${items || "<p>No actions yet. Every approve, edit and reject is recorded here.</p>"}`);
    $("#log-export").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(log, null, 2)], { type: "application/json" }));
      a.download = `acadza-audit-${new Date().toISOString().slice(0, 10)}.json`; a.click();
    };
  };
  $("#btn-bonus").onclick = () => {
    const b = DATA.bonus || [];
    openDrawer(`Found without a student report (${b.length})`, `<p class="sub">The engine audits every question, not only the reported ones. These are worth a look.</p>` +
      b.map((x) => `<div class="bonus-item"><div class="what">${esc(x.evidence)}</div><div class="where">${esc(x.subject)}, ${esc(x.chapter)} · ${esc(PLAIN[x.repair] || x.kind)} · ${x.reported ? "question was reported for something else" : "never reported"}</div></div>`).join(""));
  };
  $("#btn-help").onclick = () => openDrawer("Keyboard shortcuts", `<div class="keys">
    <kbd>A</kbd><span>Approve / keep / confirm</span><kbd>E</kbd><span>Edit the fix before approving</span><kbd>R</kbd><span>Reject / undo / reopen</span>
    <kbd>J</kbd><span>Next case</span><kbd>K</kbd><span>Previous case</span><kbd>Esc</kbd><span>Close panel or editor</span></div>`);

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("textarea, input, [contenteditable]")) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "escape") { $("#drawer").hidden = true; if (editing) { editing = false; renderCase(); } }
    else if (!$("#drawer").hidden) return;
    else if (k === "j") next();
    else if (k === "k") prev();
    else if (k === "a") act("approved");
    else if (k === "r") act("rejected");
    else if (k === "e" && $("#act-edit")) $("#act-edit").click();
    else if (k === "?") $("#btn-help").click();
  });

  renderProgress(); renderQueue(); renderCase();
})();
