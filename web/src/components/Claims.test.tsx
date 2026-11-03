import { fireEvent, render, screen } from "@testing-library/react";
import { Claims } from "./Claims";
import type { SearchResult } from "../types";

const result = { answer: "", mode: "graph", template: "neighbours", note: null, model: null, selection_model: null, sub_questions: ["q"],
  claims: [{ text: "Hope was to marry Lucy.", passage: 3, quote: "was to have married me twenty years ago" }],
  passages: [{ n: 3, chunk_id: "c3", book_id: "b1", book_title: "A Study in Scarlet", chapter_seq: 17,
    text: "That girl that I spoke of was to have married me twenty years ago." }], edges: [], seeds: [] } as SearchResult;

test("a citation opens its passage with the quote marked; hover reports the chunk", () => {
  const onHover = vi.fn();
  render(<Claims result={result} onHoverChunks={onHover} />);
  fireEvent.mouseEnter(screen.getByText("Hope was to marry Lucy.").closest("li")!);
  expect(onHover).toHaveBeenCalledWith(["c3"]);
  fireEvent.click(screen.getByRole("button", { name: /ch\.17/ }));
  expect(screen.getByText("was to have married me twenty years ago").tagName).toBe("MARK");
});
