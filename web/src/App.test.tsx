import { render, screen } from "@testing-library/react";
import App from "./App";

test("renders the LORE mark", () => {
  render(<App />);
  expect(screen.getByText("LORE")).toBeInTheDocument();
});
