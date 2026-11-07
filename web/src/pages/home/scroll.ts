import { RefObject, useEffect, useRef, useState } from "react";

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

// Where a scroll that came to rest between the top of the page and a pinned scene should settle: the scene when the
// reader was scrolling down, the top when scrolling up; null when it rests outside that gap (nothing to do).
export function snapTarget(y: number, sceneTop: number, direction: number): number | null {
  if (y <= 1 || y >= sceneTop - 1) return null;
  return direction > 0 ? sceneTop : 0;
}

// The hero and the first pinned scene never rest half-and-half: once scrolling stops in between, the page glides
// to whichever one the reader was heading for. Only where the hero fits one screen (on a phone the hero is taller
// than the view, and snapping would skip its lower part).
export function useSnapToScene(ref: RefObject<HTMLElement>, reduced: boolean): void {
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const fits = window.matchMedia("(min-width: 901px) and (min-height: 600px)");
    let last = window.scrollY, direction = 0, idle: number | undefined;
    const settle = () => {
      const el = ref.current;
      if (!el || !fits.matches) return;
      const to = snapTarget(window.scrollY, el.getBoundingClientRect().top + window.scrollY, direction);
      if (to !== null) window.scrollTo({ top: to, behavior: reduced ? "instant" : "smooth" });
    };
    const moved = () => {
      const y = window.scrollY;
      if (y !== last) direction = y > last ? 1 : -1;
      last = y;
      window.clearTimeout(idle);
      idle = window.setTimeout(settle, 140);
    };
    window.addEventListener("scroll", moved, { passive: true });
    return () => { window.removeEventListener("scroll", moved); window.clearTimeout(idle); };
  }, [ref, reduced]);
}

// A value that follows `target` smoothly: each frame it closes a fixed share of the gap, so a mouse wheel's coarse
// steps become one continuous glide. It starts AT the target (no animation on first paint) and, with reduced
// motion, simply is the target. `close` is the gap small enough to finish on: half a pixel for pixels, much less
// for a 0-to-1 fraction.
export function useEased(target: number, reduced: boolean, share = 0.14, close = 0.5): number {
  const [value, setValue] = useState(target);
  const current = useRef(target);
  const goal = useRef(target);
  const running = useRef(false);
  useEffect(() => {
    goal.current = target;
    if (reduced) { current.current = target; setValue(target); return; }
    if (running.current) return;
    running.current = true;
    const step = () => {
      const gap = goal.current - current.current;
      current.current = Math.abs(gap) < close ? goal.current : current.current + gap * share;
      setValue(current.current);
      if (current.current === goal.current) { running.current = false; return; }
      nextFrame(step);
    };
    nextFrame(step);
  }, [target, reduced, share, close]);
  return value;
}
