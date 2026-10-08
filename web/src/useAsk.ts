import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { streamAsk } from "./api";
import { askReducer, initialState, type AskState } from "./askReducer";
import { addToHistory, loadHistory, saveHistory, type HistoryItem } from "./history";
import { createScheduler, type Scheduler } from "./scheduler";
import type { AskEvent } from "./types";

type Action = { kind: "reset"; question: string } | { kind: "event"; event: AskEvent };
function reducer(state: AskState, action: Action): AskState {
  return action.kind === "reset" ? initialState(action.question) : askReducer(state, action.event);
}
const reduceMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

export function useAsk() {
  const [state, dispatch] = useReducer(reducer, initialState());
  const [history, setHistory] = useState<HistoryItem[]>(() => loadHistory());
  const [current, setCurrent] = useState<string | null>(null);
  // Bumped by every ask, retry and replay: views key on it so each run starts fresh.
  const [run, setRun] = useState(0);
  const loaded = useRef(history);
  // Saved outside the state updater (updaters must stay pure); the first render's list came from storage.
  useEffect(() => { if (history !== loaded.current) saveHistory(history); }, [history]);
  const abort = useRef<AbortController | null>(null);
  const scheduler = useRef<Scheduler | null>(null);
  const last = useRef<string>("");

  const stop = () => { abort.current?.abort(); scheduler.current?.stop(); };
  useEffect(() => stop, []);

  const ask = useCallback((question: string) => {
    stop();
    last.current = question;
    dispatch({ kind: "reset", question });
    const controller = new AbortController();
    abort.current = controller;
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion() });
    scheduler.current = sched;
    const id = `${Date.now()}`;
    setCurrent(id);
    setRun((n) => n + 1);
    (async () => {
      const events: AskEvent[] = [];
      for await (const event of streamAsk(question, controller.signal)) { events.push(event); sched.push(event); }
      if (controller.signal.aborted) return;
      if (events.some((e) => e.type === "answer"))
        setHistory((h) => addToHistory(h, { id, question, events, askedAt: new Date().toISOString() }));
    })();
  }, []);

  const replay = useCallback((id: string) => {
    const item = history.find((h) => h.id === id);
    if (!item) return;
    stop();
    setCurrent(id);
    setRun((n) => n + 1);
    dispatch({ kind: "reset", question: item.question });
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion(), replay: !reduceMotion() });
    scheduler.current = sched;
    item.events.forEach((e) => sched.push(e));
  }, [history]);

  const retry = useCallback(() => { if (last.current) ask(last.current); }, [ask]);
  return { state, history, current, run, ask, replay, retry, busy: state.status === "running" };
}
