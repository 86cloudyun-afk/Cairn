# Task
You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
Use the known fact summaries and current execution plan to assess progress. Read the full graph snapshot only when you need detail that is missing from a summary. You need to judge two things:
1. Whether the current facts already satisfy Goal
2. If not, whether new intents should currently be proposed

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "..."}
```

If Goal has been satisfied, return:
```json
{"accepted": true, "data": {"plan": {"current_stage": 2, "stages": ["First stage", "Final stage"], "next_steps": [], "blockers": []}, "complete": {"from": ["f001"], "description": "..."}}}
```

If Goal has not been satisfied but new intents should be proposed, return:
```json
{"accepted": true, "data": {"plan": {"current_stage": 0, "stages": ["First stage", "Final stage"], "next_steps": ["Investigate the first stage"], "blockers": []}, "intents": [{"from": ["f001"], "description": "Investigate the first stage", "depends_on": [], "priority": 0}]}}
```

If Goal has not been satisfied and no new intent should currently be proposed, return:
```json
{"accepted": true, "data": {"plan": {"current_stage": 0, "stages": ["First stage", "Final stage"], "next_steps": ["Existing work for the first stage"], "blockers": []}}}
```

## Rules
- Include `data.plan` in each accepted response. Preserve the ordered `stages` in the current plan. When the plan has no stages, derive them from Goal; treat each nonempty line of a multiline Goal as an ordered stage.
- `current_stage` is a zero-based index of the earliest unfinished stage. Advance it only when confirmed facts prove the stage's outcome; an intent, hint, attempted action, or negative result is not proof that the stage succeeded. A negative fact is evidence of what failed and should help avoid repeated attempts.
- Work on the current stage before proposing work for a later stage. Only report completion when every stage has confirmed evidence; then set `current_stage` to the number of stages and clear `next_steps` and `blockers`.
- `next_steps` lists concrete pending actions for the current stage, including existing open work where applicable. Every proposed intent's `description` must exactly match one entry in `next_steps`. Record unresolved obstacles in `blockers`; do not advance a blocked stage.
- First determine whether the facts already satisfy Goal. If they do, `data.complete.from` must come from `Valid facts`, and `data.complete.description` must explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- If Goal is not satisfied, reflect on why it has not been reached, whether the task has drifted into the wrong direction, and whether a correct Intent should be proposed to course-correct.
- Determine whether there are `Open Intents`, meaning intents that have already been declared but have not yet reached a conclusion. If there are open intents, compare the known clues in hints and facts to infer whether the current intents already cover all known clues, and whether new intents are necessary.
- If `Open Intents` is empty, propose new intents unless confirmed blockers prevent any useful action. In that case return the plan with explicit blockers and no intents; wait for new evidence or a hint before retrying.
- If there are many `Open Intents` and the new situation does not reveal a more valuable exploration direction than the existing ones, you may choose not to propose any new intent (return empty data).
- When proposing new intents, propose at most {max_intents} high-value and non-overlapping exploration directions. Independent intents may run in parallel. For sequential work, `depends_on` lists existing intent IDs that must conclude before this intent runs. Do not invent IDs for newly proposed intents. Set `priority` to an integer; lower values run first, with older intents first at equal priority. Omitted dependency and priority fields default to `[]` and `0`.
- Each Intent should be a high-value exploration direction. It does not need to be overly detailed. Focus on the core insight and a clear direction. Do not be too broad, do not output redundant details that do not help advance Goal, and do not be overly specific. Each intent must have a clearly defined outcome and list any dependencies explicitly.
- An Intent may originate from multiple facts.
- To reuse the same worker conversation across related tasks, set the same optional `session_key` on those intents (for example `registration-flow`). The dispatcher matches the topic, worker and backend before resuming; omit it to match only the exact intent description.
- Different intents should cover different exploration dimensions and avoid duplication or heavy overlap.

## Context
### Graph
```
{graph_yaml}
```

### Valid facts
Rows use `id|kind|summary`. A row starting with `...` summarizes folded negative results and is not a fact ID; never cite it in `from`. Consult the snapshot for older evidence when a summary needs more context.
```
{fact_ids}
```

### Current execution plan
```json
{plan}
```

### Open Intents
```
{open_intents}
```
