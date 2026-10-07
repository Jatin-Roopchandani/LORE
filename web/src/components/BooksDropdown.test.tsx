import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BooksDropdown } from "./BooksDropdown";
import type { Book } from "../types";

const books: Book[] = Array.from({ length: 12 }, (_, i) => ({ id: `b${i}`, title: `Book ${i}`, author: i % 2 ? "Doyle" : "Austen",
  status: i === 11 ? "chunked" : "graphed", chunk_count: 10, entity_count: 5 }));

test("filters a long library and toggles books", async () => {
  const onChange = vi.fn();
  render(<BooksDropdown books={books} hiddenBooks={new Set()} onChange={onChange} />);
  expect(screen.getByRole("button", { name: /All books \(11\)/ })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /All books/ }));
  await userEvent.type(screen.getByRole("searchbox"), "Book 1");
  expect(screen.getAllByRole("checkbox")).toHaveLength(3);   // Book 1, 10, 11
  expect(screen.getByRole("checkbox", { name: /Book 11/ })).toBeDisabled();
  await userEvent.click(screen.getByRole("checkbox", { name: /Book 10/ }));
  expect(onChange).toHaveBeenCalledWith(new Set(["b10"]));
});

test("label summarises the selection", () => {
  const { rerender } = render(<BooksDropdown books={books} hiddenBooks={new Set(books.slice(0, 10).map((b) => b.id))} onChange={() => {}} />);
  expect(screen.getByRole("button", { name: "Book 10" })).toBeInTheDocument();
  rerender(<BooksDropdown books={books} hiddenBooks={new Set(["b1"])} onChange={() => {}} />);
  expect(screen.getByRole("button", { name: /10 of 11 books/ })).toBeInTheDocument();
});
