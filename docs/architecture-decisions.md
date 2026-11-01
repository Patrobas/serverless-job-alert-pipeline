# Serverless Job Alert Pipeline: Scout, Score, Send

## What it is

A small system on AWS that checks 24 company career pages every morning, scores each job against what I am looking for, and emails me the best ones. Nothing runs on a server I manage, and it costs close to nothing.

Plain version: a scout finds the jobs, a scorer ranks them, a messenger emails me the best. Each one only touches jobs at its own stage.

## Say it out loud

**30 seconds**
I built a pipeline that checks 24 company career pages every morning, scores each job against what I am looking for, and emails me the best ones. It runs on Lambda, DynamoDB, EventBridge and SNS, so there are no servers to manage and it costs almost nothing.

**2 minutes**
It has three stages. Scout pulls open jobs from public career-page APIs, drops the obvious misses and saves the rest. Score gives each job a number from 0 to 100 based on the title, whether it is remote in Canada or hybrid in Calgary, and penalties for roles like sales. Send picks the best ones, merges duplicates, and emails me through SNS. Each stage is its own Lambda, started by an EventBridge schedule, and they hand work to each other through a status field in DynamoDB.
The part I learned the most from was the first real email. It had European roles and engineering jobs I did not want, and it linked only to the apply form. I tightened the filters, added penalties, and added a link to the full job description. Later I found the live score bar was lower than my template said, because CloudFormation kept an old setting. I now pin it at deploy time.

## The three stages

| Stage | What it does | AWS service | Why this one |
|---|---|---|---|
| **Scout** | Pulls open jobs from 24 public career-page APIs, drops obvious misses (wrong region, in-person outside Calgary), saves the rest as "new" | Lambda (Python) | Short job that runs a few minutes a day. Paying per run beats paying for a server that sits idle. |
| **Score** | Gives each new job 0 to 100: title match, remote or Calgary hybrid, penalties for sales and marketing roles. Marks it "scored" with a reason | Lambda (Python) | Same reasoning. Rules live in a JSON file, so I can tune scoring without changing code. |
| **Send** | Picks the best jobs above the bar, merges the same role listed in several cities, caps 3 per company, emails me, marks them "sent" | Lambda + SNS | SNS handles email delivery and unsubscribes. I did not have to build or run a mail server. |

Supporting pieces:
- **EventBridge:** the alarm clock. Three schedules, 10 minutes apart, start Scout, Score, then Send each morning.
- **DynamoDB:** the shared notebook. Every job has a status (new, scored, sent). On-demand billing, and old jobs expire on their own after 45 days.
- **IAM:** each function gets only the permissions it needs. Scout can write, Score can read and update, Send can read and publish.
- **CloudWatch:** logs and error alarms on all three functions.
- **CloudFormation:** the whole thing is defined as code in two stacks, so it deploys the same way every time.

## Decisions worth explaining

1. **Serverless over EC2.** The work is a few minutes a day. A server would idle 99% of the time and need patching.
2. **Three small functions, not one big one.** Each can fail, be tested and be changed on its own. Statuses in DynamoDB are the hand-off.
3. **No duplicate alerts.** A conditional write ("only save if this job does not exist") checks and saves in one step, so two runs can never both think a job is new. The Send step publishes first and marks sent second, so a failure means a repeat email, never a lost one.
4. **Cheap filter first, careful scoring second.** Scout throws away obvious misses so I am not scoring thousands of jobs. Score handles the nuance.
5. **Public APIs only.** Greenhouse, Lever, Ashby and Workable publish job feeds. I did not scrape sites that forbid it.
6. **Least privilege.** One role per function.

## What I fixed after seeing real output

- First email had EU roles and engineering jobs, and linked only to the apply form. Fixed with filters, title penalties, and a "Read the job" link.
- On-site Toronto roles got through. Rebuilt location logic: remote anywhere in Canada or North America, hybrid only in Calgary, in-person elsewhere dropped.
- One company filled the whole email. Added merging of the same role across cities and a cap of 3 per company.
- The live score bar was lower than the template said, because CloudFormation kept an old parameter value. Now pinned at deploy.

## Honest limits and next steps

- Scoring is rules-based. Next version: Amazon Bedrock to read the full description and judge fit.
- Step Functions would replace the staggered schedules with a proper workflow.
- The table is scanned, not indexed. Fine at hundreds of jobs. An index on status is the upgrade at scale.
- Tests (pytest with moto) prove my own logic, not what the company APIs return.

## Likely interview questions

**Why serverless?**
Short, scheduled work. I pay only when it runs, and there is nothing to patch.

**Why DynamoDB and not RDS?**
I look jobs up by a key and update a status. No joins. It is cheap on-demand, and TTL cleans up old rows for free.

**How do you avoid duplicate emails?**
A conditional write on save, plus statuses. A job moves new, scored, sent and each stage only picks up its own status.

**What happens if something fails?**
One broken company source does not stop the others. If every source fails, the function errors and a CloudWatch alarm fires. If the email fails, jobs stay unsent and go out next run.

**How is it secured?**
A separate IAM role per function with only what it needs. No credentials in code.

**How would you scale it?**
Add a status index instead of scanning, move to Step Functions for orchestration, and add Bedrock for scoring.

**What would you change?**
Replace rules with Bedrock scoring, and add deploy automation from GitHub.

**What did you learn?**
Real output exposes problems tests miss. I tuned filters and scoring from actual emails, and found a deploy setting that silently kept an old value.
