# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Instead, report it privately through
GitHub: open the repository's **Security** tab and choose
[**Report a vulnerability**](https://github.com/Jatin-Roopchandani/LORE/security/advisories/new).

Please include what you found, how to reproduce it, and what an attacker could do with it.
You'll get a reply within a few days, and a fix will be prioritised according to its severity.
You'll be credited in the fix unless you'd rather not be.

## Scope

This covers the code in this repository and the hosted beta at
[lore.jrvn.site](https://lore.jrvn.site). Of particular interest:

- one reader being able to see another reader's books, chats or keys;
- getting around the reading-position limit to see content past a reader's chapter;
- exposure of stored AI provider keys;
- authentication, session or CSRF weaknesses.

Please don't run automated scans or load tests against the hosted beta, and don't access other
people's data. Test against your own self-hosted instance where you can.
