import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LIBRARY } from "../messages";
import { SAMPLE_NOTICE_KEY, SampleNotice } from "./SampleNotice";

let books: object[];
const book = (id: string, title: string, is_sample = false) => ({ id, title, author: null, status: "graphed", world: null,
  series: null, series_index: null, job: null, is_sample });

beforeEach(() => {
  localStorage.clear();
  books = [book("s", "A Study in Scarlet", true)];
  vi.stubGlobal("fetch", vi.fn(async () => ({ status: 200, ok: true,
    json: async () => ({ books, gemini: { used_today: 0, limit: 1, answer_reserve: 0 } }) })));
});
afterEach(() => vi.unstubAllGlobals());

const show = async (onAdd = vi.fn()) => { render(<SampleNotice onAdd={onAdd} />); await act(async () => {}); return onAdd; };

test("with the sample book in the library: names it and says how to add one's own", async () => {
  await show();
  const notice = screen.getByRole("status", { name: LIBRARY.sampleNoticeLabel });
  expect(notice).toHaveTextContent(LIBRARY.sampleNotice("A Study in Scarlet"));
  expect(notice).toHaveTextContent(LIBRARY.sampleNoticeAdd);
});

test("no sample book in the library: nothing", async () => {
  books = [book("a", "My Own Book")];
  await show();
  expect(screen.queryByRole("status", { name: LIBRARY.sampleNoticeLabel })).toBeNull();
});

test("Got it hides it for good in this browser", async () => {
  await show();
  await userEvent.click(screen.getByRole("button", { name: LIBRARY.sampleNoticeDismiss }));
  expect(screen.queryByRole("status", { name: LIBRARY.sampleNoticeLabel })).toBeNull();
  expect(localStorage.getItem(SAMPLE_NOTICE_KEY)).toBe("1");
  await show();
  expect(screen.queryByRole("status", { name: LIBRARY.sampleNoticeLabel })).toBeNull();
  expect(fetch).toHaveBeenCalledTimes(1); // once dismissed, the library isn't even asked
});

test("Add a book opens Add book and puts the notice away", async () => {
  const onAdd = await show();
  await userEvent.click(screen.getByRole("button", { name: LIBRARY.sampleNoticeAddButton }));
  expect(onAdd).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("status", { name: LIBRARY.sampleNoticeLabel })).toBeNull();
});
