import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { POSITION } from "../messages";
import type { ReadingPositionBody, ReadingPositionData } from "../types";
import { ReadingPosition, SLIDER_DEBOUNCE_MS } from "./ReadingPosition";

vi.mock("../api", () => ({
  api: { readingPosition: vi.fn(), setReadingPosition: vi.fn() },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));
const m = (f: unknown) => f as ReturnType<typeof vi.fn>;

const books: ReadingPositionData["books"] = [
  { id: "b1", title: "Hollow Gate", series: "Hollow Saga", series_index: 1, chapters: 12, order: "unknown" },
  { id: "b2", title: "Hollow Keep", series: "Hollow Saga", series_index: 2, chapters: 9, order: "unknown" },
  { id: "b3", title: "Wanderer Road", series: null, series_index: null, chapters: 5, order: "unknown" }];

// A tiny server: what was PUT is what the next GET returns, and `order` follows the book being read.
let stored: ReadingPositionBody;
const view = (): ReadingPositionData => ({
  world: { id: 1, name: "Hollow Saga" }, ...stored,
  books: books.map((b) => ({ ...b, order: b.id === stored.book_id ? "current" :
    stored.book_id && b.series && b.series_index != null && b.series_index < (books.find((x) => x.id === stored.book_id)?.series_index ?? 0) ? "before" : "unknown" })) });

beforeEach(() => {
  stored = { enabled: false, book_id: null, chapter: 0, read_books: [] };
  m(api.readingPosition).mockReset().mockImplementation(async () => view());
  m(api.setReadingPosition).mockReset().mockImplementation(async (_w: string, body: ReadingPositionBody) => { stored = body; return view(); });
});

const open = async () => {
  await userEvent.click(screen.getByRole("button", { name: new RegExp(POSITION.label) }));
  return screen.findByRole("switch", { name: POSITION.switchLabel });
};

test("the switch stores the position through the API, shows the badge and tells the app", async () => {
  const onChange = vi.fn();
  render(<ReadingPosition world="1" onChange={onChange} />);
  expect(screen.queryByRole("status")).toBeNull();
  const sw = await open();
  expect(sw).not.toBeChecked();
  await userEvent.click(sw);
  expect(api.setReadingPosition).toHaveBeenCalledWith("1", { enabled: true, book_id: "b1", chapter: 0, read_books: [] });
  expect(await screen.findByRole("status")).toHaveTextContent("Up to Hollow Gate ch. 0");
  expect(sw).toBeChecked();
  expect(onChange).toHaveBeenCalledTimes(1);
  await userEvent.click(sw);
  expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", expect.objectContaining({ enabled: false }));
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
});

test("the position persists: a fresh control reads it back from the server", async () => {
  stored = { enabled: true, book_id: "b2", chapter: 4, read_books: ["b3"] };
  render(<ReadingPosition world="1" />);
  // The badge is there before the menu is ever opened.
  expect(await screen.findByRole("status")).toHaveTextContent("Up to Hollow Keep ch. 4");
  expect(api.readingPosition).toHaveBeenCalledWith("1");
  await open();
  expect(screen.getByRole("switch", { name: POSITION.switchLabel })).toBeChecked();
  expect(screen.getByRole("combobox", { name: POSITION.bookLabel })).toHaveValue("b2");
  expect(screen.getByRole("slider", { name: POSITION.chapterLabel })).toHaveValue("4");
  expect(screen.getByRole("checkbox", { name: POSITION.readLabel("Wanderer Road") })).toBeChecked();
});

test("books are listed in the order the server gives, with series numbers", async () => {
  render(<ReadingPosition world="1" />);
  await open();
  const options = screen.getAllByRole("option").map((o) => o.textContent);
  expect(options).toEqual(["—", "Hollow Gate (book 1)", "Hollow Keep (book 2)", "Wanderer Road"]);
});

test("read toggles are offered only for books of unknown order, never the one being read or an earlier one", async () => {
  stored = { enabled: true, book_id: "b2", chapter: 1, read_books: [] };
  render(<ReadingPosition world="1" />);
  await open();
  expect(screen.getAllByRole("checkbox", { name: /I have read/ }).map((c) => c.getAttribute("aria-label") ?? c.parentElement?.textContent))
    .toEqual([POSITION.readLabel("Wanderer Road")]);
  await userEvent.click(screen.getByRole("checkbox", { name: POSITION.readLabel("Wanderer Road") }));
  expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", { enabled: true, book_id: "b2", chapter: 1, read_books: ["b3"] });
  await userEvent.click(screen.getByRole("checkbox", { name: POSITION.readLabel("Wanderer Road") }));
  expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", expect.objectContaining({ read_books: [] }));
});

test("choosing another book starts at its first chapter", async () => {
  stored = { enabled: true, book_id: "b1", chapter: 7, read_books: ["b3"] };
  render(<ReadingPosition world="1" />);
  await open();
  await userEvent.selectOptions(screen.getByRole("combobox", { name: POSITION.bookLabel }), "b2");
  expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", { enabled: true, book_id: "b2", chapter: 0, read_books: [] });
  expect(await screen.findByRole("status")).toHaveTextContent("Up to Hollow Keep ch. 0");
});

describe("the chapter slider", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  test("moves are debounced into one request and one refetch, and the badge follows at once", async () => {
    stored = { enabled: true, book_id: "b1", chapter: 2, read_books: [] };
    const onChange = vi.fn();
    render(<ReadingPosition world="1" onChange={onChange} />);
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("button", { name: new RegExp(POSITION.label) }));
    const slider = await screen.findByRole("slider", { name: POSITION.chapterLabel });
    expect(slider).toHaveAttribute("max", "12");
    fireEvent.change(slider, { target: { value: "3" } });
    fireEvent.change(slider, { target: { value: "4" } });
    fireEvent.change(slider, { target: { value: "5" } });
    expect(screen.getByRole("status")).toHaveTextContent("Up to Hollow Gate ch. 5");
    expect(slider).toHaveAttribute("aria-valuetext", POSITION.chapterValue(5, 12));
    act(() => { vi.advanceTimersByTime(SLIDER_DEBOUNCE_MS - 50); });
    expect(api.setReadingPosition).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(100); });
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(api.setReadingPosition).toHaveBeenCalledTimes(1);
    expect(api.setReadingPosition).toHaveBeenCalledWith("1", { enabled: true, book_id: "b1", chapter: 5, read_books: [] });
    expect(screen.getByRole("status")).toHaveTextContent("Up to Hollow Gate ch. 5");
  });
});

