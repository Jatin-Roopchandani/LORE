import { hostedSnippets } from "./mcpSnippets";

const url = "https://lore.example.org/mcp";

test("the Claude Code command is the exact one, with the address and the token", () => {
  expect(hostedSnippets(url, "lore_abc").claudeCode)
    .toBe('claude mcp add --transport http lore https://lore.example.org/mcp --header "Authorization: Bearer lore_abc"');
});

test("the JSON configs parse and carry the address and the bearer header", () => {
  const s = hostedSnippets(url, "lore_abc");
  expect(JSON.parse(s.json)).toEqual({ mcpServers: { lore: { type: "http", url, headers: { Authorization: "Bearer lore_abc" } } } });
  expect(JSON.parse(s.cursor)).toEqual({ mcpServers: { lore: { url, headers: { Authorization: "Bearer lore_abc" } } } });
});

test("a placeholder token stays readable in every snippet", () => {
  const s = hostedSnippets(url, "<your token>");
  for (const text of [s.claudeCode, s.json, s.cursor]) expect(text).toContain("Bearer <your token>");
});
