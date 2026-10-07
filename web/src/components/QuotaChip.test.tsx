import { render, screen } from "@testing-library/react";
import { QuotaChip } from "./QuotaChip";

const q = (remaining: number) => ({ provider: "gemini" as const, resets_at: "x",
  models: [{ model: "a", count: 500 - remaining, limit: 500, remaining }, { model: "b", count: 500, limit: 500, remaining: 0 }] });

test("shows remaining over limit and warns when low", () => {
  render(<QuotaChip quota={q(40)} showInternals />);
  const chip = screen.getByText("Gemini: 40 / 1000 left");
  expect(chip).toHaveClass("warn");
});

test("hidden for local providers or without internals", () => {
  const { container, rerender } = render(<QuotaChip quota={{ provider: "local", models: [], resets_at: "" }} showInternals />);
  expect(container).toBeEmptyDOMElement();
  rerender(<QuotaChip quota={q(400)} showInternals={false} />);
  expect(container).toBeEmptyDOMElement();
});
