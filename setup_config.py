#!/usr/bin/env python3
"""
Interactive setup wizard for job-search-mcp.

Interviews you in the terminal and writes a valid config.json.
Stdlib only — run it before installing anything:

    python setup_config.py

Safe to re-run: refuses to overwrite an existing config.json unless
you pass --force. Writes UTF-8 *without* BOM, because a BOM on this
file breaks the JSON parse inside the container (see README, Lessons
learned).
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
EXAMPLE = HERE / "config.example.json"
TARGET = HERE / "config.json"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_required(prompt: str) -> str:
    while True:
        val = input(f"{prompt}: ").strip()
        if val:
            return val
        print("  This one's required.")


def ask_list(prompt: str, min_items: int = 1) -> list[str]:
    print(f"{prompt} (one per line, empty line to finish)")
    items = []
    while True:
        val = input("  - ").strip()
        if not val:
            if len(items) >= min_items:
                return items
            print(f"  Need at least {min_items}.")
            continue
        items.append(val)


def ask_yes(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    val = input(f"{prompt} [{d}]: ").strip().lower()
    if not val:
        return default_yes
    return val in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate config.json interactively.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config.json")
    args = parser.parse_args()

    if TARGET.exists() and not args.force:
        print(f"{TARGET} already exists. Re-run with --force to overwrite it.")
        return 1

    if not EXAMPLE.exists():
        print(f"Missing {EXAMPLE} — run this from the repo root.")
        return 1

    # Non-personal defaults (careers page patterns, ATS list, application
    # method order) come straight from the example config.
    config = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    print("\n=== Job Search MCP — setup interview ===")
    print("Everything you enter stays in config.json, which is gitignored.\n")

    print("-- Who you are (used in cover letters and application forms) --")
    config["candidate"] = {
        "name": ask_required("Full name"),
        "email": ask_required("Email"),
        "phone": ask("Phone", ""),
        "location": ask_required("City / area"),
        "linkedin": ask("LinkedIn URL", ""),
        "github": ask("GitHub URL", ""),
        "blog": ask("Blog / personal site URL", ""),
    }
    # Drop empty optional fields rather than shipping blanks
    config["candidate"] = {k: v for k, v in config["candidate"].items() if v}

    print("\n-- Geography (used to prioritize nearby companies) --")
    base = ask("Home base town/city", config["candidate"].get("location", ""))
    config["geography"] = {
        "base": base,
        "max_drive_minutes": int(ask("Max commute in minutes", "30") or 30),
        "search_areas": ask_list("Areas to search (towns/cities you'd commute to)"),
    }

    print("\n-- Target roles --")
    print("Each role maps to a CV file so applications always attach the right one.")
    roles = []
    while True:
        title = ask_required("Role title (e.g. 'Detection Engineer')")
        keywords = ask_list("  Keywords that identify this role in job listings")
        cv = ask_required("  CV filename for this role (must exist in your CV directory)")
        roles.append({"title": title, "keywords": [k.lower() for k in keywords], "cv": cv})
        if not ask_yes("Add another role?", default_yes=False):
            break
    config["target_roles"] = roles

    print("\n-- Company seed list --")
    print("The companies the nightly scan starts from. You can add more anytime;")
    print("the pipeline also grows this list itself on zero-match runs.")
    seeds = []
    while True:
        name = ask_required("Company name")
        url = ask_required("  Company website (https://...)")
        loc = ask("  Location", "")
        entry = {"name": name, "url": url}
        if loc:
            entry["location"] = loc
        seeds.append(entry)
        if not ask_yes("Add another company?", default_yes=True):
            break
    config["company_seed_list"] = seeds

    # UTF-8, no BOM. json.dump via a text handle opened with encoding="utf-8"
    # never writes a BOM; ensure_ascii=False keeps non-Latin names readable.
    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nWrote {TARGET} ({len(roles)} role(s), {len(seeds)} seed company(ies)).")
    print("Next steps:")
    print("  1. Put your CV PDFs in your CV directory (JOB_SEARCH_CV_DIR, or <repo>\\cvs)")
    print("  2. Set JOB_SEARCH_SMTP_USER / JOB_SEARCH_SMTP_PASS user env vars")
    print("  3. docker build -t job-search-mcp .")
    print("  4. Register the MCP server with your client (see README)")
    print("  5. Run setup_scheduled_task.ps1 once as Administrator (Windows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
