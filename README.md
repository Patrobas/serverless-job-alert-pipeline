# Serverless Job Alert Pipeline

An event-driven, serverless pipeline on AWS that monitors job boards and company
career pages (including Anthropic's), deduplicates postings against what's already
been seen, scores fit against a defined candidate profile, and sends digest-style
notifications instead of one alert per job.

Built as a hands-on AWS portfolio project while studying for the SAA-C03 exam.
Every architectural choice below maps to something the exam actually tests.

## Problem

Job board alerts (LinkedIn, Indeed) are noisy: they repeat postings, don't check
company career pages directly, and give no signal on actual fit before you click
through. This pipeline pulls from multiple sources on a schedule, filters out
what's already been seen, ranks by fit, and delivers one digest instead of a
stream of one-off emails.

## Who it's for

Me, first, an active job search running against a defined role profile
(Solutions/Implementation Consultant, Cloud Solutions Consultant, etc.). Built to
be reusable for any defined search criteria.

## Architecture

```
EventBridge (scheduled rule)
    ↓
Lambda: Fetcher        → pulls postings from configured sources
    ↓
DynamoDB (JobPostings)  ← dedup + storage, TTL auto-cleanup
    ↓
Lambda: Matcher         → scores fit against profile, flags complexity
    ↓
Lambda: Digest Builder  → batches new matches into one message
    ↓
SNS Topic               → email (SMS/Telegram as later subscribers)
```

**Why these services** (see `docs/architecture-decisions.md` for the full writeup):

- **EventBridge over cron-on-EC2**, no server to maintain, no SPOF.
- **Lambda, split into three functions**, each fails independently; pay only
  per invocation.
- **DynamoDB, composite key `source` + `job_id`**, O(1) dedup lookups, no
  joins needed, fits the pure key-value access pattern.
- **SNS**, decouples matching logic from delivery channel.
- **IAM roles scoped per function**, least privilege, not one shared role.

## MVP scope

**Search scope:** fully remote roles anywhere in Canada or across North America,
hybrid or remote roles in my home city (Calgary), and US remote roles that are open
to Canadians (flagged for a manual check). A job in another Canadian city only counts
if it is remote. Edit `lambdas/fetcher/profile.json` and `lambdas/matcher/scoring.json`
to point the pipeline at your own roles and locations; no code changes needed.

**In scope for V1:**
- Sources on public job APIs: Greenhouse, Lever, Ashby, Workable and RemoteOK
- A cheap title and location pre-filter, so only plausible jobs are stored and scored
- Dedup via a DynamoDB conditional write, with expiry refreshed on re-seen jobs
- Rules-based fit score with a short reason (Matcher)
- One email digest via SNS with direct application links (Digest Builder)
- A CloudWatch alarm that emails me if the Fetcher fails

**Out of scope for V1** (candidates for later phases):
- Amazon Bedrock scoring (V2 swaps the Matcher's internals; the score and reason format stays the same)
- Simple / Medium / Complex application flag
- Telegram/SMS delivery (SNS makes this an additive change later)
- Step Functions orchestration
- Sources without a public API

## Roadmap

- **V1: a small version that works**
  - [x] Base stack deployed (DynamoDB, SNS, IAM roles)
  - [x] Fetcher code with tests
  - [x] Fetcher deployed and run by hand (24 sources, dedup proven)
  - [x] Matcher (rules-based), Digest Builder and EventBridge schedules written and tested
  - [ ] Matcher, Digest and schedules deployed and first email received
  - [x] CloudWatch alarms on all three functions (in the template)
- **V2: Bedrock Matcher**, Simple/Medium/Complex flag
- **V3: more sources, Step Functions, other delivery channels**

## Tech stack

- **Language:** Python 3.12 (Lambda runtime)
- **IaC:** AWS CloudFormation (no manual console changes)
- **Infra:** Lambda, DynamoDB, SNS, EventBridge, IAM, CloudWatch, S3 (source config)
- **Local tooling:** AWS CLI, cfn-lint for template validation

## Repo structure

```
infrastructure/    CloudFormation templates
lambdas/            One folder per Lambda function
  fetcher/
  matcher/
  digest_builder/
config/             Job source configs, candidate profile for scoring
docs/               Architecture decisions, write-up drafts
```

## Status

In progress. The base stack and the Fetcher are deployed and working against 24
sources. The Matcher, Digest Builder and daily schedules are written and tested
(`python -m pytest tests -q`) and are the next thing to deploy.

How a job moves through the pipeline: `new` (Fetcher) to `scored` (Matcher) to
`sent` (Digest Builder). Each stage only looks for jobs in the status it owns,
which is also what stops the same job being emailed twice.

Two stacks, on purpose: `base-stack.yaml` holds the foundation that rarely
changes (table, topic, roles); `functions-stack.yaml` holds the code that
changes often. A mistake in the second cannot touch the first.
