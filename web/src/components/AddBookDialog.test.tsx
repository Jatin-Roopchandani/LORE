import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AddBookDialog } from "./AddBookDialog";
import { UPLOAD, uploadError } from "../messages";

// A stand-in for XMLHttpRequest: records what was sent, and the test answers it.
class FakeXhr {
  static last: FakeXhr | null = null;
  method = ""; url = ""; headers: Record<string, string> = {}; body: FormData | null = null;
  status = 0; responseText = ""; aborted = false;
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null; onerror: (() => void) | null = null; onabort: (() => void) | null = null;
  constructor() { FakeXhr.last = this; }
  open(method: string, url: string) { this.method = method; this.url = url; }
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send(body: FormData) { this.body = body; }
  abort() { this.aborted = true; this.onabort?.(); }
  progress(loaded: number, total: number) { this.upload.onprogress?.({ lengthComputable: true, loaded, total }); }
  reply(status: number, body: unknown) { this.status = status; this.responseText = typeof body === "string" ? body : JSON.stringify(body); this.onload?.(); }
}

const worlds = [{ id: 1, name: "Alpha Saga", books: [] }, { id: 2, name: "Beta Realm", books: [] }];
const ok = (o: object = {}) => ({ book_id: "b123", title: "The Book", world: { id: 7, name: "Gamma Isles" }, filename: "Gamma_Isles.epub",
  processing: { state: "processing" }, ...o });

beforeEach(() => {
  FakeXhr.last = null;
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  document.cookie = "lore_csrf=tok123";
});
afterEach(() => { vi.unstubAllGlobals(); document.cookie = "lore_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT"; });

const dialog = (extra = {}) => {
  const props = { onClose: vi.fn(), onUploaded: vi.fn(), onOpenBook: vi.fn(), ...extra };
  render(<AddBookDialog open worlds={worlds} {...props} />);
  return props;
};
const file = (name: string, size = 10) => new File(["x".repeat(size)], name);
const choose = async (name = "Gamma_Isles.epub") =>
  userEvent.upload(document.getElementById("add-file") as HTMLInputElement, file(name));
const upload = async () => userEvent.click(screen.getByRole("button", { name: UPLOAD.upload }));

test("the limits are shown before any file is chosen", () => {
  dialog();
  expect(screen.getByText("EPUB or PDF, up to 50 MB. Add only books you own.")).toBeInTheDocument();
});

test("the drop zone takes a dropped book and suggests its world", () => {
  dialog();
  const zone = screen.getByRole("button", { name: /Drop an EPUB or PDF/ });
  fireEvent.drop(zone, { dataTransfer: { files: [file("Alpha_Saga_3.epub")] } });
  expect(screen.getByText(/^Alpha_Saga_3\.epub · 1 KB$/)).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "World for this book" })).toHaveValue("1");
});

test("the picker takes a book; a wrong type or a file over 50 MB is refused before sending", async () => {
  dialog();
  const input = document.getElementById("add-file") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file("notes.txt")] } });  // past the picker's own filter
  expect(screen.getByRole("alert")).toHaveTextContent("notes.txt is not an EPUB or PDF");
  const big = new File(["x"], "huge.pdf");
  Object.defineProperty(big, "size", { value: 51 * 1024 * 1024 });
  fireEvent.change(input, { target: { files: [big] } });
  expect(screen.getByRole("alert")).toHaveTextContent("huge.pdf is larger than 50 MB.");
  expect(screen.queryByRole("button", { name: UPLOAD.upload })).toBeNull();
  expect(FakeXhr.last).toBeNull();
});

test("upload sends the file, a new world by name and the CSRF token; progress shows; success links to the book", async () => {
  const props = dialog();
  await choose();
  await upload();
  const xhr = FakeXhr.last!;
  expect([xhr.method, xhr.url]).toEqual(["POST", "/api/books/upload"]);
  expect(xhr.headers["X-CSRF-Token"]).toBe("tok123");
  expect(xhr.body!.get("world")).toBe("Gamma Isles");
  expect((xhr.body!.get("file") as File).name).toBe("Gamma_Isles.epub");
  act(() => xhr.progress(5, 10));
  const bar = screen.getByRole("progressbar", { name: "Upload progress" });
  expect(bar).toHaveAttribute("aria-valuenow", "50");
  expect(screen.getByRole("button", { name: UPLOAD.uploading })).toBeDisabled();
  act(() => xhr.progress(10, 10));
  expect(screen.getByText(UPLOAD.checking)).toBeInTheDocument();
  await act(async () => xhr.reply(202, ok()));
  expect(screen.getByText("Processing started")).toBeInTheDocument();
  expect(screen.getByText(UPLOAD.next.processing)).toBeInTheDocument();
  expect(props.onUploaded).toHaveBeenCalledWith(expect.objectContaining({ book_id: "b123" }));
  await userEvent.click(screen.getByRole("button", { name: UPLOAD.openLibrary }));
  expect(props.onOpenBook).toHaveBeenCalledWith("b123");
});

