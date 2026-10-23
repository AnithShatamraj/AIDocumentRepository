import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { UploadDialog } from "../components/UploadDialog";
import { TagEditor, useTagSuggestions } from "../components/TagEditor";
import { SearchPanel } from "./Search";
import { CheckList, FilterPopover } from "../components/ColumnFilter";
import {
  DateRangePicker, DateRangeValue, NO_DATE_RANGE, dateRangeActive, dateRangeBounds,
} from "../components/DateRangeFilter";

type NameOp = "contains" | "not_contains" | "equals" | "starts" | "ends";
const NAME_OPS: { value: NameOp; label: string }[] = [
  { value: "contains", label: "contains" },
  { value: "not_contains", label: "does not contain" },
  { value: "equals", label: "equals" },
  { value: "starts", label: "starts with" },
  { value: "ends", label: "ends with" },
];

interface Filters {
  name: { op: NameOp; value: string };
  tags: { mode: "any" | "all"; values: string[] };
  types: string[];
  statuses: string[];
  versions: string[];
  modified: DateRangeValue;
}
const NO_FILTERS: Filters = {
  name: { op: "contains", value: "" },
  tags: { mode: "any", values: [] },
  types: [], statuses: [], versions: [], modified: NO_DATE_RANGE,
};

// Filter key for documents with no document type (e.g. still being classified).const UNCLASSIFIED = "__unclassified__";
const distinct = (xs: (string | undefined)[]) =>
  [...new Set(xs.filter((x): x is string => !!x))].sort();

// Filter key for documents with no document type (e.g. still being classified).
const UNCLASSIFIED = "__unclassified__";

const nameActive = (f: Filters) => f.name.value.trim() !== "";

function matchesName(name: string, { op, value }: Filters["name"]): boolean {
  const hay = name.toLowerCase();
  const needle = value.trim().toLowerCase();
  if (!needle) return true;
  switch (op) {
    case "contains": return hay.includes(needle);
    case "not_contains": return !hay.includes(needle);
    case "equals": return hay === needle;
    case "starts": return hay.startsWith(needle);
    case "ends": return hay.endsWith(needle);
  }
}

function matches(d: any, f: Filters): boolean {
  if (!matchesName(d.name || "", f.name)) return false;

  if (f.tags.values.length) {
    const have = new Set((d.tags || []).map((t: any) => t.name));
    const test = (t: string) => have.has(t);
    if (!(f.tags.mode === "all" ? f.tags.values.every(test) : f.tags.values.some(test))) return false;
  }
  if (f.types.length) {
    const names: string[] = (d.document_types || []).map((t: any) => t.name);
    const keys = names.length ? names : [UNCLASSIFIED];
    if (!keys.some((k) => f.types.includes(k))) return false;
  }
  if (f.statuses.length && !f.statuses.includes(d.processing_status)) return false;
  if (f.versions.length && !f.versions.includes(String(d.current_version))) return false;

  if (dateRangeActive(f.modified)) {
    const { start, end } = dateRangeBounds(f.modified);
    const t = new Date(d.updated_at).getTime();
    if (start !== undefined && t < start) return false;
    if (end !== undefined && t > end) return false;
  }
  return true;
}

