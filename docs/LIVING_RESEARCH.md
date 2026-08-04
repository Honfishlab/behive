# Living Research Projects

This fork extends BeHive from discrete research missions into durable investigations.
The unit of work is no longer a chat response; it is a research project whose evidence,
questions, contradictions, and unanswered gaps continue to evolve.

## Core loop

1. Create a project with a root research question.
2. Decompose it into a tiered question tree.
3. Attach each question to a BeHive mission and ingest sourced claims as findings.
4. Re-rank the open frontier from uncertainty, evidence gaps, contradictions, priority,
   staleness, and question depth.
5. Propose new research threads as explicit leads, never as findings.
6. Render `/projects/{id}/map` as nodes and typed edges in any graph UI.

## API surface

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/projects` | Create a living investigation and root question |
| `POST` | `/projects/{id}/questions` | Add a child or independent question |
| `GET` | `/projects/{id}/map` | Get graph-ready questions, findings, and edges |
| `GET` | `/projects/{id}/frontier` | Rank the next questions worth researching |
| `GET` | `/projects/{id}/suggestions` | Generate follow-up leads from questions and entities |

## Design principles

- Findings require evidence; generated text is only a hypothesis or research lead.
- Contradictions increase research priority instead of being smoothed into a summary.
- The question tree may change as evidence arrives.
- Every relationship is typed so the graph remains inspectable and exportable.
- Scoring is deterministic and explainable before an optional learned planner is added.

## Next milestones

- Attach completed mission claims to questions with novelty and contradiction detection.
- Add a scheduler/worker that runs the highest-ranked frontier within a configurable budget.
- Add a web workspace for the question tree, evidence graph, timeline, and review queue.
- Add human approval gates, source-policy rules, and per-project discovery budgets.
