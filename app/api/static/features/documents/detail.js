// features/documents/detail.js — the Documents detail slide-over
// panel. CD-4 WI-4's `Detail` object (open/close/timeline rendering)
// relocated unchanged by CD-5 WI-4's modularisation, PLUS this WI's
// own additions:
//
// * `_renderAiPanel` — the "AI Analysis" section (PID §45) every
//   detail view gains, showing every AIInvocation for this document
//   (via the new `primary_input_reference` filter, deliverable 1),
//   "Run analysis"/"Retry" actions, and "Ask BAGMAN about this".
// * `openForEvidence(evidenceId)` — a second entry point (used when
//   Ask BAGMAN's response references an evidence_id the operator was
//   not already looking at, PID §44 "make relevant referenced records
//   clickable") that renders a reduced, evidence-only view when no
//   IntakeRecord is at hand — a documented, honestly-scoped limitation
//   (see its own docstring below), not a fabricated intake record.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtBytes, fmtDateTime, statusBadge } from "../../shared/format.js";
import { API, apiGet, errorMessage } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import { DOCUMENT_BACKGROUND_TASKS, listInvocationsForEvidence, runBackgroundTask } from "../ai/ai-api.js";
import { renderInvocationCard } from "../ai/invocation-card.js";
import * as AskBagman from "../ai/ask-bagman.js";
import { ClassificationPanel } from "./classification-panel.js";

