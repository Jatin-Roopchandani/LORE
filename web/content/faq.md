---
title: LORE FAQ
description: Answers to common questions about LORE, the AI reader that answers questions about your own ebooks with citations and without spoilers.
date: 2026-11-11
---

## What is LORE?

LORE is an AI reader for the ebooks you own. You upload an EPUB or PDF, LORE turns it into a knowledge graph of the characters, places, groups and events in the book, and then you can ask it anything about the story. Every sentence in an answer cites the passage it comes from.

## Can AI answer questions about my own ebooks?

Yes. LORE answers from the text of the books you upload, not from what a model remembers about them. That means it works for less famous books, new releases and your own copy. Each fact in an answer quotes a passage, and a second check cuts anything the passage doesn't actually say.

## How does LORE avoid spoilers?

You tell LORE how far you've read: each book is Finished, Reading (with the chapter you're on) or Not started. LORE then only searches the chapters you've read. Answers, the graph and character profiles all stop at your position, so nothing past it can leak into an answer. If you want to see further ahead for one question, tick "Show spoilers for this question".

## What happens if I ask about something I haven't read yet?

LORE tells you that you haven't reached the part of the book that answers it yet, and offers the spoiler toggle in case you want to see it anyway. It doesn't pretend the book doesn't cover it.

## Which file formats does LORE support?

EPUB and PDF, up to 50 MB per book. LORE reads the book once when you add it; after that you can explore it and ask questions straight away.

## Which AI models does LORE use?

You bring your own key from Google Gemini (the free tier works), Anthropic Claude or OpenAI. Your questions run on your own account, so you stay in control of usage and cost. LORE shows your usage in Settings.

## Can I ask questions that span a whole series?

Yes. Books can be grouped into a world, such as a series, and questions run across every book in it that you've read. You can ask how a character changes from the first book to the fifth, how a magic system works, or for a deep dive: a longer, sectioned analysis with LORE's own reading and the other side, every claim cited.

## How is LORE different from ChatGPT?

A general chatbot answers about famous books from memory. It can't cite the exact passage, it may not know your book at all, and it has no idea which chapter you're on. LORE answers only from your copy of the book, cites every sentence, checks each one against its passage, and stops at your reading position.

## Can I use my LORE library from Claude?

Yes. LORE runs an MCP server, so you can add it to claude.ai or Claude Desktop as a custom connector, or connect Claude Code, Cursor or any MCP client with an access token. Claude can then search your library, look up characters and ask LORE questions, with your reading position still applied. See [Use your ebook library in Claude](/blog/use-your-ebook-library-in-claude).

## Is my library private?

Yes. Each reader's books, graph and chats are kept in their own library, and nothing is shared between readers. Your AI key is stored encrypted, and LORE only ever shows its last four characters. The [privacy page](/privacy) has the details.

## Is LORE open source?

Yes. The code is on [GitHub](https://github.com/Jatin-Roopchandani/LORE), and you can run it on your own computer. The hosted version at lore.jrvn.site is in an invite-only beta.

## How do I get access?

Request an invite on the [homepage](/#beta). Invites go out in small groups and arrive from login@jrvn.site, so check your spam folder if you don't see it.
