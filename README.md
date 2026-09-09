# learn

A tutoring skill that teaches one concept at a time. It runs in any coding agent that loads skills from a `SKILL.md`.

Each topic gets a single web page: picture first, few words, then questions with answer boxes. Concept lessons end with questions you answer in your own words and the tutor grades. Drills check themselves as you go. A terse note in your Obsidian vault keeps the record.

## Install

Link this folder into wherever your agent loads skills from. Claude Code:

```
rm -rf ~/.claude/skills/learn
ln -s "$(pwd)" ~/.claude/skills/learn
```

Paths and learner preferences are in `SKILL.md`: pages under `~/Learning/<topic>/`, notes in `~/Obsidian Vault/`, and a short "The learner" section. Edit to match your setup. The `name` and `description` fields in the frontmatter are the portable ones; the rest are Claude Code hints that other agents ignore.

## Use

```
/learn stars and bars, source ~/notes/discrete-math-study-guide.md
```

Outside Claude Code, ask for it by name: "use the learn skill: stars and bars, source ...".

The tutor asks for a goal if it is unclear, plans 3 to 6 lessons, writes lesson 0 (where you're at) and lesson 1, then publishes or opens the page.

Do the lesson on the page, then say one word in the terminal:

| word | what happens |
|---|---|
| `done` | your answers are graded, feedback appears under each question, the next lesson unlocks |
| `stuck` / `too hard` | more help now: a hint or a worked step is added to the current lesson |
| `too easy` | less help from here on |
| `just tell me` | the answer, no argument |
| `next` | move on |

Come back any time with the topic name; it picks up at the active lesson.

## Two tiers

The page works two ways, and detects which on its own.

- **Hosted.** If the agent can publish a page with a small database (Claude Code artifacts do this), the page gets a URL that opens on your laptop and your phone, answers sync between them, and the tutor reads them directly.
- **Local.** Otherwise the page opens from disk. Answers stay in that browser. A **Copy answers** button appears after each lesson; press it and paste the block into the terminal with `done`.

## Files

- `SKILL.md` - the skill
- `template.html` - the topic page template, with a worked example (stars and bars) showing a probe, a passed concept lesson with feedback, an active drill, cheat sheet, glossary, and the how-it-works page
