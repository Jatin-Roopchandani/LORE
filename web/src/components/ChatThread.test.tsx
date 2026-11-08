// The answer pane as a thread: earlier questions and answers above the latest, folded to one line until opened.
import { fireEvent, render, screen, within } from "@testing-library/react";
import single from "../fixtures/single-part.json";
import { askReducer, initialState } from "../askReducer";
import type { ThreadEntry } from "../history";
import { CHAT, UI } from "../messages";
import type { AskEvent } from "../types";
import { ChatPane, MAX_SHOWN } from "./ChatPane";

const events = single as AskEvent[];
const entry = (n: number, extra: Partial<ThreadEntry> = {}): ThreadEntry => ({ key: `m${n}`, question: `Question ${n}?`, standalone: null, events, ...extra });
const askWith = (thread: ThreadEntry[], latest: AskEvent[] = events, question = "Latest?") => ({
  state: latest.reduce(askReducer, initialState(question)), thread, run: 1,
  ask: vi.fn(), retry: vi.fn(), clear: vi.fn(), depth: "auto" as const, setDepth: vi.fn(), busy: false });
const pane = (ask: ReturnType<typeof askWith>) => render(<ChatPane ask={ask} showInternals onHoverChunks={() => {}} />);

test("earlier questions stack above the latest, collapsed, with the latest answer open", () => {
  pane(askWith([entry(1), entry(2)]));
  const earlier = screen.getByRole("region", { name: CHAT.earlierLabel });
  const questions = [...document.querySelectorAll(".q")].map((q) => q.textContent);
  expect(questions).toEqual(["Question 1?", "Question 2?", "Latest?"]);
  const toggles = within(earlier).getAllByRole("button", { expanded: false });
  expect(toggles).toHaveLength(2);
  expect(toggles[0]).toHaveAccessibleName(CHAT.expand("Question 1?"));
  // Collapsed turns draw no answer; only the latest's citations are on screen.
  expect(within(earlier).queryAllByRole("button", { name: /ch\.\d+/ })).toHaveLength(0);
  expect(screen.getAllByRole("button", { name: /ch\.\d+/ }).length).toBeGreaterThan(0);
});

test("an older answer opens and folds again, and its citation chips still work", () => {
  pane(askWith([entry(1)]));
  const earlier = screen.getByRole("region", { name: CHAT.earlierLabel });
  fireEvent.click(within(earlier).getByRole("button", { name: CHAT.expand("Question 1?") }));
  const cite = within(earlier).getAllByRole("button", { name: /ch\.\d+/ })[0];
  fireEvent.click(cite);
  expect(cite).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(within(earlier).getByRole("button", { name: CHAT.collapse("Question 1?") }));
  expect(within(earlier).queryAllByRole("button", { name: /ch\.\d+/ })).toHaveLength(0);
});

test("Understood as shows under a follow-up the server rewrote, and only then", () => {
  const latest = [{ type: "understood", t: 0, standalone: "What did Ren Ashby do next?" } as AskEvent, ...events];
  pane(askWith([entry(1), entry(2, { standalone: "Where did Ren Ashby go?" })], latest));
  const notes = [...document.querySelectorAll(".understood")].map((n) => n.textContent);
  expect(notes).toEqual([`${CHAT.understoodAs} Where did Ren Ashby go?`, `${CHAT.understoodAs} What did Ren Ashby do next?`]);
});

test("no Understood as when the question was answered as typed", () => {
  pane(askWith([]));
  expect(screen.queryByText(CHAT.understoodAs)).toBeNull();
});

test("the box asks for a follow-up inside a chat and about your books in a new one", () => {
  const { unmount } = pane(askWith([]));
  expect(screen.getByPlaceholderText(CHAT.followupPlaceholder)).toBeInTheDocument();
  unmount();
  pane({ ...askWith([]), state: initialState() });
  expect(screen.getByPlaceholderText(UI.askPlaceholder)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: CHAT.newChat })).toBeNull();
});

test("New chat starts a fresh conversation", () => {
  const ask = askWith([entry(1)]);
  pane(ask);
  fireEvent.click(screen.getByRole("button", { name: CHAT.newChat }));
  expect(ask.clear).toHaveBeenCalled();
});

test("a submitted follow-up goes to ask", () => {
  const ask = askWith([entry(1)]);
  pane(ask);
  fireEvent.change(screen.getByPlaceholderText(CHAT.followupPlaceholder), { target: { value: "And then?" } });
  fireEvent.submit(screen.getByPlaceholderText(CHAT.followupPlaceholder).closest("form")!);
  expect(ask.ask).toHaveBeenCalledWith("And then?");
});

test("a long thread draws about 20 answers, and show earlier draws more", () => {
  const thread = Array.from({ length: 45 }, (_, i) => entry(i + 1));
  pane(askWith(thread));
  const earlier = screen.getByRole("region", { name: CHAT.earlierLabel });
  expect(earlier.querySelectorAll(".earlier-turn")).toHaveLength(MAX_SHOWN - 1);
  expect(earlier.querySelector(".q")!.textContent).toBe("Question 27?");
  fireEvent.click(screen.getByRole("button", { name: CHAT.showEarlier(MAX_SHOWN) }));
  expect(earlier.querySelectorAll(".earlier-turn")).toHaveLength(MAX_SHOWN * 2 - 1);
  fireEvent.click(screen.getByRole("button", { name: CHAT.showEarlier(6) }));
  expect(earlier.querySelectorAll(".earlier-turn")).toHaveLength(45);
  expect(screen.queryByRole("button", { name: /Show \d+ earlier/ })).toBeNull();
});
