# Career Ops Agent

An agentic career-operations system that discovers relevant vacancies, evaluates
them against a target profile, drafts tailored application materials, and
coordinates human-approved submissions.

The project combines:

- an AI agent and skill layer that defines goals, decision rules, MCP tool usage,
  and approval boundaries;
- an MCP (Model Context Protocol) server that exposes job-search and
  application-management capabilities to compatible AI clients;
- a scheduled scanner that monitors company career pages;
- persistent state for deduplication, application tracking, and interrupted
  session recovery;
- a human approval gate before any application is submitted; and
- ATS automation, an email fallback, and completion notifications for approved
  applications.

The MCP server is one component of the system, not the entire system.
Deterministic work such as scheduled scanning and deduplication runs
automatically, while the AI layer interprets results, drafts application
materials, and guides the broader workflow. The system is therefore agentic
rather than fully autonomous: consequential submissions remain subject to
human approval.

I built this because I'm a security engineer and the job search is,
structurally, a monitoring-and-response problem: noisy signal sources, a triage
step, an enrichment step, and an automated response with a human-approval gate.
I built it the way I'd build a detection-and-response pipeline.

> **Status:** Personal project actively used in my own job search. The published
> version excludes credentials, personal profile data, application history, and
> the private company seed list. See [Configuration](#configuration).

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Windows Task Scheduler (nightly)                       │
│      └─► Docker readiness check ─► container start      │
├─────────────────────────────────────────────────────────┤
│  Scan stage      career-page fetchers (per-company)     │
│  Filter stage    dedupe · already-applied · lateral-    │
│                  move rules · false-positive URL rules  │
│  Draft stage     cover-letter generation from profile + │
│                  job description                        │
│  Approval gate   letters surface for human confirmation │
│                  (MCP session, resume-capable)          │
│  Submit stage    ATS form automation ─► Gmail fallback  │
│  Notify stage    Windows toast + email summary          │
├─────────────────────────────────────────────────────────┤
│  Shared data volume (single mount, scanner + MCP server)│
│  Seed list: 117+ companies · JSON state · run logs      │
└─────────────────────────────────────────────────────────┘
```

Design decisions worth calling out:

- **Human-in-the-loop where it matters, nowhere else.** The pipeline pauses at
  exactly one gate: cover-letter confirmation. Everything upstream (rejecting
  already-applied companies, lateral moves, product-page false positives) is
  silent and automatic. Everything downstream (submission) fires immediately on
  approval. This mirrors how a good SOAR playbook treats analyst approval —
  one decision point, full context attached, no busywork.
- **Self-expanding coverage.** A scan that produces zero genuinely new matches
  after filtering triggers a research step that adds five new target companies
  to the seed list before the run ends. The pipeline treats "no findings" as a
  coverage gap, not a success state.
- **Session resume.** Long-running MCP sessions survive interruption via an
  `initialize_session` / `resume: true` handshake, so a nightly run that dies
  mid-approval can be picked up without re-scanning.

## Lessons learned (the fun part)

These are the failures that shaped the current design — the kind of thing a
README usually hides and an engineer actually wants to read:

- **UTF-8 BOM poisoning.** Windows-edited JSON state files grew a byte-order
  mark that the parser silently choked on. Fix: normalize encoding on every
  read, never trust the editor.
- **Split-brain storage.** The nightly script and the MCP server originally
  mounted different Docker volumes and disagreed about reality. Fix: one shared
  data directory, one source of truth.
- **Scheduler time limits.** Windows Task Scheduler's default execution limit
  killed long runs mid-submission. Fix: extended limits + resume logic, so a
  kill is an inconvenience instead of data loss.
- **Em dash corruption.** Encoding mismatches mangled punctuation in generated
  letters — a cosmetic bug with real cost, since a garbled cover letter reads
  as carelessness. Fix: end-to-end UTF-8 enforcement and an output lint pass.
- **Docker readiness races.** The scheduled task fired before the Docker daemon
  was up. Fix: an explicit readiness probe with backoff before the run starts.

## Prerequisites

- **Docker** — Docker Desktop on Windows (the nightly wrapper auto-starts it),
  or any Docker engine if you only want the MCP server. Both halves of the
  system — the scheduled scan and the interactive MCP server — run in the
  same `job-search-mcp:latest` image and share the `data/` volume.
- **An MCP-capable AI client** (Claude Desktop, or anything speaking MCP over
  stdio) for the interactive half: reviewing pending matches, approving
  letters, triggering submissions. Something like Docker's MCP gateway works
  too; the server itself is plain stdio, so any launcher that can run
  `docker run -i` can host it.
- **Windows** for the nightly automation as shipped (PowerShell wrapper,
  Task Scheduler, toast notifications). The Python core is OS-agnostic —
  porting the wrapper to cron/systemd is a small exercise.

## Registering the MCP server

The server speaks stdio, so the client launches the container directly.
For Claude Desktop, add to `claude_desktop_config.json` (adjust paths):

```json
{
  "mcpServers": {
    "job-search": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "C:\\path\\to\\repo\\data:/data",
        "-v", "C:\\path\\to\\repo\\config.json:/app/config.json",
        "-v", "C:\\path\\to\\your\\cvs:/cvs:ro",
        "job-search-mcp:latest"
      ]
    }
  }
}
```

The `-i` flag and the shared `data/` mount are both load-bearing: stdio needs
the former, and the nightly scan and the interactive session only agree about
reality because they read the same `/data` (see Lessons learned).

## Configuration

**The repo ships the engine; you bring the fuel.** Docker and an MCP client
get the machine running, but it does nothing useful until `config.json`
contains *your* identity, *your* target roles (each mapped to a CV file), and
*your* seed list of companies worth scanning. That targeting is where the real
work of a job search lives — expect to invest in it, and expect the quality of
your matches to track the quality of your seed list. All of it stays in one
gitignored file; nothing personal ever belongs in the repo itself.

The fastest way in is the interview wizard:

```
python setup_config.py
```

It walks you through identity, geography, roles, and seed companies, inherits
the non-personal defaults (careers-page patterns, ATS list) from
`config.example.json`, and writes `config.json` as UTF-8 **without a BOM** —
which matters, because a BOM on this file breaks the JSON parse inside the
container (ask me how I know; see Lessons learned).

Prefer doing it by hand? `cp config.example.json config.json` and edit. Either
way, then:

1. Put your CV PDFs in the directory pointed to by `JOB_SEARCH_CV_DIR`
   (defaults to `<repo>\cvs`).
2. Set `JOB_SEARCH_SMTP_USER` / `JOB_SEARCH_SMTP_PASS` as user environment
   variables (Gmail app password) — see `setup_scheduled_task.ps1` header.
3. `docker build -t job-search-mcp .`, register the MCP server with your
   client, and run `setup_scheduled_task.ps1` once as Administrator to
   create the nightly task.

The agent's operating rules — triage behavior, the approval gate, the
zero-match seed-expansion rule — live in `CLAUDE.md` / `AGENTS.md`.

## Roadmap

- Response/funnel tracking: tag every reply, interview, and offer by channel
  so the pipeline measures conversion, not just throughput
- Pluggable fetchers for common ATS platforms (Comeet, Greenhouse, Lever)
- Structured detection-style rules for match filtering, replacing ad-hoc logic

## Why this is on my CV

Everything in this repo — agent orchestration, human-approval gates, silent
auto-triage, self-healing scheduled execution, encoding hygiene, state
management across container boundaries — is the same engineering that security
automation platforms are built from. I just happened to point it at job boards
first.
