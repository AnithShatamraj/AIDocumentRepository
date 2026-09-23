import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export interface TagSuggestion {
  id: string;
  name: string;
  document_count: number;
}

/** Shared suggestion list. The server only returns tags the caller can actually
 *  see documents under, so this never reveals a tag name from a closed corner
 *  of the tenant. */
export function useTagSuggestions() {
  return useQuery<TagSuggestion[]>({
    queryKey: ["tags"],
    queryFn: () => api.get("/api/tags"),
    staleTime: 30_000,
  });
}

const clean = (s: string) => s.trim().replace(/\s+/g, " ");

/**
 * Pills plus a free-text box. Controlled: `value` is a list of tag *names*, and
 * every edit calls `onChange` with the next list — the caller decides whether
 * that means a save or just staging a bulk action.
 */
export function TagEditor({
  value,
  onChange,
  disabled,
  placeholder = "Add a tag…",
  autoFocus,
}: {
  value: string[];
  onChange: (names: string[]) => void;
  disabled?: boolean;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const { data: suggestions } = useTagSuggestions();

  const unused = useMemo(() => {
    const taken = new Set(value.map((v) => v.toLowerCase()));
    return (suggestions || []).filter((s) => !taken.has(s.name.toLowerCase()));
  }, [suggestions, value]);

  function add(raw: string) {
    const name = clean(raw);
    setDraft("");
    if (!name) return;
    // Case-insensitive, matching the server's uniqueness rule — so typing
    // "urgent" when "Urgent" is already on the document is a no-op, not a
    // second pill that vanishes on save.
    if (value.some((v) => v.toLowerCase() === name.toLowerCase())) return;
    onChange([...value, name]);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add(draft);
    } else if (e.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1));
    }
  }

  const listId = "tag-suggestions";

  return (
    <div className={`tag-editor${disabled ? " disabled" : ""}`}>
      {value.map((name) => (
        <span key={name} className="pill tag">
          {name}
          {!disabled && (
            <button
              type="button"
              className="tag-x"
              title={`Remove ${name}`}
              onClick={() => onChange(value.filter((v) => v !== name))}
            >
              ×
            </button>
          )}
        </span>
      ))}
      {!disabled && (
        <>
          <input
            className="tag-input"
            list={listId}
            value={draft}
            autoFocus={autoFocus}
            placeholder={value.length ? "" : placeholder}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            onBlur={() => add(draft)}
          />
          <datalist id={listId}>
            {unused.map((s) => (
              <option key={s.id} value={s.name}>
                {s.document_count} document{s.document_count === 1 ? "" : "s"}
              </option>
            ))}
          </datalist>
        </>
      )}
    </div>
  );
}
