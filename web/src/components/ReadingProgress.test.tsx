import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { POSITION } from "../messages";
import type { PositionBook, ProgressState, ReadingPositionBody, ReadingPositionData } from "../types";
import { ReadingProgress } from "./ReadingProgress";

vi.mock("../api", () => ({
  api: { readingPosition: vi.fn(), setReadingPosition: vi.fn() },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));
const m = (f: unknown) => f as ReturnType<typeof vi.fn>;

const sections = (n: number, named = false) =>
  Array.from({ length: n }, (_, i) => ({ seq: i + 2, number: i + 1, heading: named ? `Heading ${i + 1}` : null }));
const book = (id: string, title: string, series: string | null, index: number | null, n: number): PositionBook =>
  ({ id, title, series, series_index: index, chapters: n, sections: sections(n, id === "s2"), order: "unknown", state: "finished", progress_chapter: 0 });
const WORLDS: Record<string, PositionBook[]> = {
  "1": [book("s1", "A Study in Scarlet", "Holmes", 1, 14), book("s2", "The Sign of the Four", "Holmes", 2, 12),
    book("s3", "The Hound", "Holmes", 3, 15), book("x", "A Standalone", null, null, 5)],
  "2": [book("o1", "Other Solo", null, null, 3)],
};
const NAMES: Record<string, string> = { "1": "Sherlock Holmes", "2": "Other Realm" };

// A tiny server with the series fill-in of lore/query/visible.py: states per world, every book finished at first.
let states: Record<string, Record<string, { state: ProgressState; chapter: number }>>;
const view = (w: string, filled: { id: string; state: ProgressState }[] = []): ReadingPositionData => ({
  world: { id: Number(w), name: NAMES[w] }, enabled: false, book_id: null, chapter: 0, read_books: [], filled,
  books: WORLDS[w].map((b) => ({ ...b, state: states[w][b.id]?.state ?? "finished", progress_chapter: states[w][b.id]?.chapter ?? 0 })) });
const put = (w: string, body: ReadingPositionBody) => {
  const { book_id, state, chapter } = body.change;
  const books = WORLDS[w];
  const me = books.find((b) => b.id === book_id)!;
  const before = Object.fromEntries(books.map((b) => [b.id, states[w][b.id]?.state ?? "finished"]));
  states[w][book_id] = { state, chapter: state === "reading" ? chapter ?? me.sections![0].seq : 0 };
  for (const b of books) {
    if (b.id === book_id || b.series !== me.series || me.series_index == null || b.series_index == null) continue;
    if (b.series_index < me.series_index && state !== "not_started") states[w][b.id] = { state: "finished", chapter: 0 };
    if (b.series_index > me.series_index && state !== "finished") states[w][b.id] = { state: "not_started", chapter: 0 };
  }
  const filled = books.filter((b) => b.id !== book_id && (states[w][b.id]?.state ?? "finished") !== before[b.id])
    .map((b) => ({ id: b.id, state: states[w][b.id].state }));
  return view(w, filled);
};

beforeEach(() => {
  states = { "1": {}, "2": {} };
  m(api.readingPosition).mockReset().mockImplementation(async (w: string) => view(w));
  m(api.setReadingPosition).mockReset().mockImplementation(async (w: string, body: ReadingPositionBody) => put(w, body));
});

const button = (name: string) => screen.findByRole("button", { name: POSITION.buttonLabel(name) });
const open = async (label = POSITION.allRead) => { await userEvent.click(await button(label)); return screen.findByRole("dialog"); };
const group = (title: string) => screen.getByRole("radiogroup", { name: title });

test("every book starts finished: the button says All read and the list says LORE uses them all", async () => {
  render(<ReadingProgress world="1" />);
  const dialog = await open();
  expect(within(dialog).getByRole("heading", { name: POSITION.heading("Sherlock Holmes") })).toBeInTheDocument();
  for (const title of ["A Study in Scarlet", "The Sign of the Four", "The Hound", "A Standalone"])
    expect(within(group(title)).getByRole("radio", { name: POSITION.states.finished })).toBeChecked();
  expect(within(group("A Study in Scarlet")).getAllByRole("radio").map((r) => r.parentElement?.textContent))
    .toEqual(["Finished", "Reading", "Not started"]);
  expect(screen.getByText(POSITION.lead)).toBeInTheDocument();
  expect(screen.getByText(POSITION.usesAll)).toBeInTheDocument();
  // no limit switch any more
  expect(screen.queryByRole("switch")).toBeNull();
  expect(screen.queryByRole("combobox")).toBeNull();
});

test("Reading shows a chapter picker of the book's real chapters, and the line says where answers stop", async () => {
  const onChange = vi.fn();
  render(<ReadingProgress world="1" onChange={onChange} />);
  await open();
  await userEvent.click(within(group("The Sign of the Four")).getByRole("radio", { name: POSITION.states.reading }));
  expect(api.setReadingPosition).toHaveBeenCalledWith("1", { change: { book_id: "s2", state: "reading" } });
  const picker = await screen.findByRole("combobox", { name: POSITION.chapterLabelFor("The Sign of the Four") });
  expect(within(picker).getAllByRole("option").map((o) => o.textContent).slice(0, 2)).toEqual(["1 · Heading 1", "2 · Heading 2"]);
  await userEvent.selectOptions(picker, "7 · Heading 7");
  expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", { change: { book_id: "s2", state: "reading", chapter: 8 } });
  expect(await screen.findByText(POSITION.stopAt([{ chapter: 7, title: "The Sign of the Four" }]))).toBeInTheDocument();
  expect(screen.getByText("Answers, graph and profiles stop at chapter 7 of The Sign of the Four.")).toBeInTheDocument();
  expect(await button("Ch. 7 · The Sign of the Four")).toHaveClass("on");
  expect(onChange).toHaveBeenCalledTimes(2);
});

test("series order fills itself in, and a small note says which books it marked", async () => {
  states["1"] = { s1: { state: "not_started", chapter: 0 }, s2: { state: "not_started", chapter: 0 }, s3: { state: "not_started", chapter: 0 } };
  render(<ReadingProgress world="1" />);
  await open("1 of 4 read");
  await userEvent.click(within(group("The Hound")).getByRole("radio", { name: POSITION.states.finished }));
  const note = await screen.findByRole("status");
  expect(note).toHaveTextContent("Following the series order, also marked A Study in Scarlet and The Sign of the Four as finished.");
  expect(within(group("A Study in Scarlet")).getByRole("radio", { name: POSITION.states.finished })).toBeChecked();
  // an earlier book set to Not started marks the later ones too; the standalone is left alone
  await userEvent.click(within(group("The Sign of the Four")).getByRole("radio", { name: POSITION.states.not_started }));
  expect(await screen.findByRole("status")).toHaveTextContent("also marked The Hound as not started");
  expect(within(group("A Standalone")).getByRole("radio", { name: POSITION.states.finished })).toBeChecked();
  expect(screen.getByText(POSITION.leaveOut(["The Sign of the Four", "The Hound"]))).toBeInTheDocument();
  expect(await button("2 of 4 read")).toBeInTheDocument();
  // no modal: nothing else asks to be confirmed
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
});

test("the top-bar label: the chapter being read, All read, or how many are read", async () => {
  states["1"] = { s1: { state: "reading", chapter: 4 } };
  const { unmount } = render(<ReadingProgress world="1" />);
  expect(await button("Ch. 3 · A Study in Scarlet")).toBeInTheDocument();  // section 4 is story chapter 3
  unmount();
  states["1"] = { x: { state: "not_started", chapter: 0 } };
  render(<ReadingProgress world="1" />);
  expect(await button("3 of 4 read")).toBeInTheDocument();
});

test("All worlds: the rows are grouped by world and each change goes to its own world", async () => {
  const worlds = [{ id: 1, name: "Sherlock Holmes", books: [] }, { id: 2, name: "Other Realm", books: [] }];
  render(<ReadingProgress world="all" worlds={worlds} />);
  const dialog = await open();
  expect(within(dialog).getByRole("heading", { name: POSITION.heading(POSITION.allWorlds) })).toBeInTheDocument();
  const other = within(dialog).getByRole("region", { name: "Other Realm" });
  expect(within(other).getByRole("radiogroup", { name: "Other Solo" })).toBeInTheDocument();
  await userEvent.click(within(other).getByRole("radio", { name: POSITION.states.not_started }));
  expect(api.setReadingPosition).toHaveBeenCalledWith("2", { change: { book_id: "o1", state: "not_started" } });
  expect(await button("4 of 5 read")).toBeInTheDocument();
  expect(within(other).getByText(POSITION.usesNone)).toBeInTheDocument();
  expect(within(dialog).getByRole("region", { name: "Sherlock Holmes" })).toHaveTextContent(POSITION.usesAll);
});

test("keyboard: opening focuses the first book's state, arrows move within its group, Escape returns to the button", async () => {
  render(<ReadingProgress world="1" />);
  await open();
  const first = within(group("A Study in Scarlet")).getByRole("radio", { name: POSITION.states.finished });
  await waitFor(() => expect(first).toHaveFocus());
  await userEvent.keyboard("{ArrowRight}");
  await waitFor(() => expect(api.setReadingPosition).toHaveBeenCalledWith("1", { change: { book_id: "s1", state: "reading" } }));
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(await button("Ch. 1 · A Study in Scarlet")).toHaveFocus();
});

test("onLimited says whether anything is held back", async () => {
  const onLimited = vi.fn();
  render(<ReadingProgress world="1" onLimited={onLimited} />);
  await open();
  expect(onLimited).toHaveBeenLastCalledWith(false);
  await userEvent.click(within(group("A Standalone")).getByRole("radio", { name: POSITION.states.not_started }));
  await waitFor(() => expect(onLimited).toHaveBeenLastCalledWith(true));
});

test("a change the server refuses says so and keeps the list", async () => {
  m(api.setReadingPosition).mockResolvedValueOnce({ error: { code: "invalid_position", message: "no" } });
  render(<ReadingProgress world="1" />);
  await open();
  await userEvent.click(within(group("A Standalone")).getByRole("radio", { name: POSITION.states.not_started }));
  expect(await screen.findByRole("alert")).toHaveTextContent(POSITION.failed);
  expect(within(group("A Standalone")).getByRole("radio", { name: POSITION.states.finished })).toBeChecked();
});
