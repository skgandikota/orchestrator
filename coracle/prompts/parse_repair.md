You returned content that could not be parsed as a valid JSON document of
coracle actions. Below is the raw text you produced.

Return **only** a single JSON object on one line of the form:

```json
{"actions": [{"type": "<one of: tool_call, code_block, file_write, message_to_user, plan_update>", "payload": { ... }, "order": <int>, "dependencies": [<int>, ...]}]}
```

Rules:
- Do not wrap the JSON in markdown fences.
- Do not add any commentary.
- Preserve the original intent of the text as faithfully as possible.
- If you cannot recover any structured actions, return a single
  `message_to_user` action whose payload `{"text": "..."}` contains the
  best plain-text summary of the original output.

Original output:

{{RAW}}
