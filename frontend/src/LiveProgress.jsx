import { useEffect, useMemo, useState } from "react";

const STEP_DEFINITIONS = [
  { id: "planning", title: "Strategy & Planning", icon: "🎯", desc: "Formulating multi-tool search queries" },
  { id: "searching", title: "Information Gathering", icon: "🌐", desc: "Retrieving web sources & deep pages" },
  { id: "summarizing", title: "Evidence Synthesis", icon: "📊", desc: "Extracting verified facts & findings" },
  { id: "writing", title: "Report Drafting", icon: "✍️", desc: "Generating structured markdown report" },
  { id: "critiquing", title: "Fact-Check & Review", icon: "⚖️", desc: "Verifying citations & assessing accuracy" },
  { id: "finalizing", title: "Finalization", icon: "🏆", desc: "Formatting citations & exports" },
];

export default function LiveProgress({ events = [], isLive = true, onReportReady }) {
  const [expandedSteps, setExpandedSteps] = useState({
    planning: true,
    searching: true,
    summarizing: false,
    writing: false,
    critiquing: false,
    finalizing: true,
  });

  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    if (!isLive) return;
    const timer = setInterval(() => {
      setElapsedSeconds((s) => s + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, [isLive]);

  const toggleStep = (stepId) => {
    setExpandedSteps((prev) => ({ ...prev, [stepId]: !prev[stepId] }));
  };

  // Group events by step
  const stepData = useMemo(() => {
    const data = {
      planning: { status: "pending", events: [], plan: null },
      searching: { status: "pending", events: [], sources: [], toolsUsed: new Set() },
      summarizing: { status: "pending", events: [], summary: "" },
      writing: { status: "pending", events: [], drafts: [], iteration: 1 },
      critiquing: { status: "pending", events: [], critique: null },
      finalizing: { status: "pending", events: [], report: null },
    };

    let activeStepFound = false;

    for (const ev of events) {
      const step = ev.step;
      if (!data[step]) continue;

      data[step].events.push(ev);

      if (ev.type === "plan") {
        data.planning.plan = ev.details;
        data.planning.status = "done";
      } else if (ev.type === "tool_start") {
        data.searching.status = "running";
        if (ev.details?.provider) data.searching.toolsUsed.add(ev.details.provider);
      } else if (ev.type === "tool_result") {
        if (ev.details?.sources) {
          data.searching.sources = [...data.searching.sources, ...ev.details.sources];
        }
      } else if (ev.type === "sources") {
        data.searching.status = "done";
        if (ev.details?.citations) {
          data.searching.sources = ev.details.citations;
        }
      } else if (ev.type === "summary") {
        data.summarizing.summary = ev.details?.preview || "";
        data.summarizing.status = "done";
      } else if (ev.type === "draft") {
        data.writing.drafts.push(ev.details);
        data.writing.status = "done";
        data.writing.iteration = ev.details?.iteration || 1;
      } else if (ev.type === "critique") {
        data.critiquing.critique = ev.details?.critique;
        data.critiquing.status = "done";
      } else if (ev.type === "completed") {
        data.finalizing.status = "done";
        data.finalizing.report = ev.details;
      } else if (ev.type === "error" || ev.type === "blocked") {
        if (step && data[step]) data[step].status = "error";
      }
    }

    // Determine running state based on progression
    const stepOrder = ["planning", "searching", "summarizing", "writing", "critiquing", "finalizing"];
    for (let i = 0; i < stepOrder.length; i++) {
      const s = stepOrder[i];
      if (data[s].events.length > 0 && data[s].status === "pending") {
        data[s].status = "running";
      }
    }

    return data;
  }, [events]);

  const latestEvent = events[events.length - 1];

  return (
    <div className="live-progress-container">
      <div className="live-progress-header">
        <div className="live-progress-badge">
          <span className={`pulse-dot ${isLive ? "pulse-active" : "pulse-done"}`} />
          <span className="live-progress-title">
            {isLive ? "REAL-TIME MULTI-AGENT EXECUTION GRAPH" : "RESEARCH EXECUTION TRACE"}
          </span>
        </div>
        <div className="live-progress-meta">
          <span className="elapsed-time">⏱️ {elapsedSeconds}s elapsed</span>
          <span className="events-counter">{events.length} events streamed</span>
        </div>
      </div>

      {latestEvent && isLive && (
        <div className="live-current-activity">
          <div className="activity-spinner" />
          <div className="activity-content">
            <span className="activity-label">Active Step</span>
            <p className="activity-text">{latestEvent.title}</p>
          </div>
        </div>
      )}

      <div className="steps-timeline">
        {STEP_DEFINITIONS.map((def, idx) => {
          const stepInfo = stepData[def.id] || { status: "pending", events: [] };
          const isDone = stepInfo.status === "done";
          const isRunning = stepInfo.status === "running";
          const isError = stepInfo.status === "error";
          const isExpanded = expandedSteps[def.id];

          return (
            <div
              key={def.id}
              className={`timeline-item ${isDone ? "step-done" : ""} ${isRunning ? "step-running" : ""} ${
                isError ? "step-error" : ""
              } ${stepInfo.status === "pending" ? "step-pending" : ""}`}
            >
              <div className="timeline-node" onClick={() => toggleStep(def.id)}>
                <div className="timeline-icon-wrap">
                  {isDone ? (
                    <span className="node-icon check-icon">✓</span>
                  ) : isRunning ? (
                    <span className="node-icon spin-icon">⟳</span>
                  ) : isError ? (
                    <span className="node-icon error-icon">✕</span>
                  ) : (
                    <span className="node-icon step-num">{idx + 1}</span>
                  )}
                </div>

                <div className="timeline-header-info">
                  <div className="timeline-title-row">
                    <span className="step-emoji">{def.icon}</span>
                    <strong className="step-name">{def.title}</strong>
                    <span className={`step-badge badge-${stepInfo.status}`}>
                      {stepInfo.status === "done"
                        ? "Completed"
                        : stepInfo.status === "running"
                        ? "Executing..."
                        : stepInfo.status === "error"
                        ? "Error"
                        : "Waiting"}
                    </span>
                  </div>
                  <span className="step-desc">{def.desc}</span>
                </div>

                <button
                  type="button"
                  className="step-toggle-btn"
                  aria-label={isExpanded ? "Collapse step details" : "Expand step details"}
                >
                  {isExpanded ? "▲" : "▼"}
                </button>
              </div>

              {isExpanded && stepInfo.events.length > 0 && (
                <div className="timeline-body">
                  {/* Step 1: Strategy & Planning Details */}
                  {def.id === "planning" && stepInfo.plan && (
                    <div className="step-detail-card">
                      <div className="detail-row">
                        <span className="detail-tag">Selected Tools</span>
                        <div className="tool-badges">
                          {(stepInfo.plan.tools || []).map((tool) => (
                            <span key={tool} className={`tool-badge tool-${tool}`}>
                              {tool === "tavily" ? "🔍 Tavily Web" : "📚 Wikipedia"}
                            </span>
                          ))}
                        </div>
                      </div>
                      {stepInfo.plan.queries?.length > 0 && (
                        <div className="detail-row">
                          <span className="detail-tag">Generated Sub-Queries</span>
                          <ul className="query-list">
                            {stepInfo.plan.queries.map((q, qIdx) => (
                              <li key={qIdx}>"{q}"</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Step 2: Information Gathering Details */}
                  {def.id === "searching" && (
                    <div className="step-detail-card">
                      <div className="detail-row">
                        <span className="detail-tag">Sources Discovered</span>
                        <span className="sources-count-badge">
                          {stepInfo.sources.length} sources verified
                        </span>
                      </div>
                      {stepInfo.sources.length > 0 && (
                        <div className="sources-preview-grid">
                          {stepInfo.sources.slice(0, 4).map((s, sIdx) => (
                            <div key={sIdx} className="source-mini-card">
                              <span className="source-mini-provider">
                                {s.provider || "web"}
                              </span>
                              <a
                                href={s.url}
                                target="_blank"
                                rel="noreferrer"
                                className="source-mini-title"
                              >
                                {s.title || s.url}
                              </a>
                            </div>
                          ))}
                          {stepInfo.sources.length > 4 && (
                            <div className="more-sources-note">
                              +{stepInfo.sources.length - 4} more sources in bibliography
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Step 3: Summarization Details */}
                  {def.id === "summarizing" && stepInfo.summary && (
                    <div className="step-detail-card">
                      <span className="detail-tag">Key Findings Preview</span>
                      <p className="summary-preview-text">{stepInfo.summary}</p>
                    </div>
                  )}

                  {/* Step 4: Report Drafting Details */}
                  {def.id === "writing" && stepInfo.drafts.length > 0 && (
                    <div className="step-detail-card">
                      <div className="detail-row">
                        <span className="detail-tag">Draft Revision</span>
                        <span className="iteration-badge">Iteration {stepInfo.iteration}</span>
                      </div>
                      <p className="draft-meta">
                        Generated {stepInfo.drafts[stepInfo.drafts.length - 1]?.draft?.length || 0} characters
                        of cited content.
                      </p>
                    </div>
                  )}

                  {/* Step 5: Fact-Check & Critic Details */}
                  {def.id === "critiquing" && stepInfo.critique && (
                    <div className="step-detail-card">
                      <div className="detail-row">
                        <span className="detail-tag">Quality Assessment</span>
                        <span
                          className={`verdict-pill ${
                            stepInfo.critique.passed ? "verdict-pass" : "verdict-warn"
                          }`}
                        >
                          {stepInfo.critique.passed ? "✓ Verification Passed" : "⚠ Revisions Suggested"}
                        </span>
                      </div>
                      {stepInfo.critique.citation_issues?.length > 0 && (
                        <div className="critique-issues">
                          <span className="issues-label">Citation Flags:</span>
                          <ul>
                            {stepInfo.critique.citation_issues.map((issue, iIdx) => (
                              <li key={iIdx}>{issue}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Event Log Stream */}
                  <div className="sub-events-list">
                    {stepInfo.events.map((ev, evIdx) => (
                      <div key={evIdx} className="sub-event-item">
                        <span className="sub-event-time">
                          {ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : ""}
                        </span>
                        <span className="sub-event-bullet">•</span>
                        <span className="sub-event-title">{ev.title}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
