import { ReactNode } from "react";

/**
 * A small Markdown renderer for assistant answers.
 *
 * Deliberately hand-rolled rather than pulling in a parser: it covers what the
 * assistant actually emits — GFM pipe tables, lists, headings and emphasis —
 * and the repo already renders rich text this way (see `Rich` in the type
 * builder). It also keeps the frontend container's node_modules volume out of
 * the loop, which has bitten this setup before.
 *
 * Everything is built as React elements, never `dangerouslySetInnerHTML`.
 * Answer text is derived from document content, which is untrusted input, so
 * raw HTML in it must stay inert text rather than become markup.
 */

const TABLE_ROW = /^\s*\|(.+)\|\s*$/;
const TABLE_SEP = /^\s*\|[\s|:-]+\|\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
// One pass over the inline markers we care about. Non-greedy and anchored on
// matching delimiters, so an unmatched `*` stays literal instead of eating the
// rest of the line.
const INLINE = /(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_)/g;

function inline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(INLINE).filter(Boolean).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if ((part.startsWith("**") && part.endsWith("**")) || (part.startsWith("__") && part.endsWith("__")))
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1)
      return <code key={key}>{part.slice(1, -1)}</code>;
    if ((part.startsWith("*") && part.endsWith("*")) || (part.startsWith("_") && part.endsWith("_")))
      return part.length > 2 ? <em key={key}>{part.slice(1, -1)}</em> : <span key={key}>{part}</span>;
    return <span key={key}>{part}</span>;
  });
}

const cells = (row: string) =>
  (row.match(TABLE_ROW)?.[1] ?? "").split("|").map((c) => c.trim());

export function Markdown({ text }: { text: string }) {
  const lines = (text || "").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  let lastI = -1;
  while (i < lines.length) {
    // Belt and braces: this parser runs on half-finished text while an answer
    // streams, and a branch that consumes nothing would spin forever and hang
    // the tab. If an iteration made no progress, force it.
    if (i === lastI) i++;
    lastI = i;
    if (i >= lines.length) break;
    const line = lines[i];

    if (!line.trim()) {
      i++;
      continue;
    }

    // --- Table: a header row followed by the |---|---| rule ---
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = cells(line);
      const body: string[][] = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i]) && !TABLE_SEP.test(lines[i])) {
        body.push(cells(lines[i]));
        i++;
      }
      blocks.push(
        // Wrapped so a wide table scrolls inside the bubble instead of
        // stretching it past the conversation column.
        <div className="md-table-wrap" key={`t${key++}`}>
          <table>
            <thead>
              <tr>{head.map((h, n) => <th key={n}>{inline(h, `h${n}`)}</th>)}</tr>
            </thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r}>
                  {head.map((_, c) => <td key={c}>{inline(row[c] ?? "", `c${r}-${c}`)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      continue;
    }

    // --- Heading ---
    const h = line.match(HEADING);
    if (h) {
      const level = Math.min(h[1].length, 6);
      const Tag = `h${level === 1 ? 4 : Math.min(level + 3, 6)}` as "h4" | "h5" | "h6";
      blocks.push(<Tag className="md-h" key={`h${key++}`}>{inline(h[2], "hd")}</Tag>);
      i++;
      continue;
    }

    // --- List (a run of bullets or numbers) ---
    if (BULLET.test(line) || NUMBERED.test(line)) {
      const ordered = NUMBERED.test(line) && !BULLET.test(line);
      const items: string[] = [];
      while (i < lines.length && (BULLET.test(lines[i]) || NUMBERED.test(lines[i]))) {
        const m = lines[i].match(BULLET) || lines[i].match(NUMBERED);
        items.push(m?.[1] ?? "");
        i++;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List className="md-list" key={`l${key++}`}>
          {items.map((it, n) => <li key={n}>{inline(it, `li${n}`)}</li>)}
        </List>
      );
      continue;
    }

    // --- Paragraph: consecutive plain lines, kept as soft line breaks ---
    // Always consume the current line first. While an answer streams in, a
    // table's header row arrives before its |---| rule, so it reaches here
    // still matching TABLE_ROW; a loop that only consumed lines failing every
    // block test would consume nothing and spin forever.
    const para: string[] = [lines[i]];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !TABLE_ROW.test(lines[i]) &&
      !HEADING.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !NUMBERED.test(lines[i])
    ) {
      para.push(lines[i]);
      i++;
    }
    blocks.push(
      <p className="md-p" key={`p${key++}`}>
        {para.map((l, n) => (
          <span key={n}>
            {n > 0 && <br />}
            {inline(l, `p${n}`)}
          </span>
        ))}
      </p>
    );
  }

  return <div className="md">{blocks}</div>;
}
