import { useCallback, useEffect, useState } from "react";
import { api, isError } from "./api";
import { ChatPane } from "./components/ChatPane";
import { GraphPane } from "./components/GraphPane";
import { HistorySidebar } from "./components/HistorySidebar";
import { QuickSwitcher } from "./components/QuickSwitcher";
import { TopBar } from "./components/TopBar";
import { applyTheme, loadTheme } from "./theme";
import type { Book, SubgraphData, World } from "./types";
import { useAsk } from "./useAsk";
import { loadWorld, resolveWorld, saveWorld } from "./worlds";

export default function App() {
  // The remembered world choice; the list of worlds (null while loading) can still overrule it.
  const [stored, setStored] = useState<string | null>(() => loadWorld());
  const [worlds, setWorlds] = useState<World[] | null>(null);
  const world = resolveWorld(stored, worlds);
  const chooseWorld = useCallback((w: string) => { setStored(w); saveWorld(w); }, []);
  const ask = useAsk(world, chooseWorld);
  const [books, setBooks] = useState<Book[]>([]);
  const [showInternals, setShowInternals] = useState(true);
  const [hiddenBooks, setHiddenBooks] = useState<Set<string>>(new Set());
  const [hoverChunks, setHoverChunks] = useState<string[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const [resetsAt, setResetsAt] = useState<string | undefined>();
  // The library overview, fetched once for both panes: undefined while loading, null if unavailable or empty.
  const [overview, setOverview] = useState<SubgraphData | null | undefined>();
  // Bumped when the Library panel changed the worlds or moved a book.
  const [libraryVersion, setLibraryVersion] = useState(0);
  // History is a drawer over the page at every width (never a column); Ctrl/Cmd+K opens the quick switcher.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [quickOpen, setQuickOpen] = useState(false);
  const toggleHistory = useCallback(() => setDrawerOpen((o) => !o), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const closeQuick = useCallback(() => setQuickOpen(false), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); setDrawerOpen(false); setQuickOpen((o) => !o); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => { applyTheme(loadTheme()); }, []);
  useEffect(() => {
    api.settings().then((s) => { if (!isError(s)) setShowInternals(s.show_internals); });
  }, []);
  useEffect(() => {
    api.worlds().then((w) => { if (!isError(w) && Array.isArray(w)) setWorlds(w); });
  }, [libraryVersion]);
  // The graph follows the world: switching worlds refetches the books and the overview. A reply for a
  // world that has since been left is dropped.
  useEffect(() => {
    let live = true;
    api.library(world).then((l) => { if (live && !isError(l)) setBooks(l.books); });
    api.overview(world).then((o) => { if (live) setOverview(!isError(o) && Array.isArray(o.nodes) && o.nodes.length ? o : null); });
    return () => { live = false; };
  }, [world, libraryVersion]);
  // A person changing the world starts afresh: the old answer's graph belongs to the other world.
  const switchWorld = useCallback((w: string) => {
    if (w === world) return;
    setOverview(undefined);
    setHiddenBooks(new Set());
    ask.clear();
    chooseWorld(w);
  }, [world, ask.clear, chooseWorld]);
  useEffect(() => {
    // "interrupted" too: a dropped stream may still have spent requests.
    if (ask.state.status !== "done" && ask.state.status !== "error" && ask.state.status !== "interrupted") return;
    setRefreshKey((k) => k + 1);
    api.quota().then((q) => { if (!isError(q)) setResetsAt(q.resets_at); });
  }, [ask.state.status]);
  const replay = useCallback((id: string) => { ask.replay(id); setDrawerOpen(false); }, [ask.replay]);
  // A fresh start: no answer (so the graph returns to the library), and the ask box ready to type in.
  const newQuestion = useCallback(() => {
    ask.clear();
    setDrawerOpen(false);
    setTimeout(() => document.getElementById("ask-input")?.focus(), 0);
  }, [ask.clear]);
  const graphed = books.filter((b) => b.status === "graphed");
  const noBooks = graphed.length > 0 && graphed.every((b) => hiddenBooks.has(b.id));
  return (
    <div className="app">
      <TopBar books={books} hiddenBooks={hiddenBooks} onBooksChange={setHiddenBooks} showInternals={showInternals} refreshKey={refreshKey}
        worlds={worlds ?? []} world={world} onWorldChange={switchWorld} onLibraryChange={() => setLibraryVersion((v) => v + 1)}
        historyOpen={drawerOpen} onToggleHistory={toggleHistory} />
      <div className="panes">
        {drawerOpen && (
          <HistorySidebar items={ask.history} current={ask.current} world={world} worlds={worlds ?? []} onReplay={replay} onDelete={ask.remove}
            onClear={ask.clearHistory} onNew={newQuestion} onClose={closeDrawer} />
        )}
        <ChatPane ask={ask} showInternals={showInternals} resetsAt={resetsAt} onHoverChunks={setHoverChunks} overview={overview} />
        <GraphPane key={ask.run} state={ask.state} hiddenBooks={hiddenBooks} hoverChunks={hoverChunks} noBooks={noBooks} books={books} overview={overview} world={world} onAsk={ask.ask} />
      </div>
      {quickOpen && <QuickSwitcher items={ask.history} onReplay={ask.replay} onAsk={(q) => ask.ask(q)} onClose={closeQuick} />}
    </div>
  );
}
