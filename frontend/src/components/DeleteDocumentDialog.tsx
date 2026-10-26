import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

interface Props {
  docs: { id: string; name: string }[];
  onClose: () => void;
  onDeleted: (result: { deleted: number; skipped: number }) => void;
}

// No 0/O, 1/I/L: a code someone has to read and retype shouldn't contain look-alikes.
const ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
const LENGTH = 20;
const GROUP = 5;

function newCode(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(LENGTH));
  return Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join("");
}

/** Confirms deleting one or more documents. A fresh random code is shown each time and
 * must be typed into the boxes (pasting is blocked), so the delete can't happen by a
 * stray click or by muscle memory, without making anyone retype a long file name. The
 * dashes between the groups are fixed; only the characters are typed. */
export function DeleteDocumentDialog({ docs, onClose, onDeleted }: Props) {
  const [code] = useState(newCode);
  const [typed, setTyped] = useState<string[]>(() => Array(LENGTH).fill(""));
  const boxes = useRef<(HTMLInputElement | null)[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const matches = typed.join("") === code;
  const shown = code.match(/.{1,5}/g)!.join("-"); // grouped, so it is easy to read and keep your place

  const focus = (i: number) => boxes.current[Math.max(0, Math.min(LENGTH - 1, i))]?.focus();
  const setChar = (i: number, ch: string) => setTyped((t) => t.map((c, j) => (j === i ? ch : c)));

  function onType(i: number, value: string) {
    if (value.length > 2) {
      // Several characters arrived at once (an on-screen keyboard or autofill, not a paste): spread them over the boxes.
      const chars = [...value.toUpperCase()].filter((c) => ALPHABET.includes(c)).slice(0, LENGTH - i);
      setTyped((t) => t.map((c, j) => (j >= i && j - i < chars.length ? chars[j - i] : c)));
      return focus(i + chars.length);
    }
    const ch = value.slice(-1).toUpperCase(); // typing over a filled box replaces it
    if (!ch) return setChar(i, "");
    if (!ALPHABET.includes(ch)) return; // the code never contains it, so ignore it
    setChar(i, ch);
    focus(i + 1);
  }

  function onKey(i: number, e: KeyboardEvent) {
    if (e.key === "Enter") return void confirm();
    if (e.key === "ArrowLeft") return focus(i - 1);
    if (e.key === "ArrowRight") return focus(i + 1);
    if (e.key === "Backspace" && !typed[i] && i > 0) { // empty box: step back and clear the previous one
      e.preventDefault();
      setChar(i - 1, "");
      focus(i - 1);
    }
  }

  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [busy, onClose]);

  async function confirm() {
    if (!matches || busy) return;
    setBusy(true);
    setError("");
    try {
      onDeleted(await api.post("/api/documents/bulk-delete", { document_ids: docs.map((d) => d.id) }));
    } catch (e: any) {
      setError(e.message || String(e));
      setBusy(false);
    }
  }

  const many = docs.length > 1;
  return (
    <div className="modal-overlay" onClick={() => !busy && onClose()}>
      <div className="modal-card" style={{ maxWidth: 600 }} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0 }}>Delete {many ? `${docs.length} documents` : "this document"}?</h3>
        {many ? (
          <ul className="delete-list">
            {docs.slice(0, 6).map((d) => <li key={d.id}>{d.name}</li>)}
            {docs.length > 6 && <li className="muted">…and {docs.length - 6} more</li>}
          </ul>
        ) : (
          <p style={{ margin: "0 0 8px" }}><strong style={{ wordBreak: "break-all" }}>{docs[0].name}</strong></p>
        )}
        <p>
          {many ? "They" : "It"} will be removed from the document list, search and Research, together with
          {many ? " their" : " its"} summary and extracted data. Other people who had access will lose
          {many ? " them" : " it"} too.
        </p>
        <p className="muted" style={{ fontSize: 13 }}>
          Files are kept for 30 days, so an administrator can still restore {many ? "them" : "it"}; after that
          they are gone.
        </p>
        <label style={{ display: "block", fontSize: 13, marginBottom: 6 }}>
          To confirm, type this code into the boxes:
        </label>
        <div className="delete-code" aria-label="Confirmation code">{shown}</div>
        <div className="code-boxes" role="group" aria-label="Type the confirmation code">
          {Array.from({ length: LENGTH / GROUP }, (_, g) => (
            <span key={g} className="code-group">
              {g > 0 && <span className="code-dash" aria-hidden>-</span>}
              {Array.from({ length: GROUP }, (_, k) => {
                const i = g * GROUP + k;
                return (
                  <input key={i} ref={(el) => { boxes.current[i] = el; }} className="code-box" value={typed[i]}
                         autoFocus={i === 0} autoComplete="off" spellCheck={false}
                         aria-label={`Character ${i + 1} of ${LENGTH}`}
                         onFocus={(e) => e.target.select()} onPaste={(e) => e.preventDefault()}
                         onChange={(e) => onType(i, e.target.value)} onKeyDown={(e) => onKey(i, e)} />
                );
              })}
            </span>
          ))}
        </div>
        {error && <div className="tag-error" style={{ marginTop: 8 }}>Couldn't delete: {error}</div>}
        <div className="row" style={{ justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          <button className="ghost" disabled={busy} onClick={onClose}>Cancel</button>
          <button className="ghost danger" disabled={!matches || busy} onClick={confirm}>
            {busy ? "Deleting…" : many ? `Delete ${docs.length} documents` : "Delete document"}
          </button>
        </div>
      </div>
    </div>
  );
}
