import { fireEvent, render, screen } from "@testing-library/react";
import { DEEP } from "../messages";
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
  expect(DEEP.costNote).toBe("A deep answer can take a few dozen requests from the daily allowance.");
});

test("asking still works with the toggle present", () => {
  const onAsk = vi.fn();
  render(<AskBox onAsk={onAsk} busy={false} depth="auto" onDepth={() => {}} />);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: " who? " } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(onAsk).toHaveBeenCalledWith("who?");
});
