import { render, screen } from "@testing-library/react";
import { BLOG, HOME } from "../../messages";
import { pageFor } from "../../routing";
import Blog from "./Blog";
import { FAQ, POSTS, faqFrom, longDate, parse, postsFrom } from "./content";

const md = (front: string, body = "Hello **world**.") => `---\n${front}\n---\n\n${body}\n`;

test("front matter gives the title, description and date; the body is what follows", () => {
  const { meta, body } = parse(md("title: A: B\ndescription: About it\ndate: 2026-11-11"));
  expect(meta).toEqual({ title: "A: B", description: "About it", date: "2026-11-11" });
  expect(body.trim()).toBe("Hello **world**.");
  expect(() => parse(md("title: No description\ndate: 2026-11-11"))).toThrow(/description/);
});

test("posts are newest first, slugged by file name, with their Markdown rendered", () => {
  const posts = postsFrom({
    "../content/blog/older-one.md": md("title: Older\ndescription: d\ndate: 2026-01-02"),
    "../content/blog/newer-one.md": md("title: Newer\ndescription: d\ndate: 2026-03-04"),
  });
  expect(posts.map((p) => p.slug)).toEqual(["newer-one", "older-one"]);
  expect(posts[0].html).toContain("<strong>world</strong>");
});

test("the FAQ is each ## question with the answer under it", () => {
  const faq = faqFrom(md("title: FAQ\ndescription: d\ndate: 2026-11-11", "## One?\n\nFirst.\n\n## Two?\n\nSecond, [linked](/faq)."));
  expect(faq.items.map((i) => i.q)).toEqual(["One?", "Two?"]);
  expect(faq.items[1].html).toContain('<a href="/faq">linked</a>');
});

test("dates read the same everywhere", () => {
  expect(longDate("2026-11-11")).toBe("11 November 2026");
});

test("the real posts and FAQ are complete, and every link to this site points at a page that exists", () => {
  expect(POSTS.length).toBeGreaterThanOrEqual(3);
  expect(FAQ.items.length).toBeGreaterThanOrEqual(8);
  const pages = new Set(["/", "/#beta", "/privacy", "/terms", "/blog", "/faq", ...POSTS.map((p) => `/blog/${p.slug}`)]);
  for (const p of POSTS) {
    expect(p.slug).toMatch(/^[a-z0-9-]+$/);
    expect(p.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(p.description.length).toBeLessThanOrEqual(200); // a meta description, not a paragraph
  }
  const html = [...POSTS.map((p) => p.html), ...FAQ.items.map((i) => i.html)].join("");
  for (const [, href] of html.matchAll(/href="(\/[^"]*)"/g)) expect(pages, href).toContain(href);
});

test("the blog, its posts and the FAQ have their own addresses", () => {
  expect(pageFor("/blog", true)).toBe("blog");
  expect(pageFor("/blog/", true)).toBe("blog");
  expect(pageFor("/faq", true)).toBe("faq");
  expect(pageFor("/blog/use-your-ebook-library-in-claude", true)).toBe("post");
  expect(pageFor("/blog/a/b", true)).toBe("notfound");
  expect(pageFor("/blog", false)).toBe("app"); // local mode: one page, the app
});

test("the index lists every post and links the FAQ", () => {
  render(<Blog page={{ kind: "index" }} signedIn={false} />);
  expect(screen.getByRole("heading", { level: 1, name: BLOG.title })).toBeInTheDocument();
  for (const p of POSTS) expect(screen.getByRole("link", { name: p.title })).toHaveAttribute("href", `/blog/${p.slug}`);
  expect(screen.getByRole("link", { name: BLOG.faqLink })).toHaveAttribute("href", "/faq");
});

test("a post shows its title, byline, date and body; an unknown one says so", () => {
  const post = POSTS[0];
  const { unmount } = render(<Blog page={{ kind: "post", slug: post.slug }} signedIn={false} />);
  expect(screen.getByRole("heading", { level: 1, name: post.title })).toBeInTheDocument();
  expect(screen.getByText(new RegExp(BLOG.byline))).toBeInTheDocument();
  expect(screen.getByRole("link", { name: BLOG.back })).toHaveAttribute("href", "/blog");
  unmount();
  render(<Blog page={{ kind: "post", slug: "nope" }} signedIn={false} />);
  expect(screen.getByRole("heading", { level: 1, name: BLOG.notFound })).toBeInTheDocument();
});

test("the FAQ shows each question as a heading", () => {
  render(<Blog page={{ kind: "faq" }} signedIn={false} />);
  for (const item of FAQ.items) expect(screen.getByRole("heading", { level: 2, name: item.q })).toBeInTheDocument();
});

test("the footer links the blog and the FAQ, on every written page", () => {
  render(<Blog page={{ kind: "index" }} signedIn={false} />);
  const footer = screen.getByRole("contentinfo");
  expect(footer.querySelector('a[href="/blog"]')).toHaveTextContent(BLOG.footerBlog);
  expect(footer.querySelector('a[href="/faq"]')).toHaveTextContent(BLOG.footerFaq);
  expect(footer.querySelector('a[href="/signin"]')).toHaveTextContent(HOME.signIn);
});
