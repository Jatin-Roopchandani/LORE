import { fireEvent, render, screen } from "@testing-library/react";
import { DEEP, POSITION } from "../messages";
import { AskBox } from "./AskBox";

test("the Deep dive toggle is a labelled radio group with Auto, On and Off", () => {
  render(<AskBox onAsk={() => {}} busy={false} depth="auto" onDepth={() => {}} />);
  const group = screen.getByRole("group", { name: DEEP.toggleLabel });
  expect(group).toBeInTheDocument();
  expect(screen.getAllByRole("radio").map((r) => r.parentElement?.textContent)).toEqual(["Auto", "On", "Off"]);
  expect(screen.getByRole("radio", { name: "Auto" })).toBeChecked();
});

test("choosing a state reports it", () => {
  const onDepth = vi.fn();
  render(<AskBox onAsk={() => {}} busy={false} depth="auto" onDepth={onDepth} />);
  fireEvent.click(screen.getByRole("radio", { name: "On" }));
  expect(onDepth).toHaveBeenCalledWith("deep");
  fireEvent.click(screen.getByRole("radio", { name: "Off" }));
  expect(onDepth).toHaveBeenLastCalledWith("focused");
});

test("the cost note shows only when the toggle is On", () => {
  const { rerender } = render(<AskBox onAsk={() => {}} busy={false} depth="auto" onDepth={() => {}} />);
  expect(screen.queryByText(DEEP.costNote)).toBeNull();
  rerender(<AskBox onAsk={() => {}} busy={false} depth="focused" onDepth={() => {}} />);
  expect(screen.queryByText(DEEP.costNote)).toBeNull();
  rerender(<AskBox onAsk={() => {}} busy={false} depth="deep" onDepth={() => {}} />);
  expect(screen.getByText(DEEP.costNote)).toBeInTheDocument();
  expect(screen.getByRole("group", { name: DEEP.toggleLabel })).toHaveAccessibleDescription(DEEP.costNote);
  expect(DEEP.costNote).toBe("A deep answer can take a few dozen requests to your AI provider.");
});

test("asking still works with the toggle present", () => {
  const onAsk = vi.fn();
  render(<AskBox onAsk={onAsk} busy={false} depth="auto" onDepth={() => {}} />);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: " who? " } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(onAsk).toHaveBeenCalledWith("who?");
});

// ---- the one-question spoiler override ------------------------------------------------------------------------------

test("the spoiler override is offered only while the reading progress holds something back", () => {
  const { rerender } = render(<AskBox onAsk={() => {}} busy={false} />);
  expect(screen.queryByRole("switch", { name: POSITION.spoilerToggle })).toBeNull();
  rerender(<AskBox onAsk={() => {}} busy={false} spoilerGuard />);
  expect(screen.getByRole("switch", { name: POSITION.spoilerToggle })).not.toBeChecked();
});

test("Show spoilers applies to the next question only, then switches itself off", () => {
  const onAsk = vi.fn();
  render(<AskBox onAsk={onAsk} busy={false} spoilerGuard />);
  const toggle = screen.getByRole("switch", { name: POSITION.spoilerToggle });
  fireEvent.click(toggle);
  expect(toggle).toBeChecked();
  expect(screen.getByRole("status")).toHaveTextContent(POSITION.spoilerOn);
  const input = screen.getByLabelText("Question");
  fireEvent.change(input, { target: { value: "Who is the killer?" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(onAsk).toHaveBeenLastCalledWith("Who is the killer?", true);
  expect(toggle).not.toBeChecked();
  fireEvent.change(input, { target: { value: "And then?" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(onAsk).toHaveBeenLastCalledWith("And then?");
});
