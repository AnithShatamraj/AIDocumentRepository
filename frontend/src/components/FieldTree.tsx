import { useState } from "react";

export interface FieldNode {
  id: string;
  field_key: string;
  field_path: string;
  field_name: string;
  node_kind: "scalar" | "object" | "list";
  data_type: string;
  ordinal: number | null;
  is_discovered: boolean;
  raw_value: string | null;
  value_number: number | null;
  value_date: string | null;
  confidence: number;
  review_status: string;
  source_page: number | null;
  source_bbox: any;
  children: FieldNode[];
}

interface Props {
  nodes: FieldNode[];
  depth?: number;
  isPdf: boolean;
  reviewing: Record<string, boolean>;
  onJump: (n: FieldNode) => void;
  onReview: (id: string, action: "accept" | "reject") => void;
}

/** Recursive renderer: scalars are rows, objects are indented groups, lists are
 * numbered item cards. Every leaf keeps click-to-highlight and accept/reject at
 * any depth. */
export function FieldTree({ nodes, depth = 0, isPdf, reviewing, onJump, onReview }: Props) {
  return (
    <>
      {nodes.map((n) =>
        n.node_kind === "scalar" ? (
          <ScalarField
            key={n.id}
            node={n}
            depth={depth}
            isPdf={isPdf}
            busy={!!reviewing[n.id]}
            onJump={onJump}
            onReview={onReview}
          />
        ) : (
          <ContainerField
            key={n.id}
            node={n}
            depth={depth}
            isPdf={isPdf}
            reviewing={reviewing}
            onJump={onJump}
            onReview={onReview}
          />
        )
      )}
    </>
  );
}

function ScalarField({
  node, depth, isPdf, busy, onJump, onReview,
}: {
  node: FieldNode; depth: number; isPdf: boolean; busy: boolean;
  onJump: (n: FieldNode) => void; onReview: (id: string, a: "accept" | "reject") => void;
}) {
  const clickable = isPdf && (node.source_bbox?.page || node.source_page);
  return (
    <div
      className={`field-row${clickable ? " meta-field" : ""}`}
      style={{ marginLeft: depth * 10 }}
      onClick={() => clickable && onJump(node)}
      title={clickable ? "Click to highlight source in the document" : undefined}
    >
      <div className="spread">
        <strong style={{ fontSize: 13 }}>
          {node.field_name}
          {node.is_discovered && <span className="pill entity">entity</span>}
        </strong>
        <ReviewStatus status={node.review_status} />
      </div>
      <div className="spread">
        <span>{node.raw_value || <span className="muted">—</span>}</span>
        <span className="muted conf">{Math.round((node.confidence || 0) * 100)}%</span>
      </div>
      <div className="spread" style={{ alignItems: "center" }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {clickable
            ? `p.${node.source_bbox?.page || node.source_page}${
                node.source_bbox?.rects?.length ? " · region located" : ""
              }`
            : ""}
        </span>
        {node.review_status === "pending_review" && (
          <span className="row" style={{ gap: 6 }} onClick={(e) => e.stopPropagation()}>
            <button className="ghost approve" disabled={busy} onClick={() => onReview(node.id, "accept")}>
              ✓ Accept
            </button>
            <button className="ghost reject" disabled={busy} onClick={() => onReview(node.id, "reject")}>
              ✕ Reject
            </button>
          </span>
        )}
      </div>
    </div>
  );
}

function ContainerField({
  node, depth, isPdf, reviewing, onJump, onReview,
}: {
  node: FieldNode; depth: number; isPdf: boolean; reviewing: Record<string, boolean>;
  onJump: (n: FieldNode) => void; onReview: (id: string, a: "accept" | "reject") => void;
}) {
  const [open, setOpen] = useState(true);
  const [asTable, setAsTable] = useState(false);
  const isList = node.node_kind === "list";
  // A list of objects renders nicely as a compact grid.
  const objectItems = isList && node.children.every((c) => c.node_kind === "object");
  const columns = objectItems && node.children.length
    ? node.children[0].children.map((c) => ({ key: c.field_key, name: c.field_name }))
    : [];

  // Item headline: first non-empty scalar inside the item.
  const headline = (item: FieldNode) =>
    item.children.find((c) => c.node_kind === "scalar" && c.raw_value)?.raw_value || "";

  return (
    <div className="field-group" style={{ marginLeft: depth * 10 }}>
      <div className="field-group-head" onClick={() => setOpen((o) => !o)}>
        <span className="chev">{open ? "▼" : "▶"}</span>
        <strong style={{ fontSize: 13 }}>{node.field_name}</strong>
        <span className="pill">{isList ? `${node.children.length}` : "object"}</span>
        {objectItems && node.children.length > 1 && (
          <button
            className="ghost"
            style={{ marginLeft: "auto", padding: "2px 8px", fontSize: 11 }}
            onClick={(e) => { e.stopPropagation(); setAsTable((t) => !t); }}
            title="Toggle table view"
          >
            {asTable ? "☰ List" : "▦ Table"}
          </button>
        )}
      </div>

      {open && node.children.length === 0 && (
        <div className="muted" style={{ fontSize: 12, marginLeft: 18, paddingBottom: 6 }}>
          None found.
        </div>
      )}

      {open && asTable && objectItems && (
        <div style={{ overflowX: "auto", marginLeft: 12 }}>
          <table className="field-table">
            <thead>
              <tr>
                <th>#</th>
                {columns.map((c) => <th key={c.key}>{c.name}</th>)}
              </tr>
            </thead>
            <tbody>
              {node.children.map((item, i) => (
                <tr key={item.id}>
                  <td className="muted">{i + 1}</td>
                  {columns.map((c) => {
                    const cell = item.children.find((x) => x.field_key === c.key);
                    return (
                      <td
                        key={c.key}
                        className={cell && isPdf && (cell.source_bbox?.page || cell.source_page) ? "meta-field" : ""}
                        onClick={() => cell && onJump(cell)}
                      >
                        {cell?.raw_value || <span className="muted">—</span>}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && !asTable && (
        <div className="field-group-body">
          {isList && objectItems
            ? node.children.map((item, i) => (
                <div className="list-item" key={item.id}>
                  <div className="list-item-head">
                    <span className="idx">#{i + 1}</span>
                    <span className="muted">{headline(item)}</span>
                  </div>
                  <FieldTree
                    nodes={item.children}
                    depth={depth + 1}
                    isPdf={isPdf}
                    reviewing={reviewing}
                    onJump={onJump}
                    onReview={onReview}
                  />
                </div>
              ))
            : (
              <FieldTree
                nodes={node.children}
                depth={depth + 1}
                isPdf={isPdf}
                reviewing={reviewing}
                onJump={onJump}
                onReview={onReview}
              />
            )}
        </div>
      )}
    </div>
  );
}

function ReviewStatus({ status }: { status: string }) {
  const map: Record<string, string> = {
    auto_accepted: "ok", verified: "ok", corrected: "ok",
    pending_review: "muted", rejected: "err",
  };
  return (
    <span className={map[status] || "muted"} style={{ fontSize: 12 }}>
      {status.replace("_", " ")}
    </span>
  );
}
