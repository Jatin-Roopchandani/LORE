---
title: Use your ebook library in Claude with MCP
description: Connect your own ebooks to claude.ai, Claude Desktop or Claude Code through LORE's MCP server, so Claude can answer questions about your books with citations and without spoilers.
date: 2026-11-11
---

You can give Claude access to your own ebooks by connecting LORE as an MCP connector. In claude.ai, open Settings, then Connectors, choose "Add custom connector", and paste `https://lore.jrvn.site/mcp`. After you allow access, Claude can search your library, look up characters and ask LORE questions about your books, with every answer cited and your reading position still applied.

## What is MCP?

MCP (the Model Context Protocol) is an open standard that lets AI apps such as Claude use outside tools and data. An MCP server offers a set of tools; a client like claude.ai, Claude Desktop, Claude Code or Cursor connects to it and can call those tools while it answers you.

LORE runs an MCP server for your library. Your books stay in LORE: Claude asks LORE, and LORE answers from the text.

## What Claude can do with your library

Once connected, Claude has these tools:

- **lore_search** asks LORE a question. LORE answers from your books with quoted citations, using your own AI provider key.
- **vector_search** finds the passages closest to a phrase or idea.
- **entity** looks up a character, place or group: who they are and what they're connected to.
- **subgraph** walks the knowledge graph around an entity, for example everyone a character has dealt with.
- **library** lists your books and worlds.

Your reading position applies to all of them, so Claude can't see past the chapter you're on unless you ask it to ignore your position for a question.

## Connect claude.ai or Claude Desktop

You need a LORE account with at least one book added.

1. In claude.ai, open **Settings**, then **Connectors**, and choose **Add custom connector**.
2. Name it **LORE** and paste `https://lore.jrvn.site/mcp` as the server address. Leave the advanced settings empty.
3. Claude opens a LORE page. Sign in if asked, then choose **Allow**.
4. That's it. Claude Desktop uses the same connectors as your claude.ai account, so it works there too.

The LORE page tells you exactly what you're allowing: Claude can search your books and ask LORE questions, but it can't change your library or see your account, settings or keys. You can disconnect it at any time in LORE's Settings, under **MCP connector**.

## Connect Claude Code, Cursor or another MCP client

Clients that don't use claude.ai's connectors sign in with an access token instead:

1. In LORE, open **Settings**, then **MCP connector**.
2. Under **Access tokens**, give the token a name (for example "Claude Code on my laptop") and choose **Create token**. Copy it now: it won't be shown again.
3. Under **Connect a client**, copy the ready-made setup for Claude Code, Cursor or a JSON `mcpServers` config. The server address and your new token are already filled in.

Make one token per app, and revoke any you no longer use.

## Things to try

- "Using LORE, who has Holmes met so far in *The Sign of the Four*, and how?"
- "Ask LORE how the relationship between these two characters changes across the series."
- "Look up this place in my library and list every scene set there, in story order."
- "Use LORE's sources to help me write a spoiler-free recap of the first five chapters."

Claude can combine LORE's cited answers with its own writing, so this works well for recaps, study notes and book club questions.

## Get started

The hosted LORE is in an invite-only beta: [request an invite](/#beta). LORE is also open source on [GitHub](https://github.com/Jatin-Roopchandani/LORE), and the local version runs an MCP server on your own computer. The [FAQ](/faq) covers the rest.
