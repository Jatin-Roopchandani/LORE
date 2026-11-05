import { act, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { onScrollFrame, progressThrough, usePinProgress, useReducedMotion } from "./scroll";

// Each test drives frames by hand: requestAnimationFrame queues, `frame()` runs what is queued.
let queued: FrameRequestCallback[] = [];
const frame = () => act(() => { const q = queued; queued = []; q.forEach((cb) => cb(0)); });
beforeEach(() => {
  queued = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { queued.push(cb); return queued.length; });
});
afterEach(() => vi.unstubAllGlobals());

test("progress through a scene is how far its top has gone above the viewport, over its extra height", () => {
  expect(progressThrough(100, 5000, 1000)).toBe(0); // not reached yet
  expect(progressThrough(0, 5000, 1000)).toBe(0);
  expect(progressThrough(-1000, 5000, 1000)).toBe(0.25);
  expect(progressThrough(-4000, 5000, 1000)).toBe(1);
  expect(progressThrough(-9000, 5000, 1000)).toBe(1); // scrolled past: stays at the end
  expect(progressThrough(-10, 800, 1000)).toBe(1); // shorter than the viewport: no division by zero or below
});

function Probe() {
  const ref = useRef<HTMLDivElement>(null);
  const p = usePinProgress(ref);
  return <div ref={ref} data-testid="scene" data-p={p} />;
}

test("the hook maps the scene's geometry to progress on scroll, once per frame", () => {
  const geometry = { top: 0 };
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 1000 });
  const proto = HTMLElement.prototype;
  const rect = vi.spyOn(proto, "getBoundingClientRect").mockImplementation(() => ({ top: geometry.top } as DOMRect));
  const height = vi.spyOn(proto, "offsetHeight", "get").mockReturnValue(5000);
  render(<Probe />);
  const scene = screen.getByTestId("scene");
  expect(scene.dataset.p).toBe("0");

  geometry.top = -2000;
  act(() => { window.dispatchEvent(new Event("scroll")); window.dispatchEvent(new Event("scroll")); });
  expect(queued).toHaveLength(1); // two scroll events, one frame
  expect(scene.dataset.p).toBe("0"); // nothing measured until the frame runs
  frame();
  expect(scene.dataset.p).toBe("0.5");

  geometry.top = -6000;
  act(() => { window.dispatchEvent(new Event("resize")); });
  frame();
  expect(scene.dataset.p).toBe("1");
  rect.mockRestore(); height.mockRestore();
});

test("every scene shares one scroll listener, removed when the last one goes", () => {
  const add = vi.spyOn(window, "addEventListener");
  const remove = vi.spyOn(window, "removeEventListener");
  const offA = onScrollFrame(() => {});
  const offB = onScrollFrame(() => {});
  const scrolls = add.mock.calls.filter(([type]) => type === "scroll");
  expect(scrolls).toHaveLength(1);
  expect(scrolls[0][2]).toEqual({ passive: true });
  offA();
  expect(remove.mock.calls.filter(([type]) => type === "scroll")).toHaveLength(0);
  offB();
  expect(remove.mock.calls.filter(([type]) => type === "scroll")).toHaveLength(1);
  add.mockRestore(); remove.mockRestore();
});

function Motion() {
  return <span data-testid="m">{useReducedMotion() ? "reduced" : "full"}</span>;
}

test("reduced motion follows the system setting, and is off where matchMedia is missing", () => {
  const { unmount } = render(<Motion />);
  expect(screen.getByTestId("m")).toHaveTextContent("full");
  unmount();
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), addEventListener() {}, removeEventListener() {} }));
  render(<Motion />);
  expect(screen.getByTestId("m")).toHaveTextContent("reduced");
});