describe("a book with front matter", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  // Raw sections 1-4 are a licence, a title page and a contents page; the story is sections 5-7.
  const sections = [{ seq: 5, number: 1, heading: "Mr. Holmes" }, { seq: 6, number: 2, heading: null },
    { seq: 7, number: 3, heading: "The Lauriston Gardens Mystery" }];
  beforeEach(() => {
    books.push({ id: "b9", title: "Scarlet Road", series: null, series_index: null, chapters: 3, order: "unknown", sections });
  });
  afterEach(() => { books.pop(); });

  test("the slider counts story chapters only and names them; a stored section keeps its place", async () => {
    stored = { enabled: true, book_id: "b9", chapter: 7, read_books: [] };
    render(<ReadingPosition world="1" />);
    expect(await screen.findByRole("status")).toHaveTextContent("Up to Scarlet Road ch. 3");
    fireEvent.click(screen.getByRole("button", { name: new RegExp(POSITION.label) }));
    const slider = await screen.findByRole("slider", { name: POSITION.chapterLabel });
    expect(slider).toHaveAttribute("max", "3");
    expect(slider).toHaveValue("3");
    expect(slider).toHaveAttribute("aria-valuetext", "Ch. 3 of 3 \u00b7 The Lauriston Gardens Mystery");
    expect(screen.getByText(POSITION.chapterValue(3, 3, "The Lauriston Gardens Mystery"))).toBeInTheDocument();
    fireEvent.change(slider, { target: { value: "1" } });
    act(() => { vi.advanceTimersByTime(SLIDER_DEBOUNCE_MS + 50); });
    // Story chapter 1 is raw section 5: that is what is stored.
    await waitFor(() => expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", expect.objectContaining({ chapter: 5 })));
    expect(slider).toHaveAttribute("aria-valuetext", "Ch. 1 of 3 \u00b7 Mr. Holmes");
    fireEvent.change(slider, { target: { value: "0" } });
    act(() => { vi.advanceTimersByTime(SLIDER_DEBOUNCE_MS + 50); });
    await waitFor(() => expect(api.setReadingPosition).toHaveBeenLastCalledWith("1", expect.objectContaining({ chapter: 0 })));
    expect(slider).toHaveAttribute("aria-valuetext", POSITION.chapterValue(0, 3));
  });

  test("a position stored inside the front matter reads as not started", async () => {
    stored = { enabled: true, book_id: "b9", chapter: 3, read_books: [] };
    render(<ReadingPosition world="1" />);
    expect(await screen.findByRole("status")).toHaveTextContent("Up to Scarlet Road ch. 0");
  });
});

test("with all worlds shown there is nothing to set and it says why; no request is made", async () => {
  render(<ReadingPosition world="all" />);
  await userEvent.click(screen.getByRole("button", { name: new RegExp(POSITION.label) }));
  expect(screen.getByText(POSITION.chooseWorld)).toBeInTheDocument();
  expect(screen.queryByRole("switch")).toBeNull();
  expect(api.readingPosition).not.toHaveBeenCalled();
});

test("a failed save is reported and leaves the control as it was", async () => {
  m(api.setReadingPosition).mockResolvedValueOnce({ error: { code: "invalid_position", message: "no" } });
  render(<ReadingPosition world="1" />);
  const sw = await open();
  await userEvent.click(sw);
  expect(await screen.findByRole("alert")).toHaveTextContent(POSITION.failed);
  expect(sw).not.toBeChecked();
  expect(screen.queryByRole("status")).toBeNull();
});

test("switching world loads that world's position", async () => {
  const { rerender } = render(<ReadingPosition world="1" />);
  await open();
  stored = { enabled: true, book_id: "b3", chapter: 1, read_books: [] };
  rerender(<ReadingPosition world="2" />);
  expect(await screen.findByRole("status")).toHaveTextContent("Up to Wanderer Road ch. 1");
  expect(api.readingPosition).toHaveBeenLastCalledWith("2");
});
