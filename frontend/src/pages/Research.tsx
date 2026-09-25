import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { streamPost } from "../api/client";

interface Msg {
  role: "user" | "assistant";
  content: string;
  citations?: any[];
}

// Roughly six lines before the composer starts scrolling internally, so a long
// question never grows the box until it pushes the conversation off screen.
const COMPOSER_MAX_PX = 160;

export function Research() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const convId = useRef<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);

  function scroll() {
    setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 30);
  }

  // Grow the textarea to fit its content, up to a cap. Reset to "auto" first or
  // scrollHeight keeps reporting the previous (taller) size and the box only
  // ever grows.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_PX)}px`;
  }, [input]);

  async function send() {
    if (!input.trim() || busy) return;
    const question = input.trim();
    setInput("");
    setMessages((m) => [...m, { role: "user", content: question }, { role: "assistant", content: "" }]);
    setBusy(true);
    scroll();

    try {
      await streamPost(
        "/api/chat",
        { question, conversation_id: convId.current },
        (event, data) => {
          if (event === "start") convId.current = data.conversation_id;
          if (event === "delta") {
            setMessages((m) => {
              const copy = [...m];
              copy[copy.length - 1] = {
                ...copy[copy.length - 1],
                content: copy[copy.length - 1].content + (data.text || ""),
              };
              return copy;
            });
            scroll();
          }
          if (event === "meta" || event === "done") {
            if (data.citations) {
              setMessages((m) => {
                const copy = [...m];
                copy[copy.length - 1] = { ...copy[copy.length - 1], citations: data.citations };
                return copy;
              });
            }
          }
        }
      );
    } catch (err: any) {
      setMessages((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "assistant", content: `Error: ${err.message}` };
        return copy;
      });
    } finally {
      setBusy(false);
      scroll();
      boxRef.current?.focus();
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter starts a new line. The isComposing guard keeps
    // an IME's "commit this candidate" Enter from sending a half-typed question.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <div className="research-page">
      <div className="page-head">
        <h1>AI Research Assistant</h1>
        <div className="muted">Grounded answers with citations, over documents you're authorized to read.</div>
      </div>

      <div className="chat">
        <div className="messages">
          {messages.length === 0 && (
            <div className="muted">Ask a question like “What are the payment terms across our contracts?”</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.content || <span className="muted">…</span>}
              {m.citations && m.citations.length > 0 && (
                <div className="citations">
                  {m.citations.map((c: any) => (
                    <Link key={c.index} to={`/documents/${c.document_id}`} className="cite">
                      [{c.index}] {c.document_name}{c.page ? ` p.${c.page}` : ""}
                    </Link>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={bottomRef} />
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
  );
}
