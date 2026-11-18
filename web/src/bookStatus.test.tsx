import { render, screen } from "@testing-library/react";
import { unprocessedBooks } from "./bookStatus";
import { AskBox } from "./components/AskBox";
import { PROCESSING } from "./messages";
import type { LibraryBook, LibraryStatus, World } from "./types";

const book = (id: string, status: string, job: Partial<NonNullable<LibraryBook["job"]>> | null = null): LibraryBook => ({
  id, title: id.toUpperCase(), author: null, status, world: null, series: null, series_index: null,
  job: job && { stage: "extract", state: "running", done: 0, total: 0, stopped_reason: null, resumable_at: null,
    can_continue: false, continue_reason: "", ...job } });
const status = (books: LibraryBook[]): LibraryStatus => ({ books, gemini: { used_today: 0, limit: 0, answer_reserve: 0 } });
const worlds: World[] = [
  { id: 1, name: "Sample", books: [{ id: "s", title: "S", series: null, series_index: null }] },
  { id: 2, name: "Big", books: [{ id: "big", title: "BIG", series: null, series_index: null }] },
];

test("only the current world's unprocessed books, or every world's under all worlds", () => {
  const s = status([book("s", "graphed"), book("big", "chunked", { done: 969, total: 1051 })]);
  expect(unprocessedBooks(s, "1", worlds)).toEqual([]);
  expect(unprocessedBooks(s, "2", worlds).map((b) => b.id)).toEqual(["big"]);
  expect(unprocessedBooks(s, "all", worlds).map((b) => b.id)).toEqual(["big"]);
  expect(unprocessedBooks(null, "all", worlds)).toEqual([]);
});

test("the ask box names several unprocessed books with their counts, and one never started", () => {
  const { rerender } = render(<AskBox onAsk={() => {}} busy={false}
    pending={[book("big", "chunked", { done: 969, total: 1051 }), book("two", "registered")]} />);
  expect(screen.getByRole("note", { name: PROCESSING.noteLabel })).toHaveTextContent(
    `${PROCESSING.askMany(2)} BIG (969/1,051), TWO.`);
  rerender(<AskBox onAsk={() => {}} busy={false} pending={[book("two", "registered")]} />);
  expect(screen.getByRole("note", { name: PROCESSING.noteLabel })).toHaveTextContent(`TWO ${PROCESSING.askOneNotStarted}`);
  rerender(<AskBox onAsk={() => {}} busy={false} />);
  expect(screen.queryByRole("note", { name: PROCESSING.noteLabel })).toBeNull();
});
