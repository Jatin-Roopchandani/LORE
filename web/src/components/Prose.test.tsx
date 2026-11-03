import { fireEvent, render, screen } from "@testing-library/react";
import { Prose } from "./Prose";
import { UI } from "../messages";
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

test("gaps join the unanswered parts in the one muted line", () => {
  const { container } = render(
    <Prose result={{ ...result, unanswered: ["How did he lose it?"], gaps: ["whether he used it later"] }} onHoverChunks={() => {}} />);
  const lines = container.querySelectorAll("p.unanswered");
  expect(lines).toHaveLength(1);
  expect(lines[0].textContent).toBe("The passages found don't answer: How did he lose it?; whether he used it later");
});

test("gaps alone show the same muted line", () => {
  const { container } = render(<Prose result={{ ...result, gaps: ["whether he used it later"] }} onHoverChunks={() => {}} />);
  expect(container.querySelector("p.unanswered")?.textContent).toBe("The passages found don't answer: whether he used it later");
});

test("an empty gaps list adds no line", () => {
  const { container } = render(<Prose result={{ ...result, gaps: [] }} onHoverChunks={() => {}} />);
  expect(container.querySelector(".unanswered")).toBeNull();
});

const withReading = {
  ...result,
  prose: [...result.prose!, { text: "This suggests he never forgave.", claims: [1, 2], passages: [3, 5], new_paragraph: false, kind: "reading" as const }],
} as SearchResult;

// The note a screen reader gets for a reading, a counter-case or a limit, found as its sentence points to it.
const described = (container: HTMLElement, el: Element) => {
  const id = el.getAttribute("aria-describedby");
  return id ? container.querySelector(`[id="${id}"]`) : null;
};

test("a reading is marked, with a tooltip and a note for screen readers that points to one hidden description", () => {
  const { container } = render(<Prose result={withReading} onHoverChunks={() => {}} />);
  const readings = container.querySelectorAll(".sentence.reading");
  expect(readings).toHaveLength(1);
  expect(readings[0].textContent).toContain("This suggests he never forgave.");
  expect(readings[0]).toHaveAttribute("title", UI.readingNote);
  expect(described(container, readings[0])?.textContent).toBe(UI.readingNote);
  expect(container.querySelector(".sentence .visually-hidden")).toBeNull();
});

test("copying a reading does not copy the accessibility note", () => {
  const { container } = render(<Prose result={withKinds} onHoverChunks={() => {}} />);
  for (const para of container.querySelectorAll("p.prose")) {
    for (const note of [UI.readingNote, UI.counterNote, UI.limitNote]) expect(para.textContent).not.toContain(note);
  }
  const reading = container.querySelector("p.prose.reading")!;
  expect(reading.textContent).toBe(`${UI.kindLabel.reading}On balance he never forgave. [3][5] `);
  // The notes are still there for assistive technology, once per kind and outside the sentences (and hidden,
  // so a selection across the answer does not pick them up).
  const note = described(container, container.querySelector(".sentence.reading")!)!;
  expect(note).toHaveAttribute("hidden");
  expect(note.closest("p")).toBeNull();
  expect(container.querySelectorAll("[hidden]")).toHaveLength(3);
});

test("two sentences of one kind share one description", () => {
  const two = { ...result, prose: [...result.prose!,
    { text: "A reading.", claims: [1, 2], passages: [3], new_paragraph: true, kind: "reading" as const },
    { text: "Another reading.", claims: [1, 2], passages: [3], new_paragraph: false, kind: "reading" as const }] } as SearchResult;
  const { container } = render(<Prose result={two} onHoverChunks={() => {}} />);
  const [a, b] = container.querySelectorAll(".sentence.reading");
  expect(a.getAttribute("aria-describedby")).toBe(b.getAttribute("aria-describedby"));
  expect(container.querySelectorAll("[hidden]")).toHaveLength(1);
});

test("two answers on a page do not share description ids", () => {
  const first = render(<Prose result={withReading} onHoverChunks={() => {}} />);
  const second = render(<Prose result={withReading} onHoverChunks={() => {}} />);
  const ids = [first, second].map((r) => r.container.querySelector(".sentence.reading")!.getAttribute("aria-describedby"));
  expect(ids[0]).not.toBe(ids[1]);
});

test("facts, and sentences with no kind, are not marked as readings", () => {
  const { container } = render(<Prose result={result} onHoverChunks={() => {}} />);
  expect(container.querySelectorAll(".sentence")).toHaveLength(2);
  expect(container.querySelector(".reading")).toBeNull();
  expect(container.querySelector("[title]")).toBeNull();
  const explicit = { ...result, prose: result.prose!.map((s) => ({ ...s, kind: "fact" as const })) } as SearchResult;
  const again = render(<Prose result={explicit} onHoverChunks={() => {}} />);
  expect(again.container.querySelector(".reading")).toBeNull();
});

test("a reading still has its citation chips and hover", () => {
  const onHover = vi.fn();
  render(<Prose result={withReading} onHoverChunks={onHover} />);
  fireEvent.mouseEnter(screen.getByText(/never forgave/));
  expect(onHover).toHaveBeenCalledWith(["c3", "c5"]);
  expect(screen.getAllByRole("button", { name: /\[5\]/ })).toHaveLength(2);
});

