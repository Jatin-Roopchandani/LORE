// The reading progress list, as text: the top-bar label and the line under the list. The states come from the server
// (lore/query/visible.py book_states), which is exactly what the graph, panels and answers use.
import { POSITION } from "./messages";
import type { PositionBook, PositionSection, ReadingPositionData } from "./types";

/** The book's story chapters (a server that sends none: chapters 1..n, unnamed). */
export const storyOf = (book: PositionBook): PositionSection[] =>
  book.sections ?? Array.from({ length: book.chapters }, (_, i) => ({ seq: i + 1, number: i + 1, heading: null }));

/** The story chapter a stored raw section (`seq`) falls in, as the reader counts it (0: before chapter 1). */
export const chapterOf = (book: PositionBook, seq: number) => storyOf(book).filter((s) => s.seq <= seq).length;

/** The chapter picker's options: the raw section to store and its label. Before chapter 1 is offered only when stored. */
export function chapterOptions(book: PositionBook): { seq: number; label: string }[] {
  const story = storyOf(book).map((s) => ({ seq: s.seq, label: POSITION.chapterOption(s.number, s.heading) }));
  const first = story[0]?.seq ?? 1;
  return book.state === "reading" && book.progress_chapter < first ? [{ seq: book.progress_chapter, label: POSITION.chapterOption(0) }, ...story] : story;
}

/** The option a book being read shows: the last story chapter at or before its stored section. */
export function pickedSeq(book: PositionBook): number {
  const options = chapterOptions(book);
  let found = options[0]?.seq ?? 0;
  for (const o of options) if (o.seq <= book.progress_chapter) found = o.seq;
  return found;
}

const reading = (books: PositionBook[]) => books.filter((b) => b.state === "reading");

/** The line under the list: where answers, the graph and profiles stop. */
export function summaryLine(books: PositionBook[]): string {
  if (!books.length || books.every((b) => b.state === "finished")) return POSITION.usesAll;
  const now = reading(books);
  if (now.length) return POSITION.stopAt(now.map((b) => ({ chapter: chapterOf(b, b.progress_chapter), title: b.title })));
  if (books.every((b) => b.state === "not_started")) return POSITION.usesNone;
  return POSITION.leaveOut(books.filter((b) => b.state === "not_started").map((b) => b.title));
}

/** The top-bar control's text over the worlds shown: the book being read, else all read, else how many are read. */
export function topBarLabel(worlds: ReadingPositionData[]): string {
  const books = worlds.flatMap((w) => w.books);
  const now = reading(books);
  if (now.length) {
    const first = POSITION.chapterBadge(chapterOf(now[0], now[0].progress_chapter), now[0].title);
    return now.length > 1 ? `${first} ${POSITION.more(now.length - 1)}` : first;
  }
  const finished = books.filter((b) => b.state === "finished").length;
  return finished === books.length ? POSITION.allRead : POSITION.someRead(finished, books.length);
}

/** Whether anything is held back: the ask box offers its one-question override only then. */
export const limits = (worlds: ReadingPositionData[]) => worlds.some((w) => w.books.some((b) => b.state !== "finished"));
