import { render, screen } from "@testing-library/react";
import { EntityCard } from "./EntityCard";
import type { Edge } from "../types";

vi.mock("../api", () => ({
  api: {
    entity: () => Promise.resolve({ id: "e1", name: "Jefferson Hope", kind: "Character", aliases: [], books: ["b1"],
      edges_out: [], edges_in: [], edges_out_total: 40, edges_in_total: 2, truncated: false }),
    subgraph: vi.fn(),
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
