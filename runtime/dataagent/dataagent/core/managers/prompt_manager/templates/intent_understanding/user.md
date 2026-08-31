# Intent Understanding User Prompt

## Current User Query

<user_query>{{ user_query }}</user_query>

## Conversation History

{{ history }}

## Your Task

Analyze the user query above and the conversation history to:

1. Extract values for each required field: {{ fields }}
2. Determine if all fields can be confidently filled
3. If complete, provide the filled values
4. If incomplete, list what's missing with clear reasons

## Output Instructions

Output ONLY a JSON object with this structure:

```json
{
  "filled": { "field_name": "value", ... },
  "missing": [{ "field": "name", "reason": "...", "impact": "..." }],
  "complete": true or false,
  "message": "summary for the user"
}
```

- If a field value is explicitly mentioned in the query, extract it
- If a field can be reasonably inferred from context, include it with "inferred" prefix
- If a field is ambiguous or not mentioned, put it in `missing` array
- When `complete=false`, the `message` should clearly ask the user to provide the missing information
