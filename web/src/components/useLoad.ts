import { useEffect, useState } from "react";
import { isError } from "../api";
import { errorMessage } from "../messages";
import type { ApiError } from "../types";

export type Loaded<T> = { status: "loading" } | { status: "error"; message: string } | { status: "ok"; data: T };

/** Runs `load` when `deps` change (not at all while `load` is null) and reports loading, error or the data.
 *  A reply that lands after the deps changed or the component left is dropped. */
export function useLoad<T>(load: (() => Promise<T | { error: ApiError }>) | null, deps: unknown[]): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ status: "loading" });
  useEffect(() => {
    if (!load) return;
    let live = true;
    setState({ status: "loading" });
    load().then((r) => {
      if (!live) return;
      // The reads never name a provider, so internals stay off.
      setState(isError(r) ? { status: "error", message: errorMessage(r.error, { showInternals: false }).text } : { status: "ok", data: r as T });
    });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}
