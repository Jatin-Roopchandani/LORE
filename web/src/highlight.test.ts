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

test("single quote marks are ignored in passage and quote", () => {
  expect(marked(highlightQuote("‘Hello there friend,’ he said.", "Hello there friend, he said"))).toHaveLength(1);
  expect(marked(highlightQuote("He told us hello there friend today.", "'hello there friend'"))).toEqual(["hello there friend"]);
});

test("an apostrophe inside a word is kept", () => {
  expect(marked(highlightQuote("He took Hope's ring away.", "took Hope's ring"))).toEqual(["took Hope's ring"]);
  expect(marked(highlightQuote("He took Hope's ring away.", "took Hopes ring"))).toEqual([]);
});

test("matches whole words only", () => {
  const spans = highlightQuote("she said hello. he said hello there", "he said hello there");
  expect(spans.filter((s) => s.mark)).toEqual([{ text: "he said hello there", mark: true }]);
  expect(spans[0].text).toBe("she said hello. ");
});

test("a quote does not match inside a longer word", () => {
  const spans = highlightQuote("she said hello. he said hello there", "he said hello");
  expect(spans[0]).toEqual({ text: "she said hello. ", mark: false });
  expect(spans[1]).toEqual({ text: "he said hello", mark: true });
});
