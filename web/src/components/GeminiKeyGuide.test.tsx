import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GEMINI_GUIDE, geminiKeyHint } from "../messages";
import { GeminiKeyGuide, GeminiKeyHint } from "./GeminiKeyGuide";
import { ProviderKeyForm } from "./ProviderSettings";

test("collapsed at first; the toggle opens the four steps and closes them again", async () => {
  render(<GeminiKeyGuide />);
  const details = screen.getByText(GEMINI_GUIDE.toggle).closest("details")!;
  expect(details).not.toHaveAttribute("open");
  await userEvent.click(screen.getByText(GEMINI_GUIDE.toggle));
  expect(details).toHaveAttribute("open");
  const steps = screen.getByRole("list", { name: GEMINI_GUIDE.stepsLabel });
  expect(steps.querySelectorAll("li")).toHaveLength(4);
  expect(steps).toHaveTextContent(GEMINI_GUIDE.create);
  expect(screen.getByText(GEMINI_GUIDE.reassure)).toBeInTheDocument();
  await userEvent.click(screen.getByText(GEMINI_GUIDE.toggle));
  expect(details).not.toHaveAttribute("open");
});

test("the AI Studio link opens in a new tab, safely", () => {
  render(<GeminiKeyGuide />);
  const link = screen.getByRole("link", { name: GEMINI_GUIDE.open, hidden: true });
  expect(link).toHaveAttribute("href", "https://aistudio.google.com/apikey");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
});

test("a value that doesn't look like a Gemini key gets a hint, never a block", async () => {
  expect(geminiKeyHint("")).toBeNull();
  expect(geminiKeyHint("AIzaSyFAKE")).toBeNull();
  expect(geminiKeyHint("sk-ant-123")).toBe(GEMINI_GUIDE.notAGeminiKey);
  const { rerender } = render(<GeminiKeyHint value="sk-ant-123" />);
  expect(screen.getByText(GEMINI_GUIDE.notAGeminiKey)).toBeInTheDocument();
  rerender(<GeminiKeyHint value="AIzaSyFAKE" />);
  expect(screen.queryByText(GEMINI_GUIDE.notAGeminiKey)).toBeNull();
});

test("Settings' Gemini key form has the guide and the hint, and Save still works with an odd value", async () => {
  render(<ProviderKeyForm provider="gemini" onSaved={() => {}} />);
  expect(screen.getByText(GEMINI_GUIDE.toggle)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(/API key$/), "not-a-key");
  expect(screen.getByText(GEMINI_GUIDE.notAGeminiKey)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /save/i })).toBeEnabled();
});

test("another provider's key form has no Gemini guide", () => {
  render(<ProviderKeyForm provider="anthropic" onSaved={() => {}} />);
  expect(screen.queryByText(GEMINI_GUIDE.toggle)).toBeNull();
});
