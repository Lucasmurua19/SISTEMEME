import { reasonLabel, STATUS_HELP, STATUS_LABEL } from "@/lib/labels";
import { Icon, STATUS_ICON } from "./Icons";

/** El estado nunca se comunica solo con color: ícono + etiqueta. */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge ${status}`} title={STATUS_HELP[status] ?? ""}>
      <Icon name={STATUS_ICON[status] ?? "minus"} />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function ReasonChip({ code }: { code: string }) {
  return (
    <span className="reason-chip" title={code}>
      {reasonLabel(code)}
    </span>
  );
}

export function ReasonChips({ reasons, max = 2 }: { reasons: { code: string }[]; max?: number }) {
  const unique = Array.from(new Set(reasons.map((r) => r.code)));
  if (unique.length === 0) return <span className="muted">—</span>;
  return (
    <span className="reasons">
      {unique.slice(0, max).map((c) => (
        <ReasonChip key={c} code={c} />
      ))}
      {unique.length > max && (
        <span className="reason-chip" title={unique.slice(max).join(", ")}>
          +{unique.length - max}
        </span>
      )}
    </span>
  );
}
