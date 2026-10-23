import { useState } from "react";

/** A date filter is either a preset ("last 7 days", resolved against the clock
 *  when rows are tested, so it stays correct as time passes) or an explicit
 *  range picked on the calendar. Dates are `yyyy-mm-dd` calendar days in the
 *  viewer's own time zone. An empty value means no restriction. */
export interface DateRangeValue {
  preset: string;
  from: string;
  to: string;
}

export const NO_DATE_RANGE: DateRangeValue = { preset: "", from: "", to: "" };

export const DATE_PRESETS: { key: string; label: string; start: (now: Date) => Date }[] = [
  { key: "24h", label: "Last 24 hours", start: (n) => new Date(n.getTime() - 86_400_000) },
  { key: "7d", label: "Last 7 days", start: (n) => new Date(n.getTime() - 7 * 86_400_000) },
  { key: "1m", label: "Last month", start: (n) => new Date(n.getFullYear(), n.getMonth() - 1, n.getDate(), n.getHours(), n.getMinutes()) },
  { key: "3m", label: "Last 3 months", start: (n) => new Date(n.getFullYear(), n.getMonth() - 3, n.getDate(), n.getHours(), n.getMinutes()) },
  { key: "1y", label: "Last year", start: (n) => new Date(n.getFullYear() - 1, n.getMonth(), n.getDate(), n.getHours(), n.getMinutes()) },
];

const pad = (n: number) => String(n).padStart(2, "0");
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = (s: string) => new Date(`${s}T00:00:00`);

export const dateRangeActive = (v: DateRangeValue) => !!(v.preset || v.from || v.to);

/** Millisecond bounds for testing a timestamp; either end may be open. */
export function dateRangeBounds(v: DateRangeValue, now = new Date()): { start?: number; end?: number } {
  if (v.preset) {
    const p = DATE_PRESETS.find((x) => x.key === v.preset);
    return p ? { start: p.start(now).getTime() } : {};
  }
  return {
    start: v.from ? parse(v.from).getTime() : undefined,
    // The whole of the `to` day counts.
    end: v.to ? new Date(`${v.to}T23:59:59.999`).getTime() : undefined,
  };
}

const fmt = (s: string) =>
  parse(s).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

export function describeDateRange(v: DateRangeValue): string {
  if (v.preset) return DATE_PRESETS.find((p) => p.key === v.preset)?.label ?? "";
  if (v.from && v.to) return v.from === v.to ? fmt(v.from) : `${fmt(v.from)} – ${fmt(v.to)}`;
  if (v.from) return `Since ${fmt(v.from)}`;
  if (v.to) return `Until ${fmt(v.to)}`;
  return "Any time";
}

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];

function Calendar({ value, onPick }: { value: DateRangeValue; onPick: (day: string) => void }) {
  // Open on the month being filtered, or the current one.
  const [view, setView] = useState(() => {
    const base = value.from ? parse(value.from) : value.to ? parse(value.to) : new Date();
    return new Date(base.getFullYear(), base.getMonth(), 1);
  });
  const y = view.getFullYear();
  const m = view.getMonth();
  const lead = view.getDay();
  const count = new Date(y, m + 1, 0).getDate();
  const cells: (string | null)[] = [
    ...Array.from({ length: lead }, () => null),
    ...Array.from({ length: count }, (_, i) => iso(new Date(y, m, i + 1))),
  ];
  const today = iso(new Date());
  // While only the start is chosen, a lone `from` is the whole selection.
  const lo = value.preset ? "" : value.from;
  const hi = value.preset ? "" : value.to;

  return (
    <div className="cal">
      <div className="cal-head">
        <button type="button" className="ghost tiny" aria-label="Previous month"
                onClick={() => setView(new Date(y, m - 1, 1))}>‹</button>
        <strong>{view.toLocaleDateString(undefined, { month: "long", year: "numeric" })}</strong>
        <button type="button" className="ghost tiny" aria-label="Next month"
                onClick={() => setView(new Date(y, m + 1, 1))}>›</button>
      </div>
      <div className="cal-grid">
        {WEEKDAYS.map((w, i) => <div key={i} className="cal-dow">{w}</div>)}
        {cells.map((day, i) =>
          day === null ? <div key={i} /> : (
            <button
              key={i}
              type="button"
              className={
                "cal-day" +
                (day === today ? " today" : "") +
                (lo && hi && day > lo && day < hi ? " in-range" : "") +
                (day === lo || day === hi ? " edge" : "")
              }
              onClick={() => onPick(day)}
            >
              {Number(day.slice(8))}
            </button>
          ))}
      </div>
    </div>
  );
}

/** Presets plus a calendar. Click a day to start a range, then a second day to
 *  finish it (picking an earlier day second just swaps them). */
export function DateRangePicker({ value, onChange }: { value: DateRangeValue; onChange: (v: DateRangeValue) => void }) {
  function pick(day: string) {
    const open = value.from && !value.to && !value.preset;
    if (!open) return onChange({ preset: "", from: day, to: "" });
    onChange(day < value.from ? { preset: "", from: day, to: value.from } : { preset: "", from: value.from, to: day });
  }

  return (
    <div>
      <div className="date-presets">
        {DATE_PRESETS.map((p) => (
          <button
            key={p.key}
            type="button"
            className={`ghost tiny${value.preset === p.key ? " on" : ""}`}
            onClick={() => onChange(value.preset === p.key ? NO_DATE_RANGE : { preset: p.key, from: "", to: "" })}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="date-summary">{describeDateRange(value)}</div>
      <Calendar value={value} onPick={pick} />
      <div className="muted tiny-text" style={{ marginTop: 6 }}>
        {value.from && !value.to && !value.preset ? "Pick a second day to finish the range." : "Pick a start day, then an end day."}
      </div>
    </div>
  );
}
