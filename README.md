# learn

A tutoring skill that teaches one concept at a time. It runs in any coding agent that loads skills from a `SKILL.md`.

Each goal gets a single web page of short lessons. A concept lesson opens with one guess, then a picture, few words, and questions with answer boxes; at least one you answer in your own words, and the tutor grades it. Drills check themselves as you go. A terse note in your Obsidian vault keeps the record.

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

The tutor asks for a goal if it is unclear, plans 3 to 6 lessons, writes lesson 0 (where you're at), then opens the page. Each lesson you pass unlocks the next one.

Do the lesson on the page, then say one word in the terminal:

| word | what happens |
|---|---|
| `done` | your answers are graded, feedback appears under each question, the next lesson unlocks |
| `stuck` / `too hard` | more help now: a hint or a worked step is added to the current lesson |
| `too easy` | less help from here on |
| `just tell me` | the answer, no argument |
| `next` | move on |

Each rule you pass comes back later as a short review question with new numbers, every week by default or spread out before an exam date you give. When one is due, the page opens there first, never more than three a day.

Come back any time with the topic name; it picks up at the active lesson.

## Two tiers

The page works two ways, and detects which on its own.

- **Hosted.** If the agent can publish a page with a small database (Claude Code artifacts do this), the page gets a URL that opens on your laptop and your phone, answers sync between them, and the tutor reads them directly.
- **Local.** Otherwise the page opens from disk. Answers stay in that browser. Press **Finish lesson** at the end of each lesson: it copies a block to paste into the terminal with `done`.

**Phone on home Wi-Fi.** With Python 3.7+, the tutor serves each goal page from your computer (`scripts/serve.py`), and answers save to files next to the page. Ask for the phone and it turns on the same Wi-Fi mode: the laptop's page shows a QR code for the phone, and a login job (`scripts/schedule.py`, macOS for now) keeps the server running across restarts. The laptop has to be awake. Use it on your home network only: the link is plain HTTP. If your macOS firewall is on, allow Python's incoming connections when it asks.

## Files

- `SKILL.md` - the skill
- `template.html` - the topic page template: a probe, a sample concept lesson, a sample drill, the cheat sheet, and the how-it-works page. The script draws the sidebar, progress bar, and lesson strips from each section's attributes
- `references/hosted.md` - how to publish the page with a synced answer database when the agent has such a tool
- `scripts/serve.py` - the goal server: serves each goal page and keeps its `answers.json` and `state.json`; `lan on` adds the phone on the same Wi-Fi, `tailscale on` the phone anywhere through Tailscale Serve
- `scripts/schedule.py` - installs the login job that keeps the goal server running
