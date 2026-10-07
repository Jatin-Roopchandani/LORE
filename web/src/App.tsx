import { useEffect, useState } from "react";
import { api, isError } from "./api";
import { ChatPane } from "./components/ChatPane";
import { GraphPane } from "./components/GraphPane";
import { TopBar } from "./components/TopBar";
import { applyTheme, loadTheme } from "./theme";
import type { Book } from "./types";
import { useAsk } from "./useAsk";

export default function App() {
  const ask = useAsk();
  const [books, setBooks] = useState<Book[]>([]);
  const [showInternals, setShowInternals] = useState(true);
  const [hiddenBooks, setHiddenBooks] = useState<Set<string>>(new Set());
  const [hoverChunk, setHoverChunk] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [resetsAt, setResetsAt] = useState<string | undefined>();
  useEffect(() => { applyTheme(loadTheme()); }, []);
  useEffect(() => {
    api.settings().then((s) => { if (!isError(s)) setShowInternals(s.show_internals); });
    api.library().then((l) => { if (!isError(l)) setBooks(l.books); });
  }, []);
  useEffect(() => {
    if (ask.state.status !== "done" && ask.state.status !== "error") return;
    setRefreshKey((k) => k + 1);
    api.quota().then((q) => { if (!isError(q)) setResetsAt(q.resets_at); });
  }, [ask.state.status]);
  const graphed = books.filter((b) => b.status === "graphed");
  const noBooks = graphed.length > 0 && graphed.every((b) => hiddenBooks.has(b.id));
  return (
    <div className="app">
      <TopBar books={books} hiddenBooks={hiddenBooks} onBooksChange={setHiddenBooks} showInternals={showInternals} refreshKey={refreshKey} />
      <div className="panes">
        <ChatPane ask={ask} showInternals={showInternals} resetsAt={resetsAt} onHoverChunk={setHoverChunk} />
        <GraphPane state={ask.state} hiddenBooks={hiddenBooks} hoverChunk={hoverChunk} noBooks={noBooks} />
      </div>
    </div>
  );
}
