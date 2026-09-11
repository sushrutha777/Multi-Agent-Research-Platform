import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API = import.meta.env.VITE_API_BASE_URL || "";
const STORAGE = {
  apiKey: "research_agent_api_key",
  sessionId: "research_agent_session_id",
  recentChats: "research_agent_recent_chats",
};

const readStorage = (key, fallback = "") => {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch {
    return fallback;
  }
};

const readRecentChats = () => {
  try {
    const chats = JSON.parse(readStorage(STORAGE.recentChats, "[]"));
    return Array.isArray(chats) ? chats : [];
  } catch {
    return [];
  }
};

const saveRecentChats = (chats) => {
  try {
    localStorage.setItem(STORAGE.recentChats, JSON.stringify(chats.slice(0, 8)));
  } catch {
    // Local storage is a convenience; the chat still works when it is unavailable.
  }
};

function Icon({ name, size = 20 }) {
  const paths = {
    menu: <><path d="M4 6h16M4 12h16M4 18h16" /></>,
    edit: <><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4 1 1-4Z" /></>,
    send: <><path d="m22 2-7 20-4-9-9-4Z" /><path d="M22 2 11 13" /></>,
    mic: <><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3M8 22h8" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06-1.42 1.42-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V19.5h-2v-.08a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06-1.42-1.42.06-.06A1.65 1.65 0 0 0 9.6 15a1.65 1.65 0 0 0-1.51-1H8v-2h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06 1.42-1.42.06.06a1.65 1.65 0 0 0 1.82.33 1.65 1.65 0 0 0 1-1.51V6.5h2v.08a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06 1.42 1.42-.06.06A1.65 1.65 0 0 0 19.4 11c.18.61.74 1 1.38 1H21v2h-.22c-.64 0-1.2.39-1.38 1Z" /></>,
    copy: <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></>,
    download: <><path d="M12 3v12M7 10l5 5 5-5M4 21h16" /></>,
    chevron: <path d="m6 9 6 6 6-6" />,
    close: <><path d="m6 6 12 12M18 6 6 18" /></>,
    spark: <><path d="m12 3-1.2 4.8L6 9l4.8 1.2L12 15l1.2-4.8L18 9l-4.8-1.2Z" /><path d="m19 15-.6 2.4L16 18l2.4.6L19 21l.6-2.4L22 18l-2.4-.6Z" /></>,
  };

  return (
    <svg aria-hidden="true" className="icon" fill="none" height={size} viewBox="0 0 24 24" width={size} xmlns="http://www.w3.org/2000/svg">
      {paths[name]}
    </svg>
  );
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  try {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(timestamp));
  } catch {
    return "";
  }
}

