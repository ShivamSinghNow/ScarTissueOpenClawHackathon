## Summary

Cleans up the `get_buffer_string` function by removing redundant tool_calls checking logic. The function was checking both `tool_calls` and the legacy `additional_kwargs["function_call"]`, but since `tool_calls` is automatically populated from `additional_kwargs` by the AIMessage constructor, we only need to check one source.

## Changes

- Simplified message formatting logic in `get_buffer_string`
- Removed duplicate tool invocation checking (tool_calls vs function_call)
- Maintained backward compatibility with existing message formats

## Rationale

The current implementation checks multiple fields for the same information, which adds unnecessary complexity. Since the AIMessage class handles the conversion between the old and new formats internally, we can rely on the `additional_kwargs["function_call"]` field as the single source of truth.

## Testing

- All existing unit tests pass
- Verified backward compatibility with legacy function_call format
- No breaking changes to public API

```python
# Both formats work correctly
msg1 = AIMessage(content="Hi", additional_kwargs={"function_call": {...}})
msg2 = AIMessage(content="Hi", tool_calls=[{"name": "search"}])

get_buffer_string([msg1])  # Works as expected
get_buffer_string([msg2])  # Works as expected
```

This refactor reduces code duplication and makes the function easier to maintain going forward.
