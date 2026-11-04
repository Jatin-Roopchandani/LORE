import { isMulti, pageFor } from "./routing";

test("local mode has one page, the app, whatever the address", () => {
  for (const p of ["/", "/app", "/admin", "/signin", "/whatever"]) expect(pageFor(p, false)).toBe("app");
});

test("multi-user mode: the address picks the page", () => {
  expect(pageFor("/", true)).toBe("home");
  expect(pageFor("/app", true)).toBe("app");
  expect(pageFor("/signin", true)).toBe("signin");
  expect(pageFor("/waitlist", true)).toBe("waitlist");
  expect(pageFor("/admin", true)).toBe("admin");
  expect(pageFor("/privacy", true)).toBe("privacy");
  expect(pageFor("/terms", true)).toBe("terms");
  expect(pageFor("/nope", true)).toBe("notfound");
});

test("a trailing slash does not matter", () => {
  expect(pageFor("/signin/", true)).toBe("signin");
  expect(pageFor("/app//", true)).toBe("app");
});

test("the server's meta tag marks multi-user mode", () => {
  const doc = document.implementation.createHTMLDocument("x");
  expect(isMulti(doc)).toBe(false);
  doc.head.innerHTML = '<meta name="lore-multi" content="1">';
  expect(isMulti(doc)).toBe(true);
});
