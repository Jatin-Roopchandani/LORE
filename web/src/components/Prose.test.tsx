import { fireEvent, render, screen } from "@testing-library/react";
import { Prose } from "./Prose";
import type { SearchResult } from "../types";

const result = {
  answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: ["q"],
  claims: [{ text: "Hope loved Lucy.", passage: 3, quote: "Hope loved Lucy Ferrier" },
           { text: "Lucy married Drebber.", passage: 5, quote: "forced to marry Drebber" }],
  passages: [{ n: 3, chunk_id: "c3", book_id: "b1", book_title: "A Study in Scarlet", chapter_seq: 13, text: "Hope loved Lucy Ferrier dearly." },
             { n: 5, chunk_id: "c5", book_id: "b1", book_title: "A Study in Scarlet", chapter_seq: 14, text: "She was forced to marry Drebber." }],
  edges: [], seeds: [], answer_style: "prose",
  prose: [{ text: "Hope loved Lucy, who was forced to marry Drebber.", claims: [1, 2], passages: [3, 5], new_paragraph: false },
          { text: "He swore revenge.", claims: [1], passages: [3], new_paragraph: true }],
} as SearchResult;

test("renders paragraphs with a chip per passage", () => {
  const { container } = render(<Prose result={result} onHoverChunks={() => {}} />);
  expect(container.querySelectorAll("p.prose")).toHaveLength(2);
  expect(screen.getAllByRole("button", { name: /\[3\]/ })).toHaveLength(2);
  expect(screen.getByRole("button", { name: /\[5\]/ })).toBeInTheDocument();
});

test("a chip opens its passage with the cited quote marked", () => {
  render(<Prose result={result} onHoverChunks={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: /\[5\]/ }));
  expect(screen.getByText("forced to marry Drebber").tagName).toBe("MARK");
});

test("hovering a sentence reports every chunk it cites", () => {
  const onHover = vi.fn();
  render(<Prose result={result} onHoverChunks={onHover} />);
  fireEvent.mouseEnter(screen.getByText(/who was forced to marry Drebber/));
  expect(onHover).toHaveBeenCalledWith(["c3", "c5"]);
  fireEvent.mouseLeave(screen.getByText(/who was forced to marry Drebber/));
  expect(onHover).toHaveBeenLastCalledWith([]);
});

test("shows a muted line for parts the passages do not answer", () => {
  const { container } = render(<Prose result={{ ...result, unanswered: ["How did he lose it?"] }} onHoverChunks={() => {}} />);
  const line = container.querySelector("p.unanswered");
  expect(line?.textContent).toBe("The passages found don't answer: How did he lose it?");
});

test("no unanswered line when the list is empty", () => {
  const { container } = render(<Prose result={{ ...result, unanswered: [] }} onHoverChunks={() => {}} />);
  expect(container.querySelector(".unanswered")).toBeNull();
});
