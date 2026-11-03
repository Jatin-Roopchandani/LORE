import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { EXPLORE } from "../messages";
import { EdgeView } from "./EdgeView";

vi.mock("../api", () => ({ api: { edge: vi.fn(), passage: vi.fn() }, isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x }));
const m = (f: unknown) => f as ReturnType<typeof vi.fn>;
const zed = { id: "a_zed", name: "Zed Quill", kind: "Character" }, yan = { id: "a_yan", name: "Yan Vale", kind: "Character" };
const detail = { a: zed, b: yan, order_note: null, predicates: [{ predicate: "INTERACTS_WITH", count: 2, direction: "out" }, { predicate: "LEADS_TO", count: 1, direction: "in" }],
  relationships: [
    { subject: zed, object: yan, predicate: "INTERACTS_WITH", predicate_raw: "met", quote: "Zed met Yan at the gate", chunk_id: "ba1:1:0", book_id: "ba1", book_title: "Alpha One", chapter: 1, position_label: "Alpha One, chapter 1" },
    { subject: yan, object: zed, predicate: "LEADS_TO", predicate_raw: "led", quote: "Yan led Zed home that night", chunk_id: "ba2:3:0", book_id: "ba2", book_title: "Alpha Two", chapter: 3, position_label: "Alpha Two, chapter 3" }] };
const titles = new Map([["ba1", "Alpha One"], ["ba2", "Alpha Two"]]);

test("shows both ends, every predicate and each piece of evidence in story order, in the world", async () => {
  m(api.edge).mockResolvedValue(detail);
  m(api.passage).mockResolvedValue({ chunk_id: "ba2:3:0", book_id: "ba2", book_title: "Alpha Two", chapter: 3, position_label: "x", text: "Yan led Zed home that night, quietly." });
  render(<EdgeView a="a_zed" b="a_yan" world="1" bookTitles={titles} onClose={() => {}} onSelect={() => {}} onAsk={() => {}} />);
  expect(await screen.findByRole("button", { name: EXPLORE.openEntity("Zed Quill") })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: EXPLORE.openEntity("Yan Vale") })).toBeInTheDocument();
  expect(api.edge).toHaveBeenCalledWith("a_zed", "a_yan", "1");
  expect(screen.getByText("interacts with ×2 →")).toBeInTheDocument();
  expect(screen.getByText("← leads to")).toBeInTheDocument();
  const statements = screen.getAllByText(/^(Zed Quill interacts with Yan Vale|Yan Vale leads to Zed Quill)$/).map((e) => e.textContent);
  expect(statements).toEqual(["Zed Quill interacts with Yan Vale", "Yan Vale leads to Zed Quill"]);
  expect(screen.getByText("Alpha Two, chapter 3")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: EXPLORE.chipLabel("Alpha Two", 3) }));
  expect(await screen.findByText("Yan led Zed home that night", { selector: "mark" })).toBeInTheDocument();
});

test("the relationship question is one click, without a depth", async () => {
  m(api.edge).mockResolvedValue(detail);
  const onAsk = vi.fn();
  render(<EdgeView a="a_zed" b="a_yan" onClose={() => {}} onSelect={() => {}} onAsk={onAsk} />);
  const q = "How does the relationship between Zed Quill and Yan Vale develop?";
  const b = await screen.findByRole("button", { name: q });
  expect(b).toHaveAttribute("title", EXPLORE.askCost);
  await userEvent.click(b);
  expect(onAsk).toHaveBeenCalledWith(q, undefined);
});

test("clicking an end opens that entity; a pair with no stored relationship says so", async () => {
  m(api.edge).mockResolvedValue({ ...detail, relationships: [], predicates: [] });
  const onSelect = vi.fn();
  render(<EdgeView a="a_zed" b="a_yan" onClose={() => {}} onSelect={onSelect} onAsk={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: EXPLORE.openEntity("Yan Vale") }));
  expect(onSelect).toHaveBeenCalledWith("a_yan");
  expect(screen.getByText(EXPLORE.noEdges)).toBeInTheDocument();
});
