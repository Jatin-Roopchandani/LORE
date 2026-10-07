import { shellQuote } from "./shell";

test("shellQuote wraps in single quotes and escapes embedded quotes", () => {
  expect(shellQuote('a"; rm -rf ~; ".epub')).toBe(`'a"; rm -rf ~; ".epub'`);
  expect(shellQuote("$(whoami).epub")).toBe("'$(whoami).epub'");
  expect(shellQuote("`x`.epub")).toBe("'`x`.epub'");
  expect(shellQuote("it's.epub")).toBe(`'it'\\''s.epub'`);
});
