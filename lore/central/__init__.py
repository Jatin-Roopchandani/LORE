"""The central database: users, the job queue, and (later) invites and the
waitlist. Everything per-user (books, graph, vectors) stays in that user's own
directory (see lore.tenancy); this holds only what spans users."""
