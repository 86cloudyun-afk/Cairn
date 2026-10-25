# Task
You will receive a context bundle containing Origin, Goal, Hints, and the current execution plan. Understand your starting point and the available information, then drive the task forward by establishing the current stage's result.

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Only return the following after you have confirmed that Goal has been satisfied:
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

Facts may include structured fields:
```json
{"accepted": true, "data": {"fact": {"description": "Confirmed result and supporting evidence", "kind": "finding", "summary": "Concise result", "confidence": 95}, "complete": {"description": "Why the result satisfies Goal"}}}
```
Optional fact fields are `kind` (`finding`, `credential`, `endpoint`, `negative`, or `other`), `summary`, `ip`, `port` (integer 0–65535), `url`, and `confidence` (integer 0–100).

For a Goal with multiple stages, finish the current stage and return its confirmed evidence without `complete` so the reasoning phase can advance the plan:
```json
{"accepted": true, "data": {"fact": {"description": "Evidence confirming the current stage", "kind": "finding", "summary": "Current stage result"}}}
```

# Rules
- For a Goal with one stage, keep working until the problem is solved. For multiple stages, work on the current stage and return its fact as soon as that stage has a confirmed result.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this keep-working rule immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- Output `complete` only if a Goal with one stage has already been definitively achieved in this session. For multiple stages, return a fact for the current stage and leave overall completion to the reasoning phase. If the current stage is unfinished, keep working until a conclude-phase instruction replaces this task.
- `fact.description` must clearly state the confirmed key objective results. For example, in a CTF scenario, it may include multiple flags, shells, privilege proofs, key exploitation results, and similar evidence.
- Use `kind: "negative"` for a confirmed failed attempt and record the affected `ip` and `port` when known. Negative results describe what failed; they do not prove that a goal stage succeeded. Keep `summary` to a concise line, and include only confirmed metadata.
- Follow the current execution plan in order. Treat `current_stage` as the first unfinished stage, establish its result, and return the confirmed evidence before working on later stages. The reasoning phase will update the plan and decide completion for a Goal with multiple stages.
- `complete.description` should explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- Do not put long data blobs in `description`. Long data should be placed in a file and referenced from `description` instead.

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```
{hints}
```

## Current execution plan
```json
{plan}
```
