import { fireEvent, render, screen } from "@testing-library/react";
import Router from "../Router";
import { SIGNIN_LINK } from "../messages";
import { ContinueSignIn } from "./ContinueSignIn";

test("the mailed link's page posts the token only when the button is pressed", () => {
  const { container } = render(<ContinueSignIn search="?token=abc.def" />);
  const button = screen.getByRole("button", { name: SIGNIN_LINK.button });
  const form = button.closest("form")!;
  expect(form).toHaveAttribute("method", "post");
  expect(form).toHaveAttribute("action", "/auth/email/verify");
  expect(container.querySelector('input[name="token"]')).toHaveValue("abc.def");
  expect(screen.getByText(SIGNIN_LINK.lead)).toBeInTheDocument();
  expect(screen.getByText(SIGNIN_LINK.why)).toBeInTheDocument();
  expect(container.querySelector(".au .au-brand")).not.toBeNull(); // the split frame
});

test("pressing the button shows it is working (the browser then posts the form)", () => {
  render(<ContinueSignIn search="?token=abc.def" />);
  const button = screen.getByRole("button", { name: SIGNIN_LINK.button });
  const form = button.closest("form")!;
  form.addEventListener("submit", (e) => e.preventDefault()); // jsdom cannot navigate
  fireEvent.submit(form);
  expect(button).toHaveAttribute("aria-busy", "true");
  expect(button).toBeDisabled();
  fireEvent(window, new Event("pageshow")); // back from history: usable again
  expect(button).toBeEnabled();
});

test("without a token it says so and offers a new link", () => {
  render(<ContinueSignIn search="" />);
  expect(screen.getByRole("alert")).toHaveTextContent(SIGNIN_LINK.missing);
  expect(screen.getByRole("link", { name: SIGNIN_LINK.again })).toHaveAttribute("href", "/signin");
  expect(screen.queryByRole("button")).toBeNull();
});

test("the router draws it at /auth/email/verify in multi-user mode", () => {
  render(<Router pathname="/auth/email/verify" multi />);
  expect(screen.getByRole("heading", { name: SIGNIN_LINK.title })).toBeInTheDocument();
});
