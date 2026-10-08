import { exampleQuestions } from "./examples";
import type { SubgraphData } from "./types";

const node = (id: string, name: string, kind: string) => ({ id, name, kind, books: ["b1"] });
const data = (nodes: ReturnType<typeof node>[]): SubgraphData => ({ nodes, edges: [], truncated: false, depth: 0 });

test("picks the most-connected entity of each kind, in overview order", () => {
  const q = exampleQuestions(data([node("p", "Baker Street", "Place"), node("h", "Holmes", "Character"),
    node("y", "Scotland Yard", "Faction"), node("w", "Watson", "Character"), node("l", "Lestrade", "Character"),
    node("p2", "Lauriston Gardens", "Place")]));
  expect(q).toEqual([
    "How is Holmes connected to Watson?",
    "Tell me about Holmes",
    "What happens at Baker Street?",
    "Who belongs to Scotland Yard?",
  ]);
});

test("skips templates whose kind is missing", () => {
  expect(exampleQuestions(data([node("h", "Holmes", "Character"), node("x", "A Pipe", "Object")])))
    .toEqual(["Tell me about Holmes"]);
  expect(exampleQuestions(data([node("p", "Baker Street", "Place")]))).toEqual(["What happens at Baker Street?"]);
});

test("no overview, no examples", () => {
  expect(exampleQuestions(null)).toEqual([]);
  expect(exampleQuestions(undefined)).toEqual([]);
  expect(exampleQuestions(data([]))).toEqual([]);
});
