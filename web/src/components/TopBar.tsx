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
      <span className="mark">LORE<small>Reader</small></span>
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
