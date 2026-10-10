import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, streamPost } from "../api/client";
import { Markdown } from "../components/Markdown";

interface Citation {
  index: number;
  document_id: string;
  document_name: string;
  chunk_id: string;
  page?: number | null;
  section_title?: string | null;
  bbox?: { page: number; rects: any[]; page_width?: number; page_height?: number } | null;
  snippet?: string;
}

interface Coverage {
  documents_searched?: number;
  documents_cited?: number;
  passages_used?: number;
  rewritten_query?: string | null;
}

interface Check {
  checked?: boolean;
  source?: string;
  unsupported?: { sentence: string; why: string }[];
}

interface Msg {
  id?: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  citations_redacted?: number;
  coverage?: Coverage | null;
  check?: Check | null;
}

interface ConvSummary {
  id: string;
  title: string;
  updated_at: string;
}

const COMPOSER_MAX_PX = 160;

/** A citation opens the document at the block it came from, not page 1. The
 *  stored bbox is already the viewer's highlight shape; page-only is the
 *  fallback when the parser captured no coordinates (non-PDF formats). */
function highlightFor(c: Citation) {
  if (c.bbox && Array.isArray(c.bbox.rects) && c.bbox.rects.length) return c.bbox;
  if (c.page) return { page: c.page, rects: [] };
  return null;
}

