import { useEffect, useState } from "react";
import { api, isError } from "../api";
import { LIBRARY } from "../messages";

// Remembered per browser: once dismissed, the notice stays away (it only ever says what the Library tag says too).
export const SAMPLE_NOTICE_KEY = "lore.sampleNotice.dismissed";
const dismissed = () => { try { return localStorage.getItem(SAMPLE_NOTICE_KEY) === "1"; } catch { return false; } };

// On arrival, while the library holds the sample book LORE gave a new account: says it is there to explore and how
// to add one's own. `refreshKey` re-reads the library (a book added or removed); `onAdd` opens Add book.
export function SampleNotice({ refreshKey = 0, onAdd }: { refreshKey?: number; onAdd: () => void }) {
  const [title, setTitle] = useState<string | null>(null);
  const [hidden, setHidden] = useState(dismissed);
  useEffect(() => {
    if (hidden) return;
    let live = true;
    api.books().then((r) => {
      if (live && !isError(r)) setTitle(r.books.find((b) => b.is_sample)?.title ?? null);
    });
    return () => { live = false; };
  }, [refreshKey, hidden]);
  if (hidden || !title) return null;
  const close = () => {
    try { localStorage.setItem(SAMPLE_NOTICE_KEY, "1"); } catch { /* private mode: hidden for this visit only */ }
    setHidden(true);
  };
  return (
    <section className="sample-notice" role="status" aria-label={LIBRARY.sampleNoticeLabel}>
      <p>{LIBRARY.sampleNotice(title)} {LIBRARY.sampleNoticeAdd}</p>
      <div className="sample-notice-actions">
        <button className="tbtn primary" type="button" onClick={() => { onAdd(); close(); }}>{LIBRARY.sampleNoticeAddButton}</button>
        <button className="tbtn" type="button" onClick={close}>{LIBRARY.sampleNoticeDismiss}</button>
      </div>
    </section>
  );
}
