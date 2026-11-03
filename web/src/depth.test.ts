import { DEPTH_KEY, loadDepth, saveDepth } from "./depth";

beforeEach(() => localStorage.clear());

test("auto when nothing, or something invalid, is stored", () => {
  expect(loadDepth()).toBe("auto");
  localStorage.setItem(DEPTH_KEY, "bogus");
  expect(loadDepth()).toBe("auto");
});

test("a saved choice comes back", () => {
  saveDepth("deep");
  expect(localStorage.getItem(DEPTH_KEY)).toBe("deep");
  expect(loadDepth()).toBe("deep");
});

test("storage that throws is tolerated", () => {
  const bad = { getItem() { throw new Error("no"); }, setItem() { throw new Error("no"); } } as unknown as Storage;
  expect(loadDepth(bad)).toBe("auto");
  expect(() => saveDepth("focused", bad)).not.toThrow();
});