const withKinds = {
  ...result,
  prose: [...result.prose!,
    { text: "On balance he never forgave.", claims: [1, 2], passages: [3, 5], new_paragraph: false, kind: "reading" as const },
    { text: "Yet he spared Lucy's father once.", claims: [1], passages: [3], new_paragraph: false, kind: "counter" as const },
    { text: "The books don't settle: what became of the ring.", claims: [], passages: [], new_paragraph: false, kind: "limit" as const }],
} as SearchResult;

test("a reading, the other side and a limit each get a label, a style and a tooltip", () => {
  const { container } = render(<Prose result={withKinds} onHoverChunks={() => {}} />);
  const labels = [...container.querySelectorAll(".kind-label")].map((n) => n.textContent);
  expect(labels).toEqual(["LORE's reading", "The other side", "What the books don't settle"]);
  expect(labels).toEqual([UI.kindLabel.reading, UI.kindLabel.counter, UI.kindLabel.limit]);
  expect(container.querySelector(".sentence.reading")).toHaveAttribute("title", UI.readingNote);
  expect(container.querySelector(".sentence.counter")).toHaveAttribute("title", UI.counterNote);
  expect(container.querySelector(".sentence.limit")).toHaveAttribute("title", UI.limitNote);
  expect(described(container, container.querySelector(".sentence.counter")!)?.textContent).toBe(UI.counterNote);
  expect(described(container, container.querySelector(".sentence.limit")!)?.textContent).toBe(UI.limitNote);
  expect(container.querySelector("p.prose.limit")).not.toBeNull();   // the muted line
});

test("each kind opens its own paragraph so the label sits at the start of its run", () => {
  const { container } = render(<Prose result={withKinds} onHoverChunks={() => {}} />);
  expect([...container.querySelectorAll("p.prose")].map((p) => p.className)).toEqual(["prose", "prose", "prose reading", "prose counter", "prose limit"]);
});

test("a counter keeps its citation chips and hover; a limit has none", () => {
  const onHover = vi.fn();
  const { container } = render(<Prose result={withKinds} onHoverChunks={onHover} />);
  fireEvent.mouseEnter(screen.getByText(/spared Lucy/));
  expect(onHover).toHaveBeenCalledWith(["c3"]);
  expect(container.querySelector(".sentence.limit .cite")).toBeNull();
  expect(container.querySelector(".sentence.counter .cite")).not.toBeNull();
});

test("an old saved judgement is shown as a reading", () => {
  const old = { ...result, prose: [{ ...result.prose![0], kind: "judgement" }] } as unknown as SearchResult;
  const { container } = render(<Prose result={old} onHoverChunks={() => {}} />);
  expect(container.querySelector(".sentence.reading")).not.toBeNull();
  expect(container.querySelector(".judgement")).toBeNull();
});

test("a section's own sentences are cited from the result and show no unanswered line", () => {
  const { container } = render(<Prose result={{ ...result, unanswered: ["x"] }} prose={[result.prose![1]]} showMissing={false} onHoverChunks={() => {}} />);
  expect(container.querySelectorAll("p.prose")).toHaveLength(1);
  expect(container.querySelector(".unanswered")).toBeNull();
  expect(screen.getByRole("button", { name: /\[3\]/ })).toBeInTheDocument();
});

// A sentence that rests on many passages shows four chips, the ones whose claims fit it best, and a +N chip.
const many = (() => {
  const topics = ["marble quarry", "silver ferry", "tin harbour", "glass orchard", "salt bridge", "wool tower"];
  const claims = topics.map((t, i) => ({ text: `The ${t} opened in year ${i}.`, passage: i + 1, quote: `${t} opened` }));
  return {
    ...result, claims,
    passages: topics.map((t, i) => ({ n: i + 1, chunk_id: `k${i + 1}`, book_id: "b9", book_title: "T", chapter_seq: 1, text: `The ${t} opened.` })),
    prose: [{ text: "The salt bridge and the wool tower opened late.", claims: [1, 2, 3, 4, 5, 6], passages: [1, 2, 3, 4, 5, 6], new_paragraph: false }],
  } as SearchResult;
})();

test("a sentence shows at most four citation chips, the best-fitting ones, with a +N chip", () => {
  render(<Prose result={many} onHoverChunks={() => {}} />);
  const chips = screen.getAllByRole("button", { name: /^\[\d\]$/ }).map((b) => b.textContent);
  expect(chips).toHaveLength(4);
  expect(chips).toContain("[5]");
  expect(chips).toContain("[6]");   // the claims about the bridge and the tower fit the sentence best
  expect(screen.getByRole("button", { name: "+2" })).toBeInTheDocument();
});

test("the +N chip shows the rest, and hovering still reports every cited passage", () => {
  const onHover = vi.fn();
  render(<Prose result={many} onHoverChunks={onHover} />);
  fireEvent.mouseEnter(screen.getByText(/salt bridge and the wool tower/));
  expect(onHover).toHaveBeenCalledWith(["k1", "k2", "k3", "k4", "k5", "k6"]);
  fireEvent.click(screen.getByRole("button", { name: "+2" }));
  expect(screen.getAllByRole("button", { name: /^\[\d\]$/ })).toHaveLength(6);
  expect(screen.queryByRole("button", { name: "+2" })).toBeNull();
});
