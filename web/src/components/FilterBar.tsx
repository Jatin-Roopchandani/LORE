import { useId, useState } from "react";
import { activeFilters, toggled, type Filters } from "../filters";
import { EXPLORE, UI } from "../messages";

const KINDS = ["Character", "Place", "Faction", "Object", "Concept", "Event"];

/** Kind and book chips: a pressed chip is shown, an unpressed one hides its nodes and the edges to them. */
export function FilterBar({ filters, onChange, books, hiddenCount }:
  { filters: Filters; onChange: (f: Filters) => void; books: { id: string; title: string }[]; hiddenCount: number }) {
  const [open, setOpen] = useState(false);
  const uid = useId();
  const n = activeFilters(filters);
  const kindVar = new Map<string, string>(UI.legendKinds.map(([token, label]) => [label, token]));
  return (
    <>
      <button className="tbtn" type="button" aria-expanded={open} aria-controls={`${uid}-filters`} onClick={() => setOpen(!open)}>
        {n > 0 ? EXPLORE.filtersActive(n) : EXPLORE.filters}
      </button>
      {hiddenCount > 0 && <span className="hint" role="status">{EXPLORE.filteredOut(hiddenCount)}</span>}
      <div id={`${uid}-filters`} className="filters" role="group" aria-label={EXPLORE.filtersLabel} hidden={!open}>
        <div role="group" aria-label={EXPLORE.kindsLabel} className="chips">
          <span className="chip-label" aria-hidden="true">{EXPLORE.kindsLabel}</span>
          {KINDS.map((k) => (
            <button key={k} className="chip" type="button" aria-pressed={!filters.kinds.has(k)} onClick={() => onChange(toggled(filters, "kinds", k))}>
              <i style={{ background: `var(${kindVar.get(EXPLORE.kindLabel[k]) ?? "--k-other"})` }} />{EXPLORE.kindLabel[k] ?? k}
            </button>
          ))}
        </div>
        {books.length > 1 && (
          <div role="group" aria-label={EXPLORE.booksLabel} className="chips">
            <span className="chip-label" aria-hidden="true">{EXPLORE.booksLabel}</span>
            {books.map((b) => (
              <button key={b.id} className="chip" type="button" aria-pressed={!filters.books.has(b.id)} onClick={() => onChange(toggled(filters, "books", b.id))}>{b.title}</button>
            ))}
          </div>
        )}
        {n > 0 && <button className="btn" type="button" onClick={() => onChange({ kinds: new Set(), books: new Set() })}>{EXPLORE.resetFilters}</button>}
      </div>
    </>
  );
}
