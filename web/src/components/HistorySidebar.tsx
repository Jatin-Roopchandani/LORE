import { useEffect, useMemo, useRef, useState } from "react";
import type { HistoryItem } from "../history";
import { filterHistory, groupHistory, isDeep, timeLabel } from "../historyView";
import { HISTORY } from "../messages";
import type { World } from "../types";

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

/** Earlier questions, grouped by day, searchable. A column beside the panes, or (`drawer`) a modal drawer
 *  that traps focus, closes on Escape and gives focus back to what opened it. */
export function HistorySidebar({ items, current, worlds = [], drawer = false, now, onReplay, onDelete, onClear, onNew, onClose }:
  { items: HistoryItem[]; current: string | null; worlds?: World[]; drawer?: boolean; now?: Date;
    onReplay: (id: string) => void; onDelete: (id: string) => void; onClear: () => void; onNew: () => void; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [confirming, setConfirming] = useState(false);
  const root = useRef<HTMLElement>(null);
  const shown = useMemo(() => filterHistory(items, query), [items, query]);
  const groups = useMemo(() => groupHistory(shown, now ?? new Date()), [shown, now]);
  const worldName = (w?: string) => (!w ? null : w === "all" ? HISTORY.allWorlds : worlds.find((x) => String(x.id) === w)?.name ?? null);
  useEffect(() => { if (items.length === 0) setConfirming(false); }, [items.length]);
  useEffect(() => {
    if (!drawer) return;
    const opener = document.activeElement as HTMLElement | null;
    root.current?.querySelector<HTMLElement>("button")?.focus();
    return () => opener?.focus?.();
  }, [drawer]);
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!drawer) return;
    if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
    if (e.key !== "Tab") return;
    const nodes = [...(root.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])].filter((n) => !n.closest("[hidden]"));
    if (!nodes.length) return;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  const body = (
    <aside ref={root} id="history-panel" className={drawer ? "hist drawer" : "hist"} aria-label={HISTORY.title}
      {...(drawer ? { role: "dialog", "aria-modal": true } : {})} onKeyDown={onKeyDown}>
      <div className="hist-head">
        <h2>{HISTORY.title}</h2>
        {drawer && <button className="tbtn icon" type="button" aria-label={HISTORY.close} onClick={onClose}>{"×"}</button>}
      </div>
      <button className="btn-new" type="button" title={HISTORY.newQuestionHint} onClick={onNew}>{HISTORY.newQuestion}</button>
      {items.length > 0 && (
        <input type="search" className="hist-search" aria-label={HISTORY.search} placeholder={HISTORY.searchPlaceholder} autoComplete="off"
          value={query} onChange={(e) => setQuery(e.target.value)} />
      )}
      <div className="hist-list">
        {items.length === 0 && <p className="hint">{HISTORY.empty}</p>}
        {items.length > 0 && groups.length === 0 && <p className="hint" role="status">{HISTORY.noMatch(query.trim())}</p>}
        {groups.map((g) => (
          <section key={g.key} aria-label={HISTORY.groups[g.key]}>
            <h3>{HISTORY.groups[g.key]}</h3>
            <ul>
              {g.items.map((h) => {
                const world = worldName(h.world);
                const metaId = `hist-meta-${h.id}`;
                return (
                  <li key={h.id} className={h.id === current ? "cur" : undefined}>
                    <button className="hist-item" type="button" title={h.question} aria-label={h.question} aria-describedby={metaId}
                      aria-current={h.id === current ? "true" : undefined} onClick={() => onReplay(h.id)}>
                      <span className="hist-q">{h.question}</span>
                      <span className="hist-meta" id={metaId}>
                        {timeLabel(h.askedAt, now ?? new Date()) && <span className="mono">{timeLabel(h.askedAt, now ?? new Date())}</span>}
                        {world && <span className="hist-badge">{world}</span>}
                        {isDeep(h) && <span className="hist-badge deep" title={HISTORY.deepHint}>{HISTORY.deep}</span>}
                      </span>
                    </button>
                    <button className="hist-del" type="button" aria-label={HISTORY.delete(h.question)} title={HISTORY.delete(h.question)} onClick={() => onDelete(h.id)}>{HISTORY.deleteShort}</button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
      {items.length > 0 && (
        <div className="hist-foot">
          {confirming ? (
            <div role="group" aria-label={HISTORY.clearConfirmLabel} className="hist-confirm">
              <p>{HISTORY.clearConfirm(items.length)}</p>
              <div className="row-end">
                <button className="tbtn" type="button" onClick={() => setConfirming(false)}>{HISTORY.clearNo}</button>
                <button className="tbtn danger" type="button" autoFocus onClick={() => { setConfirming(false); onClear(); }}>{HISTORY.clearYes}</button>
              </div>
            </div>
          ) : <button className="linkbtn" type="button" onClick={() => setConfirming(true)}>{HISTORY.clear}</button>}
        </div>
      )}
    </aside>
  );
  return drawer ? <div className="hist-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>{body}</div> : body;
}
