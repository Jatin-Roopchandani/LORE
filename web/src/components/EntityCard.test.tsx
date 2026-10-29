import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { EntityCard } from "./EntityCard";
import type { Edge } from "../types";

vi.mock("../api", () => ({
  api: {
    entity: vi.fn((_id: string, _world?: string) => Promise.resolve({ id: "e1", name: "Jefferson Hope", kind: "Character", aliases: [], books: ["b1"],
      edges_out: [], edges_in: [], edges_out_total: 40, edges_in_total: 2, truncated: false })),
    subgraph: vi.fn((_id: string, _world?: string) => Promise.resolve({ nodes: [], edges: [], truncated: false, depth: 1 })),
  },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));

const edge = (predicate: string, chapter_seq: number, chunk_id: string): Edge => ({ subject: "Jefferson Hope", subject_id: "e1",
  predicate, predicate_raw: predicate, object: "John Ferrier", object_id: "e2", evidence: "", confidence: 1,
  book_id: "b1", chapter_seq, chunk_id });

test("shows books, the relationships in this answer with counts, and chapters", async () => {
  render(<EntityCard id="e1" onClose={() => {}} onExpand={() => {}} seed
    answerEdges={[edge("FRIEND_OF", 17, "c1"), edge("FRIEND_OF", 3, "c2")]} bookTitles={new Map([["b1", "A Study in Scarlet"]])} />);
  expect(await screen.findByText("Jefferson Hope")).toBeInTheDocument();
  expect(screen.getByText("Character · starting entity")).toBeInTheDocument();
  expect(screen.getByText("A Study in Scarlet")).toBeInTheDocument();
  expect(screen.getByText("friend of · John Ferrier")).toBeInTheDocument();
  expect(screen.getByText("×2")).toBeInTheDocument();
  expect(screen.getByText("Chapters: 3, 17")).toBeInTheDocument();
  expect(screen.getByText("42 relationships in the graph")).toBeInTheDocument();
});

test("an expand result that lands after the card moved on is dropped", async () => {
  const { api } = await import("../api");
  let land: (v: unknown) => void = () => {};
  (api.subgraph as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise((r) => { land = r; }));
  const onExpand = vi.fn();
  const { rerender } = render(<EntityCard id="e1" onClose={() => {}} onExpand={onExpand} />);
  (await screen.findByRole("button", { name: "Expand neighbours" })).click();
  rerender(<EntityCard id="e2" onClose={() => {}} onExpand={onExpand} />);
  land({ nodes: [], edges: [], truncated: false, depth: 1 });
  await new Promise((r) => setTimeout(r, 20));
  expect(onExpand).not.toHaveBeenCalled();
});

test("the card and its expansion are fetched in the selected world", async () => {
  render(<EntityCard id="e1" world="2" onClose={() => {}} onExpand={() => {}} />);
  await screen.findByText("Jefferson Hope");
  expect(api.entity).toHaveBeenLastCalledWith("e1", "2");
  await userEvent.click(screen.getByRole("button", { name: "Expand neighbours" }));
  await waitFor(() => expect(api.subgraph).toHaveBeenLastCalledWith("e1", "2"));
});
