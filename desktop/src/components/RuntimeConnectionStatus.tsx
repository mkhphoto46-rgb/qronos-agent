import type { RuntimeConnectionView } from "../runtimeConnection";
import "./RuntimeConnectionStatus.css";

/** The local runtime's connection state, in fixed Persian text; retry only when the controller offers it. */
export default function RuntimeConnectionStatus({ view, onRetry }: {
  view: RuntimeConnectionView;
  onRetry: () => void;
}) {
  const problem = view.phase === "failed" || view.phase === "degraded";
  return <aside className={`runtime-connection runtime-connection-${view.phase}`} dir="rtl">
    <div role={problem ? "alert" : "status"} aria-live={problem ? "assertive" : "polite"} className="runtime-connection-text">
      <span className="runtime-connection-dot" aria-hidden="true" />
      <strong>{view.label}</strong>
      {view.detail && <span className="runtime-connection-detail">{view.detail}</span>}
    </div>
    {view.canRetry && <button type="button" disabled={view.busy} onClick={onRetry}>تلاش دوباره</button>}
  </aside>;
}
