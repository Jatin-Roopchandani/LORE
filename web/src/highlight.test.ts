import { highlightQuote } from "./highlight";

const marked = (spans: { text: string; mark: boolean }[]) => spans.filter((s) => s.mark).map((s) => s.text);

test("marks an exact quote", () => {
  expect(marked(highlightQuote("He said hello to Watson today.", "said hello to Watson"))).toEqual(["said hello to Watson"]);
});

test("matches across curly quotes, dashes and case", () => {
  const text = "“That girl that I spoke of was to have married me twenty years ago—she was forced.”";
  expect(marked(highlightQuote(text, '"that girl that I spoke of was to have married me twenty years ago-she'))).toHaveLength(1);
});

test("marks each ellipsis segment of at least three words", () => {
  const text = "guilty of the death of two human beings, a father and a daughter, and that they had forfeited their own lives.";
  const spans = highlightQuote(text, "...guilty of the death of two human beings ... they had forfeited their own lives.");
  expect(marked(spans)).toEqual(["guilty of the death of two human beings", "they had forfeited their own lives."]);
  expect(spans.map((s) => s.text).join("")).toBe(text);
});

test("an unfound quote marks nothing and keeps the text", () => {
  const spans = highlightQuote("plain text", "not here at all");
  expect(spans).toEqual([{ text: "plain text", mark: false }]);
});
