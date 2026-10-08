import { useEffect, useState } from "react";
import { api, isError } from "../api";
import { applyTheme, isDark } from "../theme";
import type { Book, Quota } from "../types";
import { AddBookDialog } from "./AddBookDialog";
import { BooksDropdown } from "./BooksDropdown";
import { QuotaChip } from "./QuotaChip";
import { SettingsMenu } from "./SettingsMenu";

export function TopBar({ books, hiddenBooks, onBooksChange, showInternals, refreshKey }:
  { books: Book[]; hiddenBooks: Set<string>; onBooksChange: (s: Set<string>) => void; showInternals: boolean; refreshKey: number }) {
  const [adding, setAdding] = useState(false);
  const [quota, setQuota] = useState<Quota | null>(null);
  useEffect(() => { api.quota().then((q) => { if (!isError(q)) setQuota(q); }); }, [refreshKey]);
  return (
    <header>
      <span className="mark">LORE<small>Reader</small></span>
      <span className="spacer" />
      <div className="tools">
        <BooksDropdown books={books} hiddenBooks={hiddenBooks} onChange={onBooksChange} />
        <button className="tbtn primary" type="button" onClick={() => setAdding(true)}>+ Add book</button>
        <QuotaChip quota={quota} showInternals={showInternals} />
        <button className="tbtn icon" type="button" aria-label="Switch light or dark" onClick={() => applyTheme(isDark() ? "light" : "dark")}>◐</button>
        <SettingsMenu showInternals={showInternals} />
      </div>
      <AddBookDialog open={adding} onClose={() => setAdding(false)} />
    </header>
  );
}
