import { render, screen } from "@testing-library/react";
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
