import type { AskEvent } from "./types";

export const STAGGER_TOTAL_MS = 500;
const MIN_GAP_MS = 15;
const REPLAY_GAP_CAP_MS = 600;
// A live answer whose drawing has fallen this many events behind what has arrived catches up: events are drawn
// whole, without the stagger, until it is close again. Without it a deep answer's many link batches could leave the
// screen tens of seconds behind the server, still "running" long after the answer was in.
export const CATCH_UP_BACKLOG = 3;

export function splitForStagger(event: AskEvent, chunk: number): AskEvent[] {
  if (event.type === "seeds")
    return event.entities.map((e) => ({ ...event, entities: [e] }));
  if (event.type === "edges" && event.edges.length > 0) {
    const out: AskEvent[] = [];
    for (let i = 0; i < event.edges.length; i += chunk) out.push({ ...event, edges: event.edges.slice(i, i + chunk) });
    return out;
  }
  return [event];
}

export interface Scheduler { push(event: AskEvent): void; stop(): void }

export function createScheduler(dispatch: (e: AskEvent) => void, opts: { reduceMotion: boolean; replay?: boolean }): Scheduler {
  let stopped = false;
  if (opts.reduceMotion && !opts.replay)
    return { push: (e) => { if (!stopped) dispatch(e); }, stop: () => { stopped = true; } };
  let chain: Promise<void> = Promise.resolve();
  let lastT: number | null = null;
  let pending = 0;  // events pushed and not yet drawn
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const sleep = (ms: number) => new Promise<void>((resolve) => {
    if (ms <= 0) return resolve();
    const id = setTimeout(() => { timers.delete(id); resolve(); }, ms);
    timers.add(id);
  });
  return {
    push(event) {
      pending += 1;
      chain = chain.then(async () => {
        pending -= 1;
        if (stopped) return;
        if (opts.replay && lastT !== null) await sleep(Math.min(REPLAY_GAP_CAP_MS, Math.max(0, (event.t - lastT) * 1000)));
        lastT = event.t;
        const chunk = event.type === "edges" ? Math.max(1, Math.ceil(event.edges.length / 12)) : 1;
        const behind = !opts.replay && pending >= CATCH_UP_BACKLOG;
        const pieces = opts.reduceMotion || behind ? [event] : splitForStagger(event, chunk);
        const gap = Math.max(MIN_GAP_MS, STAGGER_TOTAL_MS / pieces.length);
        for (let i = 0; i < pieces.length; i++) {
          if (stopped) return;
          dispatch(pieces[i]);
          if (pieces.length > 1 && i < pieces.length - 1) await sleep(gap);
        }
      });
    },
    stop() { stopped = true; timers.forEach(clearTimeout); timers.clear(); },
  };
}
