import { useState } from "react";
import { chatsApi, failed } from "../chatsApi";
import { REPORT } from "../messages";

// "Report this answer": a small link under an answer that opens a dialog with an optional note. The report quotes
// the question's request id, so an admin can open its trace.
export function ReportAnswer({ requestId }: { requestId: string }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  if (state === "sent") return <p className="report-done" role="status">{REPORT.thanks}</p>;
  const send = async () => {
    setState("sending");
    const r = await chatsApi.report(requestId, note);
    setState(failed(r) ? "failed" : "sent");
    if (!failed(r)) setOpen(false);
  };
  return (
    <div className="report">
      {!open && <button className="report-link" type="button" onClick={() => setOpen(true)}>{REPORT.link}</button>}
      {open && (
        <div className="report-dialog" role="dialog" aria-modal="false" aria-labelledby="report-title">
          <h3 id="report-title">{REPORT.title}</h3>
          <p>{REPORT.lead}</p>
          <label>{REPORT.noteLabel}
            <textarea value={note} maxLength={2000} rows={3} onChange={(e) => setNote(e.target.value)} />
          </label>
          {state === "failed" && <p className="note" role="alert">{REPORT.failed}</p>}
          <div className="key-actions">
            <button className="tbtn primary" type="button" disabled={state === "sending"} onClick={() => void send()}>
              {state === "sending" ? REPORT.sending : REPORT.send}</button>
            <button className="tbtn" type="button" onClick={() => { setOpen(false); setState("idle"); }}>{REPORT.cancel}</button>
          </div>
        </div>
      )}
    </div>
  );
}
