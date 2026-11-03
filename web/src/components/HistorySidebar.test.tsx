import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { HistoryItem } from "../history";
import { HISTORY } from "../messages";
import { HistorySidebar } from "./HistorySidebar";

const now = new Date(2026, 10, 3, 15, 30);
const at = (days: number, h = 10, m = 42) => new Date(2026, 10, 3 - days, h, m).toISOString();
const result = { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: [], claims: [], passages: [], edges: [], seeds: [], answer_style: "sectioned" as const };
const items: HistoryItem[] = [
  { id: "1", question: "Who is Zed Quill and why does the marsh matter so much to the ferrymen of the old crossing?", askedAt: at(0), world: "2",
    events: [{ type: "answer", t: 0, result }] },
  { id: "2", question: "Where is the marsh?", askedAt: at(1), world: "1", events: [] },
  { id: "3", question: "Old question", askedAt: at(20), events: [] }, // saved before worlds: no world
];
const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];
const handlers = () => ({ onReplay: vi.fn(), onDelete: vi.fn(), onClear: vi.fn(), onNew: vi.fn(), onClose: vi.fn() });
const show = (h = handlers(), extra: Partial<React.ComponentProps<typeof HistorySidebar>> = {}) => {
  render(<HistorySidebar items={items} current="2" worlds={worlds} now={now} {...h} {...extra} />);
  return h;
};

test("entries sit under Today, Yesterday and Earlier with the time, a world badge and a deep marker", () => {
  show();
  expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([HISTORY.groups.today, HISTORY.groups.yesterday, HISTORY.groups.earlier]);
  const today = screen.getByRole("region", { name: HISTORY.groups.today });
  expect(within(today).getByText("10:42")).toBeInTheDocument();
  expect(within(today).getByText("Beta Realm")).toBeInTheDocument();
  expect(within(today).getByText(HISTORY.deep)).toBeInTheDocument();
  const yesterday = screen.getByRole("region", { name: HISTORY.groups.yesterday });
  expect(within(yesterday).getByText("Yesterday", { selector: ".mono" })).toBeInTheDocument();
  expect(within(yesterday).getByText("Alpha Saga")).toBeInTheDocument();
  expect(within(yesterday).queryByText(HISTORY.deep)).toBeNull();
  // An old item with no world still reads fine: no badge.
  const earlier = screen.getByRole("region", { name: HISTORY.groups.earlier });
  expect(within(earlier).getByText("14 Oct")).toBeInTheDocument();
  expect(within(earlier).getByText("Old question")).toBeInTheDocument();
});

test("the full question is the tooltip and the current entry is marked", () => {
  show();
  const long = screen.getByRole("button", { name: items[0].question });
  expect(long).toHaveAttribute("title", items[0].question);
  expect(screen.getByRole("button", { name: "Where is the marsh?" })).toHaveAttribute("aria-current", "true");
  expect(long).not.toHaveAttribute("aria-current");
});

test("search filters by question text and says when nothing matches", async () => {
  show();
  await userEvent.type(screen.getByRole("searchbox", { name: HISTORY.search }), "marsh");
  expect(screen.getAllByRole("button", { name: /marsh/ }).filter((b) => b.classList.contains("hist-item"))).toHaveLength(2);
  expect(screen.queryByText("Old question")).toBeNull();
  await userEvent.type(screen.getByRole("searchbox", { name: HISTORY.search }), "zzz");
  expect(screen.getByText(HISTORY.noMatch("marshzzz"))).toBeInTheDocument();
});

test("clicking an entry replays it", async () => {
  const h = show();
  await userEvent.click(screen.getByRole("button", { name: "Old question" }));
  expect(h.onReplay).toHaveBeenCalledWith("3");
});

test("each entry has a delete button naming its question", async () => {
  const h = show();
  await userEvent.click(screen.getByRole("button", { name: HISTORY.delete("Where is the marsh?") }));
  expect(h.onDelete).toHaveBeenCalledWith("2");
  expect(h.onReplay).not.toHaveBeenCalled();
});

test("Clear history asks first; Keep backs out and Clear clears", async () => {
  const h = show();
  await userEvent.click(screen.getByRole("button", { name: HISTORY.clear }));
  const box = screen.getByRole("group", { name: HISTORY.clearConfirmLabel });
  expect(within(box).getByText(HISTORY.clearConfirm(3))).toBeInTheDocument();
  expect(h.onClear).not.toHaveBeenCalled();
  await userEvent.click(within(box).getByRole("button", { name: HISTORY.clearNo }));
  expect(h.onClear).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: HISTORY.clear }));
  await userEvent.click(screen.getByRole("button", { name: HISTORY.clearYes }));
  expect(h.onClear).toHaveBeenCalledTimes(1);
});

test("New question is a button; an empty history says what will appear and offers no clear or search", async () => {
  const h = handlers();
  render(<HistorySidebar items={[]} current={null} now={now} {...h} />);
  expect(screen.getByText(HISTORY.empty)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: HISTORY.clear })).toBeNull();
  expect(screen.queryByRole("searchbox")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: HISTORY.newQuestion }));
  expect(h.onNew).toHaveBeenCalled();
});

test("as a drawer it is a modal dialog: focus starts inside, Tab wraps, Escape closes", async () => {
  const h = handlers();
  render(<><button>outside</button><HistorySidebar items={items} current={null} worlds={worlds} now={now} drawer {...h} /></>);
  const dialog = screen.getByRole("dialog", { name: HISTORY.title });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  const close = within(dialog).getByRole("button", { name: HISTORY.close });
  expect(close).toHaveFocus();
  await userEvent.tab({ shift: true }); // from the first control back to the last, not out to the page
  expect(dialog.contains(document.activeElement)).toBe(true);
  expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: HISTORY.clear }));
  await userEvent.tab();
  expect(close).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(h.onClose).toHaveBeenCalled();
});

test("the column version is a plain landmark, not a modal", () => {
  show();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("complementary", { name: HISTORY.title })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: HISTORY.close })).toBeNull();
});
