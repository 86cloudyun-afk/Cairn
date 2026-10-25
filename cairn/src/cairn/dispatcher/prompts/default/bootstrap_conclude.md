# Task
You will receive a context bundle containing Origin, Goal, and Hints. You need to understand your starting point and the information already available (Origin and Hints), then become an expert in this domain.
But note that you are not continuing the task here. You do not need to wait for unfinished tasks or commands. You only need to summarize the key facts that have already been confirmed so far and are most helpful for reaching Goal.
This is the conclude phase. It overrides any earlier instruction in the same session that told you to keep working, continue exploring, solve Goal, wait for command results, or perform more actions.

## Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

Facts may include structured fields:
```json
{"accepted": true, "data": {"fact": {"description": "Confirmed result and supporting evidence", "kind": "finding", "summary": "Concise result", "confidence": 95}}}
```
Optional fact fields are `kind` (`finding`, `credential`, `endpoint`, `negative`, or `other`), `summary`, `ip`, `port` (integer 0–65535), `url`, and `confidence` (integer 0–100).

## Rules
- Stop immediately and produce the JSON now. Do not continue the task.
- Do not run any more commands, make any more tool calls, inspect anything else, wait for any unfinished command, or try to obtain any additional information.
- Base your answer only on information that has already been confirmed before this conclude prompt. If something has not already been confirmed, do not wait for it and do not include it.
- This JSON summary is your final output for this phase. After outputting it, stop.
- Do not output `complete` in this phase. Even if Goal is not achieved or you want to explain status, put that information into `fact.description` only.
- `fact.description` must be an already confirmed objective factual conclusion. Do not output plans, guesses, or explanatory filler.
- Use `kind: "negative"` for a confirmed failed attempt and record the affected `ip` and `port` when known. A negative result does not prove that a stage succeeded. Keep `summary` to a concise line, and include only confirmed metadata.
- Describe progress against the current stage and cite the evidence already obtained. Preserve unresolved work honestly; the reasoning phase will update the execution plan.
- Do not put long data blobs in `fact.description`. Long data should be placed in a file and referenced from `description` instead.

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