async function getErrorMessage(response, fallback) {
  try {
    const payload = await response.json();
    return payload.detail || payload.error || fallback;
  } catch {
    return fallback;
  }
}

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [apiKey, setApiKey] = useState(() => readStorage(STORAGE.apiKey));
  const [sessionId, setSessionId] = useState(() => readStorage(STORAGE.sessionId));
  const [recentChats, setRecentChats] = useState(readRecentChats);
  const [messages, setMessages] = useState([]);
  const [topic, setTopic] = useState("");
  const [format, setFormat] = useState("text");
  const [job, setJob] = useState(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [latestResult, setLatestResult] = useState(null);
  const [diff, setDiff] = useState("");
  const [showSources, setShowSources] = useState(false);
  const [copied, setCopied] = useState(false);
  const pollRef = useRef(null);
  const composerRef = useRef(null);

  const authHeaders = useCallback(
    (json = false) => {
      const headers = json ? { "Content-Type": "application/json" } : {};
      if (apiKey.trim()) headers["X-API-Key"] = apiKey.trim();
      return headers;
    },
    [apiKey],
  );

  const persistSession = useCallback((nextSessionId) => {
    setSessionId(nextSessionId);
    try {
      if (nextSessionId) localStorage.setItem(STORAGE.sessionId, nextSessionId);
      else localStorage.removeItem(STORAGE.sessionId);
    } catch {
      // Continue without persistence.
    }
  }, []);

  const loadSession = useCallback(
    async (targetSessionId) => {
      if (!targetSessionId) {
        setMessages([]);
        return;
      }
      try {
        const response = await fetch(API + "/session/" + encodeURIComponent(targetSessionId), { headers: authHeaders() });
        if (!response.ok) return;
        const data = await response.json();
        setMessages(data.messages || []);
      } catch {
        // A session is optional; the current chat remains usable if it expires.
      }
    },
    [authHeaders],
  );

  useEffect(() => {
    loadSession(sessionId);
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [loadSession, sessionId]);

  useEffect(() => {
    if (job || messages.length) composerRef.current?.focus();
  }, [job, messages.length]);

  const rememberChat = useCallback((chat) => {
    setRecentChats((current) => {
      const next = [chat, ...current.filter((item) => item.sessionId !== chat.sessionId && item.topic !== chat.topic)];
      saveRecentChats(next);
      return next.slice(0, 8);
    });
  }, []);

  const finishJob = useCallback(
    async (data) => {
      if (pollRef.current) clearTimeout(pollRef.current);
      setJob(null);
      if (data.status === "done") {
        setLatestResult(data);
        setStatus("Research complete");
        setError("");
        await loadSession(data.session_id || sessionId);
      } else if (data.status === "blocked") {
        setStatus("");
        setError(data.error || "The response was blocked by the safety policy.");
      } else {
        setStatus("");
        setError(data.error || "The research job could not be completed.");
      }
    },
    [loadSession, sessionId],
  );

  const pollJob = useCallback(
    async (jobId) => {
      try {
        const response = await fetch(API + "/result/" + encodeURIComponent(jobId), { headers: authHeaders() });
        if (!response.ok) throw new Error(await getErrorMessage(response, "Unable to read job status."));
        const data = await response.json();
        if (data.status === "done" || data.status === "blocked" || data.status === "error") {
          await finishJob(data);
          return;
        }
        setStatus(data.status === "retrying" ? "Retrying research…" : "Researching live sources…");
        pollRef.current = setTimeout(() => pollJob(jobId), 2200);
      } catch (pollError) {
        setJob(null);
        setStatus("");
        setError(pollError.message || "Polling failed.");
      }
    },
    [authHeaders, finishJob],
  );

  const submitResearch = async (event) => {
    event?.preventDefault();
    const trimmedTopic = topic.trim();
    if (!trimmedTopic || job) return;
    setError("");
    setLatestResult(null);
    setDiff("");
    setShowSources(false);
    setCopied(false);
    setStatus("Starting research…");

    try {
      const response = await fetch(API + "/research", {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify({ topic: trimmedTopic, output_format: format, session_id: sessionId }),
      });
      if (!response.ok) throw new Error(await getErrorMessage(response, "Research request failed."));
      const data = await response.json();
      persistSession(data.session_id);
      setJob(data.job_id);
      rememberChat({ id: data.session_id, sessionId: data.session_id, topic: trimmedTopic, updatedAt: new Date().toISOString() });
      setSidebarOpen(false);
      setStatus("Researching live sources…");
      pollRef.current = setTimeout(() => pollJob(data.job_id), 400);
    } catch (submitError) {
      setStatus("");
      setError(submitError.message || "Research request failed.");
    }
  };

  const newChat = () => {
    if (pollRef.current) clearTimeout(pollRef.current);
    setSidebarOpen(false);
    setTopic("");
    setMessages([]);
    setLatestResult(null);
    setDiff("");
    setError("");
    setStatus("");
    setJob(null);
    setShowSources(false);
    persistSession("");
  };

  const openRecentChat = async (chat) => {
    if (pollRef.current) clearTimeout(pollRef.current);
    setSidebarOpen(false);
    setTopic(chat.topic);
    setLatestResult(null);
    setDiff("");
    setError("");
    setStatus("");
    persistSession(chat.sessionId);
    await loadSession(chat.sessionId);
  };

  const saveApiKey = (event) => {
    event.preventDefault();
    try {
      if (apiKey.trim()) localStorage.setItem(STORAGE.apiKey, apiKey.trim());
      else localStorage.removeItem(STORAGE.apiKey);
    } catch {
      // Continue if browser storage is unavailable.
    }
    setShowSettings(false);
  };

  const copyReport = async () => {
    if (!latestResult?.report) return;
    await navigator.clipboard?.writeText(latestResult.report);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  const copyJson = async () => {
    if (!latestResult) return;
    const payload = latestResult.structured || {
      report_id: latestResult.job_id,
      topic: latestResult.topic,
      report: latestResult.report,
      citations: latestResult.citations || [],
    };
    await navigator.clipboard?.writeText(JSON.stringify(payload, null, 2));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  const downloadPdf = async () => {
    if (!latestResult?.job_id) return;
    try {
      const response = await fetch(API + "/result/" + latestResult.job_id + "/pdf", { headers: authHeaders() });
      if (!response.ok) throw new Error(await getErrorMessage(response, "PDF export failed."));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = latestResult.job_id + ".pdf";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (downloadError) {
      setError(downloadError.message || "PDF export failed.");
    }
  };

  const loadDiff = async () => {
    if (!latestResult?.topic) return;
    if (diff) {
      setDiff("");
      return;
    }
    try {
      const response = await fetch(API + "/diff/" + encodeURIComponent(latestResult.topic), { headers: authHeaders() });
      if (!response.ok) throw new Error(await getErrorMessage(response, "Unable to load report changes."));
      const data = await response.json();
      setDiff(data.diff || "No previous report found.");
    } catch (diffError) {
      setError(diffError.message || "Unable to load report changes.");
    }
  };

  const startVoiceInput = () => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setError("Voice input is not supported by this browser.");
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = navigator.language || "en-US";
    recognition.onresult = (event) => setTopic((current) => (current + " " + event.results[0][0].transcript).trim());
    recognition.onerror = () => setError("Voice input could not be started.");
    recognition.start();
  };

  const sources = useMemo(() => latestResult?.citations || latestResult?.structured?.citations || [], [latestResult]);
  const hasConversation = messages.length > 0 || latestResult;

  return (
    <div className="app-shell">
      <aside className={"sidebar " + (sidebarOpen ? "sidebar-open" : "")}>
        <div className="sidebar-heading">
          <button className="new-chat-button" onClick={newChat} type="button"><Icon name="edit" size={18} /><span>New chat</span></button>
          <button className="sidebar-close" onClick={() => setSidebarOpen(false)} type="button"><Icon name="close" size={20} /></button>
        </div>
        <div className="recent-heading"><span>Recent chats</span><span className="recent-count">{recentChats.length}</span></div>
        <div className="recent-list">
          {recentChats.length ? recentChats.map((chat) => (
            <button className={"recent-chat " + (chat.sessionId === sessionId ? "recent-chat-active" : "")} key={chat.id} onClick={() => openRecentChat(chat)} type="button">
              <span>{chat.topic}</span><small>{formatTime(chat.updatedAt)}</small>
            </button>
          )) : <p className="empty-recent">Your research conversations will appear here.</p>}
        </div>
        <div className="sidebar-footer">
          <button className="settings-button" onClick={() => setShowSettings(true)} type="button"><Icon name="settings" size={18} /><span>Connection settings</span></button>
          <div className="provider-note"><span className="provider-dot" /><span>Gemini primary · Groq fallback</span></div>
        </div>
      </aside>

      {sidebarOpen && <button className="sidebar-scrim" onClick={() => setSidebarOpen(false)} type="button" />}

      <section className="workspace">
        <header className="topbar">
          <button className="menu-button" onClick={() => setSidebarOpen(true)} type="button"><Icon name="menu" size={25} /></button>
          <div className="brand"><span className="brand-name">Agentic RAG</span><span className="brand-description">Web, Wiki, Document</span></div>
          <div className="online-status"><span className="online-dot" />Online</div>
        </header>

        <main className={"chat-canvas " + (hasConversation ? "chat-canvas-conversation" : "")}>
          {hasConversation ? (
            <div className="conversation">
              {messages.map((message, index) => (
                <article className={"message-row message-" + message.role} key={message.role + "-" + index}>
                  <div className="message-avatar">{message.role === "user" ? "You" : <Icon name="spark" size={16} />}</div>
                  <div className="message-content"><div className="message-label">{message.role === "user" ? "You" : "Agentic RAG"}</div><div className="message-text">{message.content}</div></div>
                </article>
              ))}
              {job && <div className="research-progress"><span className="progress-spinner" /><span>{status || "Researching…"}</span></div>}

              {latestResult?.report && (
                <article className="report-card">
                  <div className="report-card-heading">
                    <div><div className="message-label">Latest report</div><h2>{latestResult.topic}</h2></div>
                    <span className="complete-pill">Complete</span>
                  </div>
                  <div className="report-body">{latestResult.report}</div>
                  <div className="report-actions">
                    <button onClick={copyReport} type="button"><Icon name="copy" size={15} />{copied ? "Copied" : "Copy"}</button>
                    <button onClick={copyJson} type="button"><span className="json-mark">{"{}"}</span>Copy JSON</button>
                    <button onClick={downloadPdf} type="button"><Icon name="download" size={15} />PDF</button>
                    <button onClick={loadDiff} type="button">{diff ? "Hide changes" : "Show changes"}</button>
                    {!!sources.length && <button onClick={() => setShowSources((visible) => !visible)} type="button">{showSources ? "Hide sources" : sources.length + " sources"}</button>}
                  </div>
                  {showSources && <div className="sources-list">{sources.map((source, index) => <a href={source.url} key={source.url + "-" + index} rel="noreferrer" target="_blank"><span>[S{index + 1}]</span>{source.title || source.url}</a>)}</div>}
                  {diff && <pre className="diff-box">{diff}</pre>}
                </article>
              )}
            </div>
          ) : (
            <div className="hero">
              <div className="hero-eyebrow">Hello, Explorer!</div>
              <h1>How Can I Help<br />You Today?</h1>
              <p>Ask a question and I’ll research the web, Wikipedia, and your documents.</p>
            </div>
          )}

          <div className="composer-wrap">
            {(status || error) && <div className={"composer-status " + (error ? "composer-error" : "")}>{error || status}</div>}
            <form className="composer" onSubmit={submitResearch}>
              <textarea aria-label="Research question" disabled={!!job} onChange={(event) => setTopic(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitResearch(event); } }} placeholder="Ask anything…" ref={composerRef} rows={1} value={topic} />
              <div className="composer-tools">
                <label className="format-picker"><span>Format</span><select aria-label="Output format" onChange={(event) => setFormat(event.target.value)} value={format}><option value="text">Text</option><option value="json">JSON</option><option value="pdf">PDF</option></select><Icon name="chevron" size={14} /></label>
                <button aria-label="Voice input" className="icon-button" onClick={startVoiceInput} type="button"><Icon name="mic" size={18} /></button>
                <button aria-label="Send research question" className="send-button" disabled={!topic.trim() || !!job} type="submit"><Icon name="send" size={17} /></button>
              </div>
            </form>
            <p className="composer-hint">Agentic RAG can make mistakes. Check important sources before acting.</p>
          </div>
        </main>
      </section>

      {showSettings && (
        <div className="modal-backdrop" onMouseDown={() => setShowSettings(false)}>
          <form className="settings-modal" onMouseDown={(event) => event.stopPropagation()} onSubmit={saveApiKey}>
            <div className="settings-modal-heading"><div><div className="message-label">Private connection</div><h2>API settings</h2></div><button className="modal-close" onClick={() => setShowSettings(false)} type="button"><Icon name="close" size={19} /></button></div>
            <p>Store the research API key only in this browser. Provider keys stay on the server.</p>
            <label className="settings-label" htmlFor="api-key">X-API-Key</label>
            <input autoComplete="off" id="api-key" onChange={(event) => setApiKey(event.target.value)} placeholder="Optional for local development" type="password" value={apiKey} />
            <div className="settings-modal-actions"><button className="secondary-button" onClick={() => setApiKey("")} type="button">Clear</button><button className="primary-button" type="submit">Save key</button></div>
          </form>
        </div>
      )}
    </div>
  );
}
