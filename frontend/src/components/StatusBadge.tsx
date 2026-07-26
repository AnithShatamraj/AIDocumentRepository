const LABELS: Record<string, string> = {
  pending: "Pending",
  processing: "Processing",
  processed: "Processed",
  needs_review: "Needs Review",
  failed: "Failed",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${status}`}>{LABELS[status] || status}</span>;
}
