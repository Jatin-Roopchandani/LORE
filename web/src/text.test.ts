import { cap } from "./text";

test("cap raises the first letter and keeps the rest", () => {
  expect(cap("scientific methods")).toBe("Scientific methods");
  expect(cap("the iPhone and NASA")).toBe("The iPhone and NASA");
  expect(cap("“quoted” part")).toBe("“Quoted” part");
  expect(cap("")).toBe("");
  expect(cap("42")).toBe("42");
});
