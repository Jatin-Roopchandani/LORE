import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { REPORT } from "../messages";
import { ReportAnswer } from "./ReportAnswer";

const report = vi.fn((_rid: string, _note: string) => Promise.resolve({ report: { id: 1, status: "open" } } as object));
vi.mock("../chatsApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("../chatsApi")>()),
  chatsApi: { report: (rid: string, note: string) => report(rid, note) } }));

beforeEach(() => report.mockClear());

test("the link opens a dialog; sending posts the request id and the note, then thanks", async () => {
  render(<ReportAnswer requestId="req-1" />);
  expect(screen.queryByRole("dialog")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: REPORT.link }));
  expect(screen.getByRole("dialog", { name: REPORT.title })).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(REPORT.noteLabel), "wrong book");
  await userEvent.click(screen.getByRole("button", { name: REPORT.send }));
  expect(report).toHaveBeenCalledWith("req-1", "wrong book");
  expect(await screen.findByText(REPORT.thanks)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("cancel closes it without sending; a failure says so and keeps the dialog", async () => {
  render(<ReportAnswer requestId="req-2" />);
  await userEvent.click(screen.getByRole("button", { name: REPORT.link }));
  await userEvent.click(screen.getByRole("button", { name: REPORT.cancel }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(report).not.toHaveBeenCalled();
  report.mockResolvedValueOnce({ error: { code: "not_found", message: "" } });
  await userEvent.click(screen.getByRole("button", { name: REPORT.link }));
  await userEvent.click(screen.getByRole("button", { name: REPORT.send }));
  expect(await screen.findByRole("alert")).toHaveTextContent(REPORT.failed);
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});
