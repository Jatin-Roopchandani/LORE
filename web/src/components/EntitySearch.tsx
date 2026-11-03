import { useEffect, useId, useState } from "react";
import { api, isError } from "../api";
import { EXPLORE } from "../messages";
import type { SearchHit } from "../types";

const DELAY_MS = 150;

/** A search box with autocomplete over the world's entity names and aliases (/api/search).
 *  Arrow keys move through the results, Enter picks one, Escape closes the list. */
export function EntitySearch({ world, label, placeholder, onPick, exclude, autoFocus = false, clearOnPick = true }:
  { world?: string; label: string; placeholder: string; onPick: (hit: SearchHit) => void; exclude?: string;
    autoFocus?: boolean; clearOnPick?: boolean }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [state, setState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const uid = useId();
  const listId = `${uid}-list`;
  const term = q.trim();
  useEffect(() => {
    if (!term) { setHits([]); setState("idle"); setActive(-1); return; }
    let live = true;
    setState("loading");
    const timer = setTimeout(() => {
      api.search(term, world).then((r) => {
        if (!live) return;
        if (isError(r)) { setHits([]); setState("error"); return; }
        setHits(r.results.filter((h) => h.id !== exclude));
        setState("done"); setActive(-1);
      });
    }, DELAY_MS);
    return () => { live = false; clearTimeout(timer); };
  }, [term, world, exclude]);
  const pick = (hit: SearchHit) => {
    onPick(hit);
    setOpen(false); setActive(-1);
    if (clearOnPick) { setQ(""); setHits([]); setState("idle"); } else setQ(hit.name);
  };
  const showList = open && hits.length > 0;
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!hits.length) return;
      e.preventDefault();
      setOpen(true);
      setActive((i) => (e.key === "ArrowDown" ? (i + 1) % hits.length : (i <= 0 ? hits.length - 1 : i - 1)));
    } else if (e.key === "Enter") {
      if (showList) { e.preventDefault(); pick(hits[active >= 0 ? active : 0]); }
    } else if (e.key === "Escape" && open) {
      e.stopPropagation();
      setOpen(false);
    }
  };
  return (
    <div className="esearch">
      <input type="search" role="combobox" aria-label={label} placeholder={placeholder} autoComplete="off" autoFocus={autoFocus}
        aria-autocomplete="list" aria-expanded={showList} aria-controls={listId}
        aria-activedescendant={showList && active >= 0 ? `${uid}-opt-${active}` : undefined}
        value={q} onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)} onKeyDown={onKeyDown} />
      <ul id={listId} role="listbox" aria-label={EXPLORE.searchResults} className="esearch-list" hidden={!showList}>
        {hits.map((h, i) => (
          <li key={h.id} id={`${uid}-opt-${i}`} role="option" aria-selected={i === active} className={i === active ? "on" : undefined}
            onMouseDown={(e) => e.preventDefault()} onClick={() => pick(h)}>
            <span className="name">{h.name}</span>
            <span className="mono">{[EXPLORE.kindLabel[h.kind] ?? h.kind, h.world?.name,
              h.matched_alias ? EXPLORE.matchedAlias(h.matched_alias) : null].filter(Boolean).join(" · ")}</span>
          </li>
        ))}
      </ul>
      <p className="hint" role="status">
        {state === "loading" ? EXPLORE.searching : state === "done" && !hits.length && open ? EXPLORE.noMatches
          : state === "error" ? EXPLORE.failed : ""}
      </p>
    </div>
  );
}
