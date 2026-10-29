import { useEffect, useState } from "react";
import { api, isError } from "../api";
import { LIBRARY } from "../messages";
import { applyTheme, isDark } from "../theme";
import type { Book, Quota, World } from "../types";
import { AddBookDialog } from "./AddBookDialog";
import { LibraryPanel } from "./LibraryPanel";
import { BooksDropdown } from "./BooksDropdown";
import { QuotaChip } from "./QuotaChip";
import { WorldSelector } from "./WorldSelector";
import { SettingsMenu } from "./SettingsMenu";

export function TopBar({ books, hiddenBooks, onBooksChange, showInternals, refreshKey, worlds = [], world = "all", onWorldChange = () => {}, onLibraryChange }:
  { books: Book[]; hiddenBooks: Set<string>; onBooksChange: (s: Set<string>) => void; showInternals: boolean; refreshKey: number;
    worlds?: World[]; world?: string; onWorldChange?: (w: string) => void; onLibraryChange?: () => void }) {
  const [adding, setAdding] = useState(false);
  const [library, setLibrary] = useState(false);
  const [quota, setQuota] = useState<Quota | null>(null);
  useEffect(() => { api.quota().then((q) => { if (!isError(q)) setQuota(q); }); }, [refreshKey]);
  return (
    <header>
      <span className="mark"><LoreMark />LORE<small>Reader</small></span>
      <span className="spacer" />
      <div className="tools">
        <WorldSelector worlds={worlds} world={world} onChange={onWorldChange} />
        <BooksDropdown books={books} hiddenBooks={hiddenBooks} onChange={onBooksChange} onOpenLibrary={() => setLibrary(true)} />
        <button className="tbtn" type="button" onClick={() => setLibrary(true)}>{LIBRARY.open}</button>
        <button className="tbtn primary" type="button" onClick={() => setAdding(true)}>+ Add book</button>
        <QuotaChip quota={quota} showInternals={showInternals} />
        <button className="tbtn icon" type="button" aria-label="Switch light or dark" onClick={() => applyTheme(isDark() ? "light" : "dark")}>◐</button>
        <SettingsMenu showInternals={showInternals} />
      </div>
      <LibraryPanel open={library} onClose={() => setLibrary(false)} showInternals={showInternals} onChange={onLibraryChange} />
      <AddBookDialog open={adding} onClose={() => setAdding(false)} worlds={worlds} />
    </header>
  );
}

// The LORE mark: an open book whose spine fans out into a small graph, one node
// glowing. Colours come from the theme, so it follows light and dark mode.
function LoreMark() {
  return (
    <svg className="mark-icon" viewBox="0 0 64 64" aria-hidden="true">
      <path d="M32 56 C24 51 14 50 6 52 V30 C14 28 24 29 32 34 C40 29 50 28 58 30 V52 C50 50 40 51 32 56 Z" fill="none" stroke="var(--accent)" strokeWidth="3.4" strokeLinejoin="round" />
      <path d="M32 34 V56" stroke="var(--accent)" strokeWidth="3.4" />
      <path d="M32 30 L16 14 M32 30 L32 8 M32 30 L48 14 M16 14 L32 8 L48 14" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="32" cy="30" r="3.2" fill="var(--accent)" />
      <circle cx="16" cy="14" r="3.4" fill="var(--accent)" />
      <circle cx="32" cy="8" r="3.8" fill="var(--glow)" />
      <circle cx="48" cy="14" r="3.4" fill="var(--accent)" />
    </svg>
  );
}
