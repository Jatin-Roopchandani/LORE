import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../api";
import { EXPLORE } from "../messages";
import { EntitySearch } from "./EntitySearch";

vi.mock("../api", () => ({ api: { search: vi.fn() }, isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x }));
const m = (f: unknown) => f as ReturnType<typeof vi.fn>;
const hits = [{ id: "a_wick", name: "Wick Orrin", kind: "Character", books: ["ba1"], matched_alias: "The Ferryman" },
  { id: "a_wickham", name: "Wickham Marsh", kind: "Place", books: ["ba1"], matched_alias: null }];
beforeEach(() => m(api.search).mockReset());

test("typing searches the world and lists the matches; keys move through them and Enter picks", async () => {
  m(api.search).mockResolvedValue({ results: hits });
  const onPick = vi.fn();
  render(<EntitySearch world="1" label="Find" placeholder="Find" onPick={onPick} />);
  const box = screen.getByRole("combobox", { name: "Find" });
  await userEvent.type(box, "wic");
  expect(await screen.findByRole("option", { name: /Wick Orrin/ })).toBeInTheDocument();
  expect(api.search).toHaveBeenLastCalledWith("wic", "1");
  expect(screen.getByText(/also called The Ferryman/)).toBeInTheDocument();
  expect(box).toHaveAttribute("aria-expanded", "true");
  await userEvent.keyboard("{ArrowDown}{ArrowDown}");
  expect(screen.getByRole("option", { name: /Wickham Marsh/ })).toHaveAttribute("aria-selected", "true");
  await userEvent.keyboard("{Enter}");
  expect(onPick).toHaveBeenCalledWith(hits[1]);
  expect(box).toHaveValue("");
});

test("clicking a result picks it; Escape closes the list; no match says so", async () => {
  m(api.search).mockResolvedValueOnce({ results: hits }).mockResolvedValueOnce({ results: [] });
  const onPick = vi.fn();
  render(<EntitySearch label="Find" placeholder="Find" onPick={onPick} />);
  const box = screen.getByRole("combobox", { name: "Find" });
  await userEvent.type(box, "wi");
  await userEvent.click(await screen.findByRole("option", { name: /Wick Orrin/ }));
  expect(onPick).toHaveBeenCalledWith(hits[0]);
  await userEvent.type(box, "zzz");
  expect(await screen.findByText(EXPLORE.noMatches)).toBeInTheDocument();
});

test("Escape closes the results", async () => {
  m(api.search).mockResolvedValue({ results: hits });
  render(<EntitySearch label="Find" placeholder="Find" onPick={() => {}} />);
  const box = screen.getByRole("combobox", { name: "Find" });
  await userEvent.type(box, "wi");
  await screen.findByRole("option", { name: /Wick Orrin/ });
  await userEvent.keyboard("{Escape}");
  expect(box).toHaveAttribute("aria-expanded", "false");
});
