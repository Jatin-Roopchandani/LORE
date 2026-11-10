import { describe, expect, test } from "vitest";
import { editDistance, suggestEmail } from "./emailSuggest";

describe("suggestEmail", () => {
  test.each([
    ["shadow@gmai.com", "shadow@gmail.com"],
    ["a@gmial.com", "a@gmail.com"],
    ["a@gmail.co", "a@gmail.com"],
    ["a@gmaill.com", "a@gmail.com"],
    ["a@hotmial.com", "a@hotmail.com"],
    ["a@yaho.com", "a@yahoo.com"],
    ["a@outlok.com", "a@outlook.com"],
    ["  A.B@GMAI.COM ", "A.B@gmail.com"],
  ])("%s -> %s", (typed, expected) => expect(suggestEmail(typed)).toBe(expected));

  test.each(["a@gmail.com", "a@grovio.ai", "a@mail.jiit.ac.in", "a@example.org", "a@jrvn.site", "nope", "a@", "@gmail.com"])(
    "no suggestion for %s", (typed) => expect(suggestEmail(typed)).toBeNull());

  test("a neighbouring swap is one edit", () => {
    expect(editDistance("gmial", "gmail")).toBe(1);
    expect(editDistance("gmail", "gmail")).toBe(0);
  });
});
