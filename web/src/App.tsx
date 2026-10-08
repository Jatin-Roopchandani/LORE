import { useEffect, useMemo, useState } from "react";
import { api, isError } from "./api";
import { ChatPane } from "./components/ChatPane";
import { GraphPane } from "./components/GraphPane";
import { TopBar } from "./components/TopBar";
import { applyTheme, loadTheme } from "./theme";
import type { Book, SubgraphData } from "./types";
import { useAsk } from "./useAsk";

export default function App() {
  const ask = useAsk();
  const [books, setBooks] = useState<Book[]>([]);
  const [showInternals, setShowInternals] = useState(true);
  const [hiddenBooks, setHiddenBooks] = useState<Set<string>>(new Set());
  const [hoverChunk, setHoverChunk] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [resetsAt, setResetsAt] = useState<string | undefined>();
  // The library overview, fetched once for both panes: undefined while loading, null if unavailable or empty.
  const [overview, setOverview] = useState<SubgraphData | null | undefined>();
  useEffect(() => { applyTheme(loadTheme()); }, []);
  useEffect(() => {
    api.settings().then((s) => { if (!isError(s)) setShowInternals(s.show_internals); });
    api.library().then((l) => { if (!isError(l)) setBooks(l.books); });
    api.overview().then((o) => setOverview(!isError(o) && Array.isArray(o.nodes) && o.nodes.length ? o : null));
  }, []);
  useEffect(() => {
    // "interrupted" too: a dropped stream may still have spent requests.
    if (ask.state.status !== "done" && ask.state.status !== "error" && ask.state.status !== "interrupted") return;
    setRefreshKey((k) => k + 1);
    api.quota().then((q) => { if (!isError(q)) setResetsAt(q.resets_at); });
  }, [ask.state.status]);
  const bookTitles = useMemo(() => new Map(books.map((b) => [b.id, b.title])), [books]);
  const graphed = books.filter((b) => b.status === "graphed");
  const noBooks = graphed.length > 0 && graphed.every((b) => hiddenBooks.has(b.id));
  return (
    <div className="app">
      <TopBar books={books} hiddenBooks={hiddenBooks} onBooksChange={setHiddenBooks} showInternals={showInternals} refreshKey={refreshKey} />
      <div className="panes">
        <ChatPane ask={ask} showInternals={showInternals} resetsAt={resetsAt} onHoverChunk={setHoverChunk} overview={overview} />
        <GraphPane key={ask.run} state={ask.state} hiddenBooks={hiddenBooks} hoverChunk={hoverChunk} noBooks={noBooks} bookTitles={bookTitles} overview={overview} />
      </div>
    </div>
  );
}