export const Detail = {
  /** CD-6 Slice 5 WI-5 §13 — the primary entry point from the new
   * evidence-first Documents list: `row` is one item from
   * `GET /internal/documents` (services/evidence/document_projection.py's
   * own row shape) rather than a raw IntakeRecord. Renders the SAME
   * detail slide-over as `open(record)` (an IntakeRecord), with the new
   * "Classification" section placed ABOVE the generic "AI Analysis"
   * section (WI-5 §13). When this document also has a real intake
   * record (`row.intake`), that record is fetched and rendered via the
   * existing `open()` renderer so upload-specific fields (quarantine
   * reason, idempotency key, ...) are not lost — otherwise this renders
   * the reduced evidence-only fields directly. */
  async openDocument(row) {
    if (row.intake && row.intake.intake_id) {
      const { ok, body: record } = await apiGet(API.intakeOne(row.intake.intake_id));
      if (ok && record) {
        await this.open(record);
        return;
      }
    }
    await this._openEvidenceFirst(row);
  },

  async _openEvidenceFirst(row) {
    const overlay = qs("#detail-overlay");
    const body = qs("#detail-body");
    qs("#detail-title").textContent = row.document_label || row.evidence_id;
    clear(body);
    overlay.hidden = false;

    // ---- Classification (WI-5 §13 — ABOVE AI Analysis) ----
    const classificationSection = el("div", { class: "detail-section" });
    body.appendChild(classificationSection);
    await ClassificationPanel.render(classificationSection, row.evidence_id);

    const [evRes, provRes] = await Promise.all([
      apiGet(API.evidenceOne(row.evidence_id)),
      apiGet(API.provenance("EvidenceItem", row.evidence_id)),
    ]);
    if (!evRes.ok) {
      body.appendChild(el("div", { class: "reason-box reason-box--bad", text: errorMessage(evRes.status, evRes.body) }));
      return;
    }
    const evidence = evRes.body;
    const provenanceEdges = provRes.ok && Array.isArray(provRes.body) ? provRes.body : [];
    let sourceLabelText = "—";
    const sourceEdge = provenanceEdges.find((e) => e.source);
    if (sourceEdge && sourceEdge.source) {
      sourceLabelText = `${sourceEdge.source.source_type} / ${sourceEdge.source.provider}`;
    }

    const kv = el("dl", { class: "detail-kv" });
    const rows = [
      ["Evidence ID", evidence.evidence_id],
      ["Entity", row.entity ? row.entity.display_name : "UNRESOLVED"],
      ["Sender", row.sender_address || "—"],
      ["Subject", row.subject || "—"],
      ["Evidence type", evidence.evidence_type || "—"],
      ["Evidence state", evidence.status || "—"],
      [
        "Content hash",
        evidence.content_hash ? `${evidence.content_hash.algorithm} ${evidence.content_hash.value}` : "—",
      ],
      ["Size", fmtBytes(evidence.size_bytes)],
      ["Source", sourceLabelText],
      ["Received", fmtDateTime(evidence.received_at)],
    ];
    for (const [k, v] of rows) {
      kv.appendChild(el("dt", { text: k }));
      kv.appendChild(el("dd", { text: v === null || v === undefined ? "—" : String(v) }));
    }
    body.appendChild(el("div", { class: "detail-section" }, [el("h3", { text: "Fields" }), kv]));

    // ---- download (WI-5 §19 — classification never replaces original evidence) ----
    const actions = el("div", { class: "detail-section" }, [el("h3", { text: "Actions" })]);
    actions.appendChild(
      el("a", {
        class: "btn btn--primary",
        text: "Download original",
        attrs: { href: API.evidenceContent(row.evidence_id), download: row.original_name || row.evidence_id },
      })
    );
    body.appendChild(actions);

    // ---- AI Analysis (existing generic panel, preserved — WI-5 §63) ----
    const aiSection = el("div", { class: "detail-section" });
    body.appendChild(aiSection);
    await this._renderAiPanel(aiSection, row.evidence_id);
  },

  async open(record) {
    const overlay = qs("#detail-overlay");
    const body = qs("#detail-body");
    qs("#detail-title").textContent = `Intake ${record.intake_id}`;
    clear(body);
    overlay.hidden = false;

    body.appendChild(el("div", { class: "detail-section" }, [statusBadge(record.status)]));

    if (record.status === "QUARANTINED" && record.quarantine_reason) {
      body.appendChild(
        el("div", { class: "reason-box reason-box--warn", text: `Quarantined: ${record.quarantine_reason}` })
      );
    }
    if ((record.status === "REJECTED" || record.status === "FAILED") && record.failure_code) {
      body.appendChild(
        el("div", {
          class: "reason-box reason-box--bad",
          text: `${record.status === "REJECTED" ? "Rejected" : "Failed"}: ${record.failure_code}`,
        })
      );
    }

    let evidence = null;
    let provenanceEdges = [];
    if (record.evidence_id) {
      const [evRes, provRes] = await Promise.all([
        apiGet(API.evidenceOne(record.evidence_id)),
        apiGet(API.provenance("EvidenceItem", record.evidence_id)),
      ]);
      if (evRes.ok) evidence = evRes.body;
      if (provRes.ok && Array.isArray(provRes.body)) provenanceEdges = provRes.body;
    }

    // Real Source, when we have it (from provenance) — falls back to
    // the same documented static label the list view uses when no
    // evidence/provenance exists yet (nothing to resolve it from).
    let sourceLabel = "Manual Upload (MANUAL_UPLOAD)";
    const sourceEdge = provenanceEdges.find((e) => e.source);
    if (sourceEdge && sourceEdge.source) {
      sourceLabel = `${sourceEdge.source.source_type} / ${sourceEdge.source.provider}`;
    }

    const kv = el("dl", { class: "detail-kv" });
    const rows = [
      ["Evidence ID", record.evidence_id || "—"],
      ["Intake ID", record.intake_id],
      ["Entity", record.entity_hint || "UNRESOLVED"],
      ["Filename", record.original_filename || "—"],
      ["Reported MIME", record.reported_mime_type || "—"],
      ["Detected MIME", record.detected_mime_type || "—"],
      [
        "Content hash",
        record.content_hash ? `${record.content_hash.algorithm} ${record.content_hash.value}` : "—",
      ],
      ["Size", fmtBytes(record.size_bytes)],
      ["Source", sourceLabel],
      ["Intake state", record.status],
      ["Evidence state", evidence ? evidence.status : "—"],
      ["Correlation ID", record.correlation_id],
      ["Idempotency key", record.idempotency_key || "—"],
    ];
    for (const [k, v] of rows) {
      kv.appendChild(el("dt", { text: k }));
      kv.appendChild(el("dd", { text: v === null || v === undefined ? "—" : String(v) }));
    }
    body.appendChild(el("div", { class: "detail-section" }, [el("h3", { text: "Fields" }), kv]));

    // ---- timeline (PID §39's "audit timeline") ----
    // No dedicated GET /internal/audit/... endpoint exists yet (this
    // WI's report flags that as a gap for a future delivery) — this
    // renders whatever real timeline-shaped information the endpoints
    // we DO have actually expose: the IntakeRecord's own two
    // timestamps, plus provenance edges when evidence exists. It is
    // not a substitute for a full AuditEvent trail.
    const timeline = el("ul", { class: "timeline" });
    timeline.appendChild(
      this._timelineItem("Intake received", record.received_at)
    );
    for (const edge of provenanceEdges) {
      if (edge.provenance) {
        const label = edge.provenance.relationship || edge.provenance.provenance_id || "Provenance link";
        timeline.appendChild(this._timelineItem(String(label), edge.provenance.created_at));
      }
    }
    if (evidence) {
      timeline.appendChild(this._timelineItem(`Evidence registered (${evidence.status})`, evidence.created_at));
    }
    if (record.completed_at) {
      timeline.appendChild(this._timelineItem(`Intake reached ${record.status}`, record.completed_at));
    }
    body.appendChild(
      el("div", { class: "detail-section" }, [
        el("h3", { text: "Timeline" }),
        timeline,
        el("p", {
          class: "muted small",
          text:
            "Derived from IntakeRecord timestamps and provenance edges only — a dedicated audit-event " +
            "endpoint would be needed for the complete AuditEvent trail (see WI-4 report).",
        }),
      ])
    );

    // ---- download ----
    const actions = el("div", { class: "detail-section" }, [el("h3", { text: "Actions" })]);
    if (record.evidence_id) {
      actions.appendChild(
        el("a", {
          class: "btn btn--primary",
          text: "Download original",
          attrs: {
            href: API.evidenceContent(record.evidence_id),
            download: record.original_filename || record.evidence_id,
          },
        })
      );
    } else {
      actions.appendChild(el("p", { class: "muted", text: "No evidence registered yet — nothing to download." }));
    }
    body.appendChild(actions);

    // ---- Classification (CD-6 Slice 5 WI-5 §13 — ABOVE AI Analysis) ----
    if (record.evidence_id) {
      const classificationSection = el("div", { class: "detail-section" });
      body.appendChild(classificationSection);
      await ClassificationPanel.render(classificationSection, record.evidence_id);
    }

    // ---- AI Analysis (CD-5 WI-4, PID §45) ----
    if (record.evidence_id) {
      const aiSection = el("div", { class: "detail-section" });
      body.appendChild(aiSection);
      await this._renderAiPanel(aiSection, record.evidence_id);
    }
  },

  /**
   * A reduced detail view keyed on `evidence_id` alone — no
   * IntakeRecord is available (or even guaranteed to exist — an
   * ASK_BAGMAN turn may reference evidence uploaded in a prior
   * session). Used when a referenced evidence_id in an Ask BAGMAN
   * answer is clicked (PID §44) and by the AI panel's own "Ask BAGMAN
   * about this" round trip. Deliberately does NOT fabricate intake
   * fields (status/quarantine_reason/idempotency_key/...) it has no
   * data for — a documented, honestly-scoped limitation: opening this
   * way shows less than opening from the Documents list/row does, and
   * says so.
   */
  async openForEvidence(evidenceId) {
    const overlay = qs("#detail-overlay");
    const body = qs("#detail-body");
    qs("#detail-title").textContent = `Document ${evidenceId}`;
    clear(body);
    overlay.hidden = false;

    body.appendChild(
      el("p", {
        class: "muted small",
        text: "Opened directly from a document reference — no intake record loaded for this view.",
      })
    );

    const [evRes, provRes] = await Promise.all([
      apiGet(API.evidenceOne(evidenceId)),
      apiGet(API.provenance("EvidenceItem", evidenceId)),
    ]);

    if (!evRes.ok) {
      body.appendChild(
        el("div", { class: "reason-box reason-box--bad", text: errorMessage(evRes.status, evRes.body) })
      );
      return;
    }
    const evidence = evRes.body;
    const provenanceEdges = provRes.ok && Array.isArray(provRes.body) ? provRes.body : [];

    let sourceLabel = "—";
    const sourceEdge = provenanceEdges.find((e) => e.source);
    if (sourceEdge && sourceEdge.source) {
      sourceLabel = `${sourceEdge.source.source_type} / ${sourceEdge.source.provider}`;
    }

    const kv = el("dl", { class: "detail-kv" });
    const rows = [
      ["Evidence ID", evidence.evidence_id],
      ["Evidence type", evidence.evidence_type || "—"],
      ["Evidence state", evidence.status || "—"],
      [
        "Content hash",
        evidence.content_hash ? `${evidence.content_hash.algorithm} ${evidence.content_hash.value}` : "—",
      ],
      ["Size", fmtBytes(evidence.size_bytes)],
      ["MIME type", evidence.mime_type || "—"],
      ["Source", sourceLabel],
    ];
    for (const [k, v] of rows) {
      kv.appendChild(el("dt", { text: k }));
      kv.appendChild(el("dd", { text: v === null || v === undefined ? "—" : String(v) }));
    }
    body.appendChild(el("div", { class: "detail-section" }, [el("h3", { text: "Fields" }), kv]));

    const actions = el("div", { class: "detail-section" }, [el("h3", { text: "Actions" })]);
    actions.appendChild(
      el("a", {
        class: "btn btn--primary",
        text: "Download original",
        attrs: { href: API.evidenceContent(evidenceId), download: evidenceId },
      })
    );
    body.appendChild(actions);

    // ---- Classification (CD-6 Slice 5 WI-5 §13 — ABOVE AI Analysis) ----
    const classificationSection = el("div", { class: "detail-section" });
    body.appendChild(classificationSection);
    await ClassificationPanel.render(classificationSection, evidenceId);

    const aiSection = el("div", { class: "detail-section" });
    body.appendChild(aiSection);
    await this._renderAiPanel(aiSection, evidenceId);
  },

  // ---- AI Analysis section (PID §45/§54) ----

  async _renderAiPanel(container, evidenceId) {
    clear(container);
    container.appendChild(el("h3", { text: "AI Analysis" }));
    container.appendChild(
      el("p", {
        class: "muted small",
        text: "AI-derived analysis of this document — proposals only, never canonical (PID §54).",
      })
    );

    const askBtn = el("button", {
      class: "btn btn--secondary",
      text: "Ask BAGMAN about this",
      attrs: { type: "button" },
      on: { click: () => AskBagman.open({ evidenceId }) },
    });
    container.appendChild(el("div", { class: "ai-panel__toolbar" }, [askBtn]));

    const listWrap = el("div", { class: "ai-panel__list" }, [
      el("p", { class: "muted small", text: "Loading AI analysis…" }),
    ]);
    container.appendChild(listWrap);

    const { ok, status, body } = await listInvocationsForEvidence(evidenceId);
    clear(listWrap);

    let invocations = [];
    if (!ok) {
      listWrap.appendChild(
        el("p", { class: "muted small", text: `Could not load AI analysis: ${errorMessage(status, body)}` })
      );
    } else {
      invocations = body.items || [];
      if (invocations.length === 0) {
        listWrap.appendChild(el("p", { class: "muted small", text: "No AI analysis has been run for this document yet." }));
      }
      for (const invocation of invocations) {
        listWrap.appendChild(
          renderInvocationCard(invocation, {
            onRetry: (inv) => this._runTask(container, evidenceId, inv.task_id, inv.task_version),
          })
        );
      }
    }

    // "Run analysis" buttons — one per relevant BACKGROUND task,
    // disabled when a non-terminal invocation for that exact task
    // already exists for this document (a proactive GUI courtesy, not
    // a substitute for the server's own PID §73 concurrency guard —
    // see ai.invocation.AIInvocationRepository.find_active_invocation's
    // own docstring for this exact "grey out a run analysis button"
    // motivation).
    const runRow = el("div", { class: "ai-panel__run-row" });
    for (const task of DOCUMENT_BACKGROUND_TASKS) {
      const active = invocations.some(
        (inv) => inv.task_id === task.task_id && (inv.status === "REQUESTED" || inv.status === "RUNNING")
      );
      const btn = el("button", {
        class: "btn btn--secondary",
        text: active ? `${task.label}: in progress…` : `Run analysis: ${task.label}`,
        attrs: { type: "button" },
      });
      btn.disabled = active;
      btn.addEventListener("click", () => this._runTask(container, evidenceId, task.task_id, task.task_version, btn));
      runRow.appendChild(btn);
    }
    container.appendChild(runRow);
  },

  async _runTask(container, evidenceId, taskId, taskVersion, btn) {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Running…"; // honest — the fetch below IS the wait (PID §38)
    }
    const runningNote = el("p", { class: "muted small ai-panel__running", text: `Running ${taskId}…` });
    container.appendChild(runningNote);

    const result = await runBackgroundTask({ taskId, taskVersion, evidenceId, actorId: getActorId() });

    if (runningNote.parentNode) runningNote.parentNode.removeChild(runningNote);

    if (!result.ok) {
      const message =
        result.status === 409
          ? "An analysis for this task is already in flight for this document."
          : errorMessage(result.status, result.body);
      container.appendChild(el("div", { class: "reason-box reason-box--warn", text: message }));
      if (btn) {
        btn.disabled = false;
        btn.textContent = `Run analysis`;
      }
      return;
    }

    // Re-render the whole panel so the new/updated card and the
    // run-button's active/disabled state stay consistent with the
    // server's own view, rather than hand-patching DOM state locally.
    await this._renderAiPanel(container, evidenceId);
  },

  _timelineItem(label, timestamp) {
    return el("li", {}, [
      el("div", { class: "t-label", text: label }),
      el("div", { class: "t-time", text: fmtDateTime(timestamp) }),
    ]);
  },

  close() {
    qs("#detail-overlay").hidden = true;
  },

  initClosePanel() {
    qs("#detail-close").addEventListener("click", () => Detail.close());
    qs("#detail-overlay").addEventListener("click", (e) => {
      if (e.target === qs("#detail-overlay")) Detail.close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") Detail.close();
    });
  },
};
