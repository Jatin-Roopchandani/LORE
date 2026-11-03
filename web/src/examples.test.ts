import { exampleQuestions, plainName } from "./examples";
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
    "What happens in Baker Street?",
    "Who belongs to Scotland Yard?",
  ]);
});

test("skips templates whose kind is missing", () => {
  expect(exampleQuestions(data([node("h", "Holmes", "Character"), node("x", "A Pipe", "Object")])))
    .toEqual(["Tell me about Holmes"]);
  expect(exampleQuestions(data([node("p", "Baker Street", "Place")]))).toEqual(["What happens in Baker Street?"]);
});

test("no overview, no examples", () => {
  expect(exampleQuestions(null)).toEqual([]);
  expect(exampleQuestions(undefined)).toEqual([]);
  expect(exampleQuestions(data([]))).toEqual([]);
});

test("names lose a leading honorific and a trailing degree suffix", () => {
  expect(plainName("Mr. Sherlock Holmes")).toBe("Sherlock Holmes");
  expect(plainName("John H. Watson, M.D.")).toBe("John H. Watson");
  expect(plainName("Mrs. Hudson")).toBe("Hudson");
  expect(plainName("Miss Morstan")).toBe("Morstan");
  expect(plainName("Ms. Adler")).toBe("Adler");
  expect(plainName("Dr. Watson")).toBe("Watson");
  expect(plainName("Drebber")).toBe("Drebber");
  expect(plainName("Mr.")).toBe("Mr.");
  expect(exampleQuestions(data([node("h", "Mr. Sherlock Holmes", "Character"), node("w", "John H. Watson, M.D.", "Character")])))
    .toEqual(["How is Sherlock Holmes connected to John H. Watson?", "Tell me about Sherlock Holmes"]);
});