export function Documents() {
  const qc = useQueryClient();
  const [searchOpen, setSearchOpen] = useState(false);
  const [msg, setMsg] = useState<string>("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const [activeTags, setActiveTags] = useState<string[]>([]);
  const [match, setMatch] = useState<"all" | "any">("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkTags, setBulkTags] = useState<string[]>([]);
  const [bulkBusy, setBulkBusy] = useState(false);

  const { data: tagFacets } = useTagSuggestions();

  const path = useMemo(() => {
    const params = new URLSearchParams();
    activeTags.forEach((t) => params.append("tags", t));
    if (activeTags.length) params.set("match", match);
    const qs = params.toString();
    return `/api/documents${qs ? `?${qs}` : ""}`;
  }, [activeTags, match]);

  const { data, isLoading } = useQuery({
    queryKey: ["documents", activeTags, match],
    queryFn: () => api.get(path),
    refetchInterval: 5000,
  });

  function toggleTag(name: string) {
    setActiveTags((t) =>
      t.includes(name) ? t.filter((x) => x !== name) : [...t, name]
    );
  }

  function toggleRow(id: string) {
    setSelected((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  // Column filters. They narrow the rows already loaded (the newest 200); the
  // tag pills above still filter on the server. Choices offered in the
  // dropdowns come from those loaded rows, so none of them can match nothing.
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const patch = (p: Partial<Filters>) => setFilters((f) => ({ ...f, ...p }));
  const filtersActive =
    nameActive(filters) || filters.tags.values.length > 0 || filters.types.length > 0 ||
    filters.statuses.length > 0 || filters.versions.length > 0 || dateRangeActive(filters.modified);

  const asOptions = (xs: string[], label: (x: string) => string = (x) => x) =>
    xs.map((x) => ({ value: x, label: label(x) }));
  const tagOptions = useMemo(
    () => asOptions(distinct((data || []).flatMap((d: any) => (d.tags || []).map((t: any) => t.name)))), [data]);
  const typeOptions = useMemo(() => {
    const names = distinct((data || []).flatMap((d: any) => (d.document_types || []).map((t: any) => t.name)));
    const opts = asOptions(names);
    const anyUnclassified = (data || []).some((d: any) => !(d.document_types || []).length);
    return anyUnclassified ? [...opts, { value: UNCLASSIFIED, label: "Unclassified" }] : opts;
  }, [data]);
  const statusOptions = useMemo(
    () => asOptions(distinct((data || []).map((d: any) => d.processing_status)), (s) => s.replace(/_/g, " ")), [data]);
  const versionOptions = useMemo(
    () => distinct((data || []).map((d: any) => String(d.current_version)))
      .sort((a, b) => Number(a) - Number(b)).map((v) => ({ value: v, label: `v${v}` })), [data]);
  const rows: any[] = useMemo(() => (data || []).filter((d: any) => matches(d, filters)), [data, filters]);

  const visibleIds: string[] = rows.map((d: any) => d.id);
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.has(id));

  async function bulkTag(action: "add" | "remove") {
    if (!bulkTags.length || !selected.size) return;
    setBulkBusy(true);
    setMsg("");
    try {
      const res = await api.post("/api/documents/bulk-tag", {
        document_ids: [...selected],
        add: action === "add" ? bulkTags : [],
        remove: action === "remove" ? bulkTags : [],
      });
      // `skipped` is documents in the selection the user can't edit — a normal
      // outcome when a selection spans a permission boundary, so it's reported
      // rather than treated as an error.
      setMsg(
        `${action === "add" ? "Tagged" : "Untagged"} ${res.updated} document(s).` +
          (res.skipped ? ` ${res.skipped} skipped — you can't edit them.` : "")
      );
      setBulkTags([]);
      setSelected(new Set());
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["documents"] }),
        qc.invalidateQueries({ queryKey: ["tags"] }),
      ]);
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBulkBusy(false);
    }
  }
  return (
    <div>
      <div className="page-head spread">
        <div>
          <h1>Documents</h1>
          <div className="muted">Most recently modified first. Upload triggers the AI pipeline automatically.</div>
        </div>
        <div className="row">
          <button
            className={searchOpen ? "" : "secondary"}
            aria-expanded={searchOpen}
            onClick={() => setSearchOpen((o) => !o)}
            title="Search document content and extracted fields"
          >
            Search
          </button>
          <button onClick={() => setDialogOpen(true)}>Upload document(s)</button>
        </div>
      </div>

      {searchOpen && (
        <div className="card search-panel">
          <div className="spread" style={{ marginBottom: 12 }}>
            <strong>Search documents</strong>
            <button className="ghost tiny" onClick={() => setSearchOpen(false)}>✕ Close</button>
          </div>
          <SearchPanel />
        </div>
      )}

      {msg && <div className="card" style={{ marginBottom: 16 }}>{msg}</div>}

      {dialogOpen && (
        <UploadDialog
          onClose={() => setDialogOpen(false)}
          onUploaded={() => qc.invalidateQueries({ queryKey: ["documents"] })}
        />
      )}

      {(tagFacets || []).length > 0 && (
        <div className="facets">
          {(tagFacets || []).map((t) => (
            <button
              key={t.id}
              type="button"
              className={`pill tag-facet${activeTags.includes(t.name) ? " on" : ""}`}
              onClick={() => toggleTag(t.name)}
            >
              {t.name} <span className="muted">{t.document_count}</span>
            </button>
          ))}
          {activeTags.length > 1 && (
            <button type="button" className="ghost tiny"
                    title="Whether a document must carry every selected tag or just one"
                    onClick={() => setMatch((m) => (m === "all" ? "any" : "all"))}>
              match: {match}
            </button>
          )}
          {activeTags.length > 0 && (
            <button type="button" className="ghost tiny" onClick={() => setActiveTags([])}>
              Clear tags
            </button>
          )}
        </div>
      )}

      {selected.size > 0 && (
        <div className="card bulk-bar">
          <strong>{selected.size} selected</strong>
          <TagEditor value={bulkTags} onChange={setBulkTags} placeholder="Tag to apply…" />
          <button disabled={bulkBusy || !bulkTags.length} onClick={() => bulkTag("add")}>
            Add to selected
          </button>
          <button className="secondary" disabled={bulkBusy || !bulkTags.length}
                  onClick={() => bulkTag("remove")}>
            Remove from selected
          </button>
          <button className="ghost" onClick={() => setSelected(new Set())}>Clear selection</button>
        </div>
      )}

      {filtersActive && (
        <div className="filter-summary muted">
          Showing {rows.length} of {(data || []).length} documents
          <button type="button" className="ghost tiny" onClick={() => setFilters(NO_FILTERS)}>Clear all filters</button>
        </div>
      )}

      <div className="card">
        <table className="doc-table">
          <thead>
            <tr>
              <th className="pick">
                <input
                  type="checkbox"
                  checked={allSelected}
                  title={allSelected ? "Deselect all" : "Select all"}
                  onChange={() =>
                    setSelected(allSelected ? new Set() : new Set(visibleIds))
                  }
                />
              </th>

              <th>
                <span className="th-inner">
                  Name
                  <FilterPopover label="Name" active={nameActive(filters)}
                                 onClear={() => patch({ name: { ...filters.name, value: "" } })}>
                    <select value={filters.name.op} aria-label="Name condition"
                            onChange={(e) => patch({ name: { ...filters.name, op: e.target.value as NameOp } })}>
                      {NAME_OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                    <input autoFocus placeholder="Text…" aria-label="Name text" value={filters.name.value}
                           style={{ marginTop: 8 }}
                           onChange={(e) => patch({ name: { ...filters.name, value: e.target.value } })} />
                  </FilterPopover>
                </span>
              </th>

              <th>
                <span className="th-inner">
                  Tags
                  <FilterPopover label="Tags" active={filters.tags.values.length > 0}
                                 onClear={() => patch({ tags: { ...filters.tags, values: [] } })}>
                    <select value={filters.tags.mode} aria-label="Tag match"
                            onChange={(e) => patch({ tags: { ...filters.tags, mode: e.target.value as "any" | "all" } })}>
                      <option value="any">has any of</option>
                      <option value="all">has all of</option>
                    </select>
                    <div style={{ marginTop: 8 }}>
                      <CheckList options={tagOptions} value={filters.tags.values} empty="No tags on these documents."
                                 onChange={(values) => patch({ tags: { ...filters.tags, values } })} />
                    </div>
                  </FilterPopover>
                </span>
              </th>

              <th>
                <span className="th-inner">
                  Document Type
                  <FilterPopover label="Document Type" active={filters.types.length > 0}
                                 onClear={() => patch({ types: [] })}>
                    <CheckList options={typeOptions} value={filters.types}
                               onChange={(types) => patch({ types })} />
                  </FilterPopover>
                </span>
              </th>

              <th>
                <span className="th-inner">
                  Status
                  <FilterPopover label="Status" active={filters.statuses.length > 0}
                                 onClear={() => patch({ statuses: [] })}>
                    <CheckList options={statusOptions} value={filters.statuses}
                               onChange={(statuses) => patch({ statuses })} />
                  </FilterPopover>
                </span>
              </th>

              <th>
                <span className="th-inner">
                  Ver.
                  <FilterPopover label="Version" active={filters.versions.length > 0}
                                 onClear={() => patch({ versions: [] })}>
                    <CheckList options={versionOptions} value={filters.versions}
                               onChange={(versions) => patch({ versions })} />
                  </FilterPopover>
                </span>
              </th>

              <th>
                <span className="th-inner">
                  Last modified
                  <FilterPopover label="Last modified" align="right" wide
                                 active={dateRangeActive(filters.modified)}
                                 onClear={() => patch({ modified: NO_DATE_RANGE })}>
                    <DateRangePicker value={filters.modified} onChange={(modified) => patch({ modified })} />
                  </FilterPopover>
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {isLoading && <tr><td className="muted">Loading…</td></tr>}
            {rows.map((d: any) => (
              <tr key={d.id} className={selected.has(d.id) ? "picked" : ""}>
                <td className="pick">
                  <input type="checkbox" checked={selected.has(d.id)} onChange={() => toggleRow(d.id)} />
                </td>
                <td><Link to={`/documents/${d.id}`}>{d.name}</Link></td>
                <td>
                  {(d.tags || []).map((t: any) => (
                    <span key={t.id} className="pill tag">{t.name}</span>
                  ))}
                </td>
                <td>
                  {(d.document_types || []).length
                    ? d.document_types.map((t: any) => <span key={t.id} className="pill">{t.name}</span>)
                    : <span className="muted">—</span>}
                </td>
                <td><StatusBadge status={d.processing_status} /></td>
                <td className="muted">v{d.current_version}</td>
                <td className="muted">{new Date(d.updated_at).toLocaleString()}</td>
              </tr>
            ))}
            {data && rows.length === 0 && (
              <tr><td colSpan={7} className="muted">
                {data.length === 0 && !activeTags.length
                  ? "No documents yet — upload to begin."
                  : "No documents match these filters."}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