test("an existing world is sent by its id", async () => {
  dialog();
  await choose("Alpha_Saga_3.epub");
  await upload();
  expect(FakeXhr.last!.body!.get("world")).toBe("1");
});

test("a queued, busy or waiting book says what happens next", async () => {
  dialog();
  await choose();
  await upload();
  await act(async () => FakeXhr.last!.reply(202, ok({ processing: { state: "waiting", resumable_at: "2026-11-06T08:00:00Z" } })));
  expect(screen.getByText("Book added")).toBeInTheDocument();
  expect(screen.getByText(/Today's allowance is used up, so the book waits\. Press Continue in the Library after \d\d:\d\d/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: UPLOAD.another }));
  await choose();
  await upload();
  await act(async () => FakeXhr.last!.reply(202, ok({ processing: { state: "queued", job: 4 } })));
  expect(screen.getByText(UPLOAD.next.queued)).toBeInTheDocument();
});

const CODES = ["too_large", "not_a_book", "unsafe_file", "encrypted", "too_many_pages", "too_many_books", "daily_limit",
  "rate_limited", "busy", "key_required", "bad_upload", "unknown_world", "invalid_world"];

test.each(CODES)("the %s refusal has its own friendly message", async (code) => {
  dialog();
  await choose();
  await upload();
  await act(async () => FakeXhr.last!.reply(code === "too_large" ? 413 : 400, { error: { code, message: "server text" } }));
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent(uploadError({ code, message: "" }));
  expect(alert).not.toHaveTextContent("server text");
  expect(uploadError({ code, message: "" })).not.toBe(uploadError({ code: "something_else", message: "" }));
  // the user can try again
  expect(screen.getByRole("button", { name: UPLOAD.upload })).toBeEnabled();
});

test("already added links to the book that is there", async () => {
  const props = dialog();
  await choose();
  await upload();
  await act(async () => FakeXhr.last!.reply(409, { error: { code: "already_added", message: "" }, book_id: "old1" }));
  expect(screen.getByRole("alert")).toHaveTextContent("That book is already in your library.");
  await userEvent.click(screen.getByRole("button", { name: UPLOAD.openLibrary }));
  expect(props.onOpenBook).toHaveBeenCalledWith("old1");
});

test("a proxy's 413 page, a network failure and a refused page each read clearly", async () => {
  dialog();
  await choose();
  await upload();
  await act(async () => FakeXhr.last!.reply(413, "<html>Request Entity Too Large</html>"));
  expect(screen.getByRole("alert")).toHaveTextContent(uploadError({ code: "too_large", message: "" }));
  await upload();
  await act(async () => FakeXhr.last!.onerror!());
  expect(screen.getByRole("alert")).toHaveTextContent(uploadError({ code: "unreachable", message: "" }));
  await upload();
  await act(async () => FakeXhr.last!.reply(403, { detail: "missing or invalid CSRF token" }));
  expect(screen.getByRole("alert")).toHaveTextContent(uploadError({ code: "forbidden", message: "" }));
});

test("cancel stops the upload", async () => {
  dialog();
  await choose();
  await upload();
  const xhr = FakeXhr.last!;
  await userEvent.click(screen.getByRole("button", { name: UPLOAD.cancel }));
  expect(xhr.aborted).toBe(true);
  expect(screen.getByRole("alert")).toHaveTextContent("The upload was cancelled.");
});

test("the terminal command is a disclosure in local mode only", async () => {
  dialog();
  await choose();
  expect(screen.getByText(UPLOAD.terminal)).toBeInTheDocument();
  expect(screen.getByText("lore ingest 'Gamma_Isles.epub' --world 'Gamma Isles'")).toBeInTheDocument();
});

test("the multi-user app offers no terminal command", async () => {
  dialog({ multi: true });
  await choose();
  expect(screen.queryByText(UPLOAD.terminal)).toBeNull();
  expect(screen.queryByText(/lore ingest/)).toBeNull();
  expect(screen.getByRole("button", { name: UPLOAD.upload })).toBeInTheDocument();
});
