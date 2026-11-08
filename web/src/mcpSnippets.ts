// The copy-ready snippets that connect an MCP client to a hosted LORE: built from the server's real address
// (GET /api/mcp's mcp_url) and a personal access token (the one just made, or a placeholder to fill in).
export interface HostedSnippets { claudeCode: string; json: string; cursor: string }

export function hostedSnippets(mcpUrl: string, token: string): HostedSnippets {
  const auth = `Bearer ${token}`;
  return {
    claudeCode: `claude mcp add --transport http lore ${mcpUrl} --header "Authorization: ${auth}"`,
    json: JSON.stringify({ mcpServers: { lore: { type: "http", url: mcpUrl, headers: { Authorization: auth } } } }, null, 2),
    cursor: JSON.stringify({ mcpServers: { lore: { url: mcpUrl, headers: { Authorization: auth } } } }, null, 2),
  };
}
