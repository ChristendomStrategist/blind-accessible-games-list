#!/usr/bin/env python3
"""Announce changes to the list in Discord.

Compares README.md between two commits and posts what changed to every Discord
webhook it is given, starting with @everyone:

  New       entries that were not in the list before
  Updated   entries whose status, title, links or text changed
  Removed   entries that are no longer in the list

New games are named in alphabetical order, each linking to its mod (or to the
game itself when it needs no mod). Updated games say what changed. Pushes that
change no entry are not announced.

Usage:
  python tools/announce_update.py --before SHA --after SHA
  python tools/announce_update.py --before SHA --after SHA --dry-run

The webhooks are DISCORD_WEBHOOK_URL, then DISCORD_WEBHOOK_URL2,
DISCORD_WEBHOOK_URL3 and so on (each one address, or several separated by
spaces). Without any, or with --dry-run, the messages are printed instead of
sent. Standard library only. Entries and their keys come from
update_data.py, so a game is matched across commits exactly as the sortable
list matches it.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as data  # noqa: E402

LIST_URL = "https://christendomstrategist.github.io/blind-accessible-games-list/"
LIMIT = 2000  # Discord's limit on one message
SUPPRESS_EMBEDS = 1 << 2  # no link previews under the message
USER_AGENT = "DiscordBot (blind-accessible-games-list announcer, 1.0)"
SECTION_NOTES = {"games": "", "accidental": " (accidentally accessible)", "ios": " (iOS)"}
MOD_LINK = re.compile(r"\bwith\b[^\[\]()]{0,12}" + data.LINK.pattern, re.I)
WEBHOOK = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+(?:\?\S*)?")


# --- reading the list ---------------------------------------------------------

def readme_at(commit):
    if not commit or set(commit) == {"0"}:
        return None
    result = subprocess.run(
        ["git", "show", f"{commit}:README.md"],
        cwd=data.ROOT, capture_output=True, encoding="utf-8",
    )
    return result.stdout if result.returncode == 0 else None


def entries(markdown):
    """parse_entries, plus each entry's own line and its sub-bullets."""
    found = data.parse_entries(markdown)
    blocks = []
    section = current = None
    for raw in markdown.splitlines():
        heading = re.match(r"^#+\s+(.*\S)\s*$", raw)
        if heading:
            section = data.SECTIONS.get(heading.group(1).lower())
            current = None
            continue
        if not section:
            continue
        if raw.startswith("- "):
            current = {"line": data.repair_line(raw[2:].strip()), "subs": []}
            blocks.append(current)
        elif current is not None and re.match(r"^\s+- ", raw):
            current["subs"].append(data.repair_line(raw.strip()[2:].strip()))
        elif raw.strip():
            current = None
    if len(found) != len(blocks):
        raise SystemExit("announce_update.py and update_data.py read the list differently")
    for entry, block in zip(found, blocks):
        entry.update(block)
    return {entry["key"]: entry for entry in found}


def squash(text):
    return " ".join(text.split())


def main_link(entry):
    """The mod's link, or the game's own when the entry names no mod."""
    for text in [entry["line"], *entry["subs"]]:
        match = MOD_LINK.search(text)
        if match:
            return match.group(2)
    return entry["urls"][0] if entry["urls"] else ""


def status(text, title=""):
    """The accessibility note after the dash, e.g. 'work in progress'."""
    plain = data.LINK.sub(lambda m: m.group(1), text)
    start = plain.find(title) + len(title) if title and title in plain else 0
    match = re.search(r"\s[-–—]\s*(\S.*)$", plain[start:])
    return match.group(1).strip() if match else ""


# --- writing the announcement -------------------------------------------------

def escape(text):
    return re.sub(r"([\\*_~|`>\[\]])", r"\\\1", text)


def link(entry):
    title = escape(entry["title"]) + SECTION_NOTES[entry["section"]]
    url = main_link(entry).replace("(", "%28").replace(")", "%29")
    return f"[{title}]({url})" if url else title


def sub_lines(old, new):
    """Sub-bullets (a game's second mod, say) that are new or reworded."""
    before = {squash(sub) for sub in old["subs"]}
    return [f"  - {sub}" for sub in new["subs"] if squash(sub) not in before]


def describe(old, new):
    notes = []
    if old["title"] != new["title"]:
        notes.append(f"renamed from {escape(old['title'])}")
    now = status(new["line"], new["title"])
    if now and now != status(old["line"], old["title"]):
        notes.append(f"now {now}")
    subs = sub_lines(old, new)
    if not notes and not subs and old["urls"] != new["urls"]:
        notes.append("new link")
    if not notes and not subs and squash(old["line"]) != squash(new["line"]):
        notes.append("details updated")
    if not notes and not subs and len(old["subs"]) != len(new["subs"]):
        notes.append("details updated")
    first = f"- {link(new)}" + (f" - {', '.join(notes)}" if notes else "")
    return [first, *subs]