export function Research() {
  const qc = useQueryClient();
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [convId, setConvId] = useState<string | null>(null);
  const [histQuery, setHistQuery] = useState("");
  const [feedbackFor, setFeedbackFor] = useState<string | null>(null);
  const [feedbackText, setFeedbackText] = useState("");
  const [voted, setVoted] = useState<Record<string, string>>({});

  const bottomRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const lastQuestion = useRef<string>("");

  const { data: conversations } = useQuery<ConvSummary[]>({
    queryKey: ["conversations", histQuery],
    queryFn: () => api.get(`/api/conversations${histQuery ? `?q=${encodeURIComponent(histQuery)}` : ""}`),
  });

  function scroll() {
    setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 30);
  }

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_PX)}px`;
  }, [input]);

  /** Rehydrate from the server. Streamed messages have no id until they're
   *  persisted, and feedback needs one — so after a turn completes we reload
   *  rather than guess. This is also what re-applies permission redaction. */
  async function loadConversation(id: string) {
    const data = await api.get(`/api/conversations/${id}`);
    setConvId(data.id);
    setMessages(
      (data.messages || []).map((m: any) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        citations: m.citations,
        citations_redacted: m.citations_redacted,
        coverage: m.meta?.coverage ?? null,
        check: m.meta?.check ?? null,
      }))
    );
    scroll();
  }

  function newConversation() {
    abortRef.current?.abort();
    setConvId(null);
    setMessages([]);
    setInput("");
    boxRef.current?.focus();
  }

  async function renameConversation(c: ConvSummary) {
    const title = window.prompt("Rename conversation", c.title);
    if (!title || title.trim() === c.title) return;
    await api.patch(`/api/conversations/${c.id}`, { title: title.trim() });
    qc.invalidateQueries({ queryKey: ["conversations"] });
  }

  async function deleteConversation(c: ConvSummary) {
    if (!window.confirm(`Delete “${c.title}”? This cannot be undone.`)) return;
    await api.del(`/api/conversations/${c.id}`);
    if (c.id === convId) newConversation();
    qc.invalidateQueries({ queryKey: ["conversations"] });
  }

  async function rate(messageId: string, vote: "up" | "down", reason?: string) {
    await api.post(`/api/messages/${messageId}/feedback`, { vote, reason: reason || null });
    setVoted((v) => ({ ...v, [messageId]: vote }));
    setFeedbackFor(null);
    setFeedbackText("");
  }

  async function run(question: string) {
    lastQuestion.current = question;
    setMessages((m) => [...m, { role: "user", content: question }, { role: "assistant", content: "" }]);
    setBusy(true);
    scroll();

    const controller = new AbortController();
    abortRef.current = controller;
    let startedConv = convId;

    const patchLast = (patch: Partial<Msg>) =>
      setMessages((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { ...copy[copy.length - 1], ...patch };
        return copy;
      });

    try {
      await streamPost(
        "/api/chat",
        { question, conversation_id: convId },
        (event, data) => {
          if (event === "start") {
            startedConv = data.conversation_id;
            setConvId(data.conversation_id);
          }
          if (event === "delta") {
            setMessages((m) => {
              const copy = [...m];
              const last = copy[copy.length - 1];
              copy[copy.length - 1] = { ...last, content: last.content + (data.text || "") };
              return copy;
            });
            scroll();
          }
          if (event === "meta") patchLast({ citations: data.citations, coverage: data.coverage });
          if (event === "check") patchLast({ check: data });
          if (event === "done") {
            patchLast({
              citations: data.citations ?? undefined,
              coverage: data.coverage ?? undefined,
              check: data.check ?? undefined,
            });
          }
        },
        controller.signal
      );
      // Pull the persisted turn back so messages carry their ids (feedback
      // needs them) and their stored caveats.
      if (startedConv) await loadConversation(startedConv);
      qc.invalidateQueries({ queryKey: ["conversations"] });
    } catch (err: any) {
      if (controller.signal.aborted) {
        patchLast({ content: (messages[messages.length - 1]?.content || "") + "\n\n_Stopped._" });
      } else {
        patchLast({ role: "assistant", content: `Error: ${err.message}` });
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
      scroll();
    }
  }

  async function send() {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    await run(question);
  }

  function stop() {
    abortRef.current?.abort();
  }

  async function regenerate() {
    if (busy || !lastQuestion.current) return;
    await run(lastQuestion.current);
  }

  function editLastQuestion() {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    setInput(lastUser.content);
    boxRef.current?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  const lastAssistant = messages.length && messages[messages.length - 1].role === "assistant";

  return (
    <div className="research-page">
      <div className="page-head">
        <h1>AI Research Assistant</h1>
        <div className="muted">Grounded answers with citations, over documents you're authorized to read.</div>
      </div>

      <div className="research-body">
        <aside className="history">
          <button className="secondary block" onClick={newConversation}>New conversation</button>
          <input
            className="hist-search"
            placeholder="Search history…"
            value={histQuery}
            onChange={(e) => setHistQuery(e.target.value)}
          />
          <div className="hist-list">
            {(conversations || []).map((c) => (
              <div key={c.id} className={`hist-item${c.id === convId ? " on" : ""}`}>
                <button className="hist-title" onClick={() => loadConversation(c.id)} title={c.title}>
                  {c.title}
                </button>
                <button className="hist-act" title="Rename" onClick={() => renameConversation(c)}>✎</button>
                <button className="hist-act" title="Delete" onClick={() => deleteConversation(c)}>✕</button>
              </div>
            ))}
            {conversations && conversations.length === 0 && (
              <div className="muted tiny-note">{histQuery ? "No matches." : "No conversations yet."}</div>
            )}
          </div>
        </aside>

        <div className="chat">
          <div className="messages">
            {messages.length === 0 && (
              <div className="muted">Ask a question like “What are the payment terms across our contracts?”</div>
            )}
            {messages.map((m, i) => (
              <div key={m.id || i} className={`msg ${m.role}`}>
                {/* Assistant answers are Markdown (comparisons come back as
                    tables); the user's own text stays literal so nothing they
                    typed is reinterpreted as formatting. */}
                {m.content ? (
                  m.role === "assistant" ? <Markdown text={m.content} /> : m.content
                ) : (
                  <span className="muted">…</span>
                )}

                {m.role === "assistant" && m.check?.unsupported && m.check.unsupported.length > 0 && (
                  <div className="unsupported">
                    <strong>Not supported by the cited sources</strong>
                    {m.check.unsupported.map((u, j) => (
                      <div key={j} className="unsupported-row">
                        “{u.sentence}”{u.why ? <span className="muted"> — {u.why}</span> : null}
                      </div>
                    ))}
                  </div>
                )}
                {m.role === "assistant" && m.check && m.check.checked === false && m.content && (
                  <div className="muted tiny-note">
                    Citations were not machine-checked for this answer.
                  </div>
                )}

                {m.citations && m.citations.length > 0 && (
                  <div className="citations">
                    {m.citations.map((c) => (
                      <Link
                        key={c.index}
                        to={`/documents/${c.document_id}`}
                        state={{ highlight: highlightFor(c) }}
                        className="cite"
                        title={c.snippet || ""}
                      >
                        [{c.index}] {c.document_name}
                        {c.page ? ` p.${c.page}` : ""}
                      </Link>
                    ))}
                  </div>
                )}

                {m.role === "assistant" && (m.coverage || m.citations_redacted) ? (
                  <div className="coverage muted">
                    {m.coverage?.documents_searched != null && (
                      <>
                        Searched {m.coverage.documents_searched} document
                        {m.coverage.documents_searched === 1 ? "" : "s"} ·{" "}
                        {m.coverage.documents_cited ?? 0} cited · {m.coverage.passages_used ?? 0} passage
                        {(m.coverage.passages_used ?? 0) === 1 ? "" : "s"}
                      </>
                    )}
                    {m.coverage?.rewritten_query && (
                      <div>Searched for: “{m.coverage.rewritten_query}”</div>
                    )}
                    {m.citations_redacted ? (
                      <div>
                        {m.citations_redacted} citation{m.citations_redacted === 1 ? "" : "s"} hidden —
                        you no longer have access to the source.
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {m.role === "assistant" && m.id && m.content && (
                  <div className="rate">
                    <button
                      className={`ghost tiny${voted[m.id] === "up" ? " on" : ""}`}
                      title="This answer was useful"
                      onClick={() => rate(m.id!, "up")}
                    >
                      ▲
                    </button>
                    <button
                      className={`ghost tiny${voted[m.id] === "down" ? " on" : ""}`}
                      title="Something was wrong"
                      onClick={() => setFeedbackFor(feedbackFor === m.id ? null : m.id!)}
                    >
                      ▼
                    </button>
                    {feedbackFor === m.id && (
                      <span className="rate-reason">
                        <input
                          autoFocus
                          placeholder="What was wrong?"
                          value={feedbackText}
                          onChange={(e) => setFeedbackText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") rate(m.id!, "down", feedbackText);
                          }}
                        />
                        <button className="tiny" onClick={() => rate(m.id!, "down", feedbackText)}>Send</button>
                      </span>
                    )}
                  </div>
                )}
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          <div className="turn-actions">
            {busy && (
              <button className="ghost tiny" onClick={stop}>Stop</button>
            )}
            {!busy && lastAssistant && (
              <>
                <button className="ghost tiny" onClick={regenerate}>Regenerate</button>
                <button className="ghost tiny" onClick={editLastQuestion}>Edit question</button>
              </>
            )}
          </div>

          <form
            className="chat-composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            {/* Placeholder stays short: a longer hint wraps to a second line and
                gets clipped by the one-row starting height at narrow widths, so
                the Shift+Enter hint lives in the tooltip instead. */}
            <textarea
              ref={boxRef}
              rows={1}
              placeholder="Ask about your documents…"
              title="Enter to send, Shift+Enter for a new line"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
            />
            <button disabled={busy || !input.trim()}>{busy ? "Thinking…" : "Ask"}</button>
          </form>
        </div>
      </div>
    </div>
  );
}
