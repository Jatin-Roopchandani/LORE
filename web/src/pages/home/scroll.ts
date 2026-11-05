import { RefObject, useEffect, useState } from "react";

// The homepage's scroll scenes share ONE passive scroll listener (and one resize listener), throttled to a frame:
// every subscriber is called at most once per animation frame, however many scroll events arrive.
type Subscriber = () => void;
const subscribers = new Set<Subscriber>();
let queued = false;

const nextFrame = (cb: () => void) =>
  typeof window.requestAnimationFrame === "function" ? window.requestAnimationFrame(cb) : window.setTimeout(cb, 16);

function flush() {
  queued = false;
  subscribers.forEach((fn) => fn());
}
function schedule() {
  if (queued) return;
  queued = true;
  nextFrame(flush);
}

export function onScrollFrame(fn: Subscriber): () => void {
  if (subscribers.size === 0) {
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule, { passive: true });
  }
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
    if (subscribers.size === 0) {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      queued = false;
    }
  };
}

// How far the page has scrolled through a pinned scene, 0 to 1: 0 while its top is at or below the viewport's top,
// 1 once it has scrolled by its extra height (its height less one viewport), when the pinned view lets go.
export function progressThrough(top: number, height: number, viewport: number): number {
  return Math.min(1, Math.max(0, -top / Math.max(1, height - viewport)));
}

export function usePinProgress(ref: RefObject<HTMLElement>): number {
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    const measure = () => {
      const el = ref.current;
      if (el) setProgress(progressThrough(el.getBoundingClientRect().top, el.offsetHeight, window.innerHeight));
    };
    measure();
    return onScrollFrame(measure);
  }, [ref]);
  return progress;
}

// True once the page has left the very top (the nav turns solid).
export function useScrolled(past = 8): boolean {
  const [scrolled, setScrolled] = useState(() => window.scrollY > past);
  useEffect(() => {
    const check = () => setScrolled(window.scrollY > past);
    check();
    return onScrollFrame(check);
  }, [past]);
  return scrolled;
}

// A media query's current answer, kept up to date (false where matchMedia doesn't exist).
export function useMedia(query: string): boolean {
  const read = () => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false);
  const [matches, setMatches] = useState(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const change = () => setMatches(mq.matches);
    change();
    mq.addEventListener?.("change", change);
    return () => mq.removeEventListener?.("change", change);
  }, [query]);
  return matches;
}

export const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";
export const NARROW = "(max-width: 900px)";
export const useReducedMotion = () => useMedia(REDUCED_MOTION);