def differs(old, new):
    return (old["title"] != new["title"]
            or squash(old["line"]) != squash(new["line"])
            or [squash(sub) for sub in old["subs"]] != [squash(sub) for sub in new["subs"]])


def compare(old_md, new_md):
    old, new = entries(old_md), entries(new_md)
    added = [new[key] for key in new if key not in old]
    removed = [old[key] for key in old if key not in new]
    changed = [(old[key], new[key]) for key in new if key in old and differs(old[key], new[key])]
    # A renamed entry has a new key, and so does a game that gets a second
    # entry: pair each with the removed one by its links.
    for entry in list(added):
        urls = {data.canonical_url(url) for url in entry["urls"]}
        match = next((gone for gone in removed
                      if gone["section"] == entry["section"]
                      and urls & {data.canonical_url(url) for url in gone["urls"]}), None)
        if match:
            added.remove(entry)
            removed.remove(match)
            if differs(match, entry):
                changed.append((match, entry))
    return added, changed, removed


def announcement(added, changed, removed):
    if not (added or changed or removed):
        return []
    order = lambda entry: entry["title"].casefold()  # noqa: E731
    sections = []
    if added:
        sections.append(("New", [[f"- {link(entry)}"] for entry in sorted(added, key=order)]))
    if changed:
        pairs = sorted(changed, key=lambda pair: order(pair[1]))
        sections.append(("Updated", [describe(old, new) for old, new in pairs]))
    if removed:
        sections.append(("Removed", [[f"- {escape(entry['title'])}"] for entry in sorted(removed, key=order)]))
    return pack(sections)


def pack(sections):
    """Messages of at most LIMIT characters. A game and its sub-bullets stay
    together, and a section carried over to the next message says so."""
    size = lambda lines: len("\n".join(lines).strip())  # noqa: E731
    messages = []
    current = ["@everyone The Blind Accessible Games List has been updated."]
    for heading, blocks in sections:
        title = ["", f"**{heading}**"]
        for block in blocks:
            block = [line[:LIMIT // 2] for line in block]
            if size(current + title + block) > LIMIT and size(current):
                messages.append("\n".join(current).strip())
                current = []
                title = [f"**{heading}** (continued)"]
            current += title + block
            title = []
    footer = ["", f"Full list: <{LIST_URL}>"]
    if size(current + footer) > LIMIT:
        messages.append("\n".join(current).strip())
        current = []
    messages.append("\n".join(current + footer).strip())
    return [message[:LIMIT] for message in messages]


# --- sending ------------------------------------------------------------------

def webhooks():
    """DISCORD_WEBHOOK_URL first, then DISCORD_WEBHOOK_URL2, 3, ... in order."""
    names = [name for name in os.environ if re.fullmatch(r"DISCORD_WEBHOOK_URL\d*", name)]
    names.sort(key=lambda name: int(name[len("DISCORD_WEBHOOK_URL"):] or 1))
    found = []
    for name in names:
        found += WEBHOOK.findall(os.environ[name])
    return list(dict.fromkeys(found))


def post(webhook, content, ping):
    body = json.dumps({
        "content": content,
        "allowed_mentions": {"parse": ["everyone"] if ping else []},
        "flags": SUPPRESS_EMBEDS,
    }).encode("utf-8")
    url = webhook + ("&" if "?" in webhook else "?") + "wait=true"
    for _ in range(5):
        request = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as err:
            detail = err.read()
            if err.code == 429:
                wait = json.loads(detail or b"{}").get("retry_after", 2)
                time.sleep(float(wait) + 0.5)
                continue
            raise RuntimeError(f"Discord refused the message: HTTP {err.code} {detail[:300]!r}")
        except urllib.error.URLError as err:
            raise RuntimeError(f"could not reach Discord: {err.reason}")
    raise RuntimeError("Discord kept asking to slow down; gave up.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before", default="HEAD~1", help="commit to compare from")
    parser.add_argument("--after", default="HEAD", help="commit to compare to")
    parser.add_argument("--dry-run", action="store_true", help="print the messages, send nothing")
    args = parser.parse_args()

    old_md, new_md = readme_at(args.before), readme_at(args.after)
    if old_md is None or new_md is None:
        print("No earlier README to compare with; nothing announced.")
        return
    messages = announcement(*compare(old_md, new_md))
    if not messages:
        print("No entry changed; nothing announced.")
        return

    targets = webhooks()
    if args.dry_run or not targets:
        if args.dry_run:
            print(f"Would send to {len(targets)} webhook(s):\n")
        else:
            print("No Discord webhook found in the secrets, so this was not sent:\n")
        print("\n\n---- next message ----\n\n".join(messages))
        return
    # Every webhook gets its try: one server failing does not stop the rest.
    failed = 0
    for number, webhook in enumerate(targets, 1):
        try:
            for index, message in enumerate(messages):
                post(webhook, message, ping=index == 0)
                time.sleep(1)
            print(f"Webhook {number} of {len(targets)}: announced in {len(messages)} message(s).")
        except RuntimeError as err:
            failed += 1
            print(f"Webhook {number} of {len(targets)}: {err}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
