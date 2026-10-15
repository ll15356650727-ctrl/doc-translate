---
product: Northstar
version: 1
---

# Guide to objective management

A team lead can create an objective. A team member can only update a key result assigned to them.

## Update progress

1. Open the objective details and select the key result to update.
2. Enter a progress note and save. Progress updates do not require approval from the team lead.

| Field | Description |
| --- | --- |
| Deadline | 2026-12-31 |
| Reminder threshold | Notify the team lead when completion falls below 60% |

> A deleted objective cannot be restored. Please confirm before deleting it.

See the [Help Center](https://example.org/help). The API field `goal_id` and the variable {member_name} must remain unchanged.

```json
{"goal_id": "goal-001", "progress": 60}
```
