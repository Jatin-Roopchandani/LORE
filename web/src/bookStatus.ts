import { useEffect, useState } from "react";
import { api, isError } from "./api";
import { LIBRARY, PROCESSING } from "./messages";
import type { LibraryBook, LibraryStatus, ProcessingBook, World } from "./types";
import { ALL_WORLDS } from "./worlds";

// The Library's book status (GET /api/books), shared: the Library publishes every poll it makes, so the ask box's
// "still being processed" note follows it without polling of its own. `useBookStatus` reads it once on mount and
// whenever `refreshKey` changes (an answer finished, the library changed), and then takes whatever is published.
let latest: LibraryStatus | null = null;
const listeners = new Set<(s: LibraryStatus) => void>();

export function publishBookStatus(status: LibraryStatus): void {
  latest = status;
  listeners.forEach((f) => f(status));
}

export function useBookStatus(refreshKey: string | number = 0): LibraryStatus | null {
  const [status, setStatus] = useState<LibraryStatus | null>(latest);
  useEffect(() => {
    listeners.add(setStatus);
    return () => { listeners.delete(setStatus); };
  }, []);
  useEffect(() => {
    let live = true;
    // A hint only: a failed read (or an older server) just shows no note.
    Promise.resolve().then(() => api.books())
      .then((r) => { if (live && r && !isError(r) && Array.isArray(r.books)) publishBookStatus(r); })
      .catch(() => {});
    return () => { live = false; };
  }, [refreshKey]);
  return status;
}

// Only for tests: forget what was published.
export function resetBookStatus(): void { latest = null; }

/** The books in `world` (a world id, or "all") that can't be searched yet: not processed to the graph. */
export function unprocessedBooks(status: LibraryStatus | null, world: string, worlds: World[] | null): LibraryBook[] {
  if (!status) return [];
  const inWorld = world === ALL_WORLDS ? null
    : new Set((worlds ?? []).find((w) => String(w.id) === world)?.books.map((b) => b.id) ?? []);
  return status.books.filter((b) => b.status !== "graphed" && b.job?.state !== "done" && (inWorld === null || inWorld.has(b.id)));
}

const clock = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

function progress(stage: string | null, done: number | null, total: number | null): string | null {
  if (stage === null || done === null || total === null) return null;
  if (stage === "extract") return total > 0 ? PROCESSING.partsRead(done, total) : LIBRARY.stage(stage).toLowerCase();
  const label = LIBRARY.stage(stage).toLowerCase();
  return total > 1 ? PROCESSING.stageCount(label, done, total) : label;
}

function when(b: ProcessingBook, now: number): string {
  if (b.state === "running") return PROCESSING.running;
  if (b.state === "waiting_quota" && b.resumable_at) {
    const at = new Date(b.resumable_at);
    const left = Math.ceil((at.getTime() - now) / 60000);
    return left > 0 ? PROCESSING.resumesAfter(clock(at), LIBRARY.inHM(left)) : PROCESSING.resumingSoon;
  }
  if (b.state === "waiting_key") return PROCESSING.pausedKey;
  if (b.state === "failed") return PROCESSING.failed;
  return PROCESSING.paused;
}

/** What the chat says of one book an empty answer could not search: the text after its title. */
export function processingLine(b: ProcessingBook, now: number): string {
  if (b.state === "not_started") return PROCESSING.notStarted;
  return `${PROCESSING.stillBeing(progress(b.stage, b.done, b.total))} ${when(b, now)}`;
}

/** The ask box's count for a book: "969/1,051", or null with nothing counted yet. */
export function askCount(b: LibraryBook): string | null {
  return b.job && b.job.total > 0 ? PROCESSING.askCount(b.job.done, b.job.total) : null;
}
