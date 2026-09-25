# Hosted tier

Use this when the host can publish an HTML file to a URL and give the page a small shared database. The page detects the database itself: the template's store hook calls `window.claude.use('db')` and falls back to localStorage when that is missing, so the same file works on both tiers. On the hosted tier you read answers from the database, and the page's `onSnapshot` subscription keeps two open devices in step. Copy answers appears on the current lesson while answers wait to send, and the sidebar says how many; a pasted block may be newer than the database, so merge by `at` (the newer record wins, histories are joined, and `value` and `correct` follow the latest attempt).

## Claude Code

- Before the first publish in a session, load the `artifact-design` and `artifact-capabilities` skills.
- First publish: `Artifact` with `file_path`, `capabilities: {db: {}}`, an `icon`, and a one-line `description`. Put the URL in the vault note's `page:` line.
- Later sessions: `Artifact` action `read` with the URL first, then publish with `url` so the same link updates. Open views refresh on their own.
- Grading: `ArtifactData` action `query`, collection `answers`, `query.where` on `lesson == <section id>`. Each document has the same fields as a pasted answer: `lesson`, `q`, `kind`, `value`, `correct`, `attempts`, `history`, `at`.
- The page always opens at the active lesson. Hash links in the URL do not reach it, so never promise a deep link.
- The phone uses the same URL, signed in.

Tool names change; if the ones above are missing, look for the host's current artifact and artifact-database tools and use those. If the host cannot do all three steps (publish, database, read the collection), use the local tier.

## Other hosts

Same shape: publish the file, give the page a database the script can reach, read the `answers` collection when grading. Add the host's database call next to the `window.claude` line in the template's store hook.
