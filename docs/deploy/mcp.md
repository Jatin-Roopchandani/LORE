# Connecting Claude (and other MCP clients) to a hosted LORE

A hosted LORE (multi-user mode, e.g. `https://lore.jrvn.site`) serves an MCP endpoint at **`<base>/mcp`**. Every reader connects to the same address and reads only their own library. There are two ways in:

- **claude.ai and Claude Desktop**: a *custom connector*. Claude signs you in to LORE through OAuth and asks you to allow it. No token to copy.
- **Claude Code, Cursor and any other MCP client**: a *personal access token* you make in LORE's Settings, sent as `Authorization: Bearer lore_…`. (Claude Code can also use the OAuth sign-in; see below.)

What a connected client can do: search your books, read the characters, places and relationships LORE found, and ask LORE questions. Questions run on **your own AI provider key** (Settings → AI providers) and count against your usual limits. It cannot change your library, upload or delete books, or see your account, settings or keys. Your reading position applies unless you ask Claude to ignore it. Raw Cypher queries (`cypher_query`) are a local-only tool and are not offered here.

---

## claude.ai and Claude Desktop (custom connector)

You need: a LORE account, and LORE served over **https** (OAuth is switched off on a plain-http address other than localhost).

1. In **claude.ai**, open **Settings → Connectors** and choose **Add custom connector**.
2. **Name:** `LORE`. **Remote MCP server URL:** `https://lore.jrvn.site/mcp` (Settings → MCP connector in LORE shows the exact address). Leave the advanced settings (client ID and secret) empty: Claude registers itself.
3. Click **Add**, then **Connect**. A LORE page opens:
   - if you are not signed in to LORE, sign in (Google, GitHub or an email link); you come back to the same page;
   - the page says **"Claude wants to read your LORE library"** and what that means. Choose **Allow**.
4. You are sent back to Claude, and the connector shows as connected. In a chat, enable LORE from the tools menu and ask, for example, *"Using LORE, who is Irene Adler?"*

**Claude Desktop** uses the connectors of the claude.ai account it is signed in to, so once it is added on claude.ai it is there too (on a Team or Enterprise plan an owner may have to add custom connectors first).

**To disconnect:** in LORE, **Settings → MCP connector → Connected apps → Disconnect**. Claude's access ends at once (every token it holds stops working). Removing the connector in claude.ai does not by itself revoke LORE's side, so disconnect in LORE too.

## Claude Code

With a token (works everywhere, no browser needed):

1. In LORE, **Settings → MCP connector → Access tokens**: type a name (e.g. *Claude Code on my laptop*) and choose **Create token**. Copy it now: it is shown once.
2. The **Connect a client** section now shows the command with your token filled in. Run it once in a terminal:
   ```
   claude mcp add --transport http lore https://lore.jrvn.site/mcp --header "Authorization: Bearer lore_…"
   ```
3. In Claude Code, `/mcp` lists `lore` as connected.

With OAuth instead: `claude mcp add --transport http lore https://lore.jrvn.site/mcp`, then `/mcp` in Claude Code, choose `lore` and **Authenticate**: your browser opens LORE's consent page; choose **Allow**.

## Cursor and other clients

Settings → MCP connector gives copy-ready snippets with the address (and your new token) filled in:

- **Cursor** (`~/.cursor/mcp.json`):
  ```json
  { "mcpServers": { "lore": { "url": "https://lore.jrvn.site/mcp",
      "headers": { "Authorization": "Bearer lore_…" } } } }
  ```
- **Any client with an `mcpServers` file** (e.g. a project's `.mcp.json`): the same with `"type": "http"`.

Revoke a token you no longer use in **Settings → MCP connector → Access tokens → Revoke**. Each account can hold 10 tokens.

---

## For the operator

- Nothing to configure: `/mcp`, the OAuth endpoints and the consent page are on whenever `LORE_MULTI_USER=1`. OAuth needs `LORE_BASE_URL` on https (or loopback for local testing); without it the server logs "MCP OAuth is off" and tokens still work.
- Endpoints (all on the public origin): `/mcp` (Streamable HTTP, stateless, JSON responses), `/.well-known/oauth-protected-resource[/mcp]`, `/.well-known/oauth-authorization-server`, `/register`, `/authorize`, `/token`, `/revoke`, the consent page `/oauth/consent`.
- Caddy needs no special rule: these are ordinary requests to the same upstream. MCP clients send no `Origin` header; a browser page on another origin is refused by the origin guard.
- Limits (central database, shared by every web process): `LORE_MCP_PER_MINUTE` (30 tool calls a minute per token or connection), `LORE_MCP_PER_MINUTE_PER_USER` (60 across all of a user's tokens). A `lore_search` also takes the user's question slot and counts against `LORE_ASK_PER_MINUTE`, exactly like a question in the web app. Client registration: 20 an hour per address.
- Logs: one line per tool call on the `lore.mcp.hosted` logger (tool, `pat:<id>` or `oauth:<grant id>`, user id, outcome, time, size). Never the question, names or any book text.
- `lore admin rotate-keys` also re-seals registered clients' secrets under the new master key.
- Account deletion removes the user's tokens, connections, codes and OAuth tokens.
