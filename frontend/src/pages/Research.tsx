import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { streamPost } from "../api/client";

interface Msg {
  role: "user" | "assistant";
  content: string;
  citations?: any[];
}

export function Research() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const convId = useRef<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  function scroll() {
    setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 30);
  }

  async function ask(e: React.FormEvent) {
    e.preventDefault();
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
    }
  }

  return (
    <div>
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
        <form className="row" onSubmit={ask} style={{ marginTop: 12 }}>
          <input placeholder="Ask about your documents…" value={input} onChange={(e) => setInput(e.target.value)} />
          <button disabled={busy}>{busy ? "Thinking…" : "Ask"}</button>
        </form>
      </div>
    </div>
  );
}
