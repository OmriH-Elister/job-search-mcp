---
description: Review the nightly LinkedIn Easy Apply queue with the candidate, then submit the approved jobs through Claude in Chrome
---

Review and submit the LinkedIn Easy Apply queue built by the nightly job-search run.

Paths (`<repo>` is wherever you cloned this project; copy this file to `~/.claude/commands/` to use it):
- Project: `<repo>`
- Queue tool: `python3 <repo>/linkedin_queue.py`
- Answer bank: `<repo>/data/linkedin_answers.json`
- Sync (optional): `<repo>/data/sync_applied_jobslist.sh`

Never edit `linkedin_queue.json` or `previously_applied.json` by hand. Every status change goes through the queue tool.

## 1. Sync and load

1. If `data/sync_applied_jobslist.sh` exists, run it so applications made by other agents are visible.
2. Run `linkedin_queue.py list`. If nothing is pending, say so and stop.
3. For each pending job, run `linkedin_queue.py check <job_id>`. Mark any that fail with `mark <job_id> skipped "applied elsewhere"` and leave them out of the review.

## 2. Review (the approval gate)

Show the pending jobs as one numbered table: company, title, location, matched role, CV, link.
Then wait for the candidate to say which to submit. Do not open LinkedIn before they answer.
Mark every job they turn down with `mark <job_id> rejected`. Jobs they neither approve nor rejects stay pending.

## 3. Submit the approved jobs

Load the `claude-in-chrome` skill first and follow its rules. Work in a new tab, one job at a time. The candidate must already be logged in to LinkedIn in Chrome; if not, stop and ask them to log in.

For each approved job:

1. Open the job URL. If there is no **Easy Apply** button (external apply, posting closed), `mark <job_id> skipped "<reason>"` and move on.
2. In the resume step, select the uploaded resume whose name matches the job's `cv` field. If none matches, skip the job with that reason.
3. For every screening question, find an answer bank entry whose `patterns` match the question text (case-insensitive regex) and whose `answer` is non-empty. Use `per_role[<role>]` if present.
   - If no entry matches, the answer is blank, or the bank answer does not map cleanly onto the field's options, **skip the job**: close the dialog without submitting and `mark <job_id> skipped "unanswered: <question text>"`.
   - Never invent, infer or round an answer. Contact fields LinkedIn prefills from the profile can be left as they are.
4. On the review screen, untick "Follow company" and submit.
5. Confirm LinkedIn shows the application as sent, then run `record <job_id>`. If submission errored, `mark <job_id> failed "<error>"` instead.

## 4. Wrap up

1. If the sync script exists, run it again so the new entries reach the other copies.
2. Report: submitted, skipped (with reasons), failed, still pending.
3. Gather the unanswered screening questions so the candidate can add them to the answer bank. Do not draft answers for them.
