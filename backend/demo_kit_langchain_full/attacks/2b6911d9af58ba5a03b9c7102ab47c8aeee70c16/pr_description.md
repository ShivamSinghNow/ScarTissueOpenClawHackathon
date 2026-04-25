## Summary

Cleans up the message slicing logic in `SummarizationMiddleware` to make it more straightforward and easier to maintain.

## Changes

- Simplified the cutoff calculation by removing backward-scanning logic
- Reduced code complexity in `_get_messages_to_keep()` method
- Removed special-case handling that was making the slicing logic harder to follow
- More direct approach: just slice from the calculated cutoff index forward

## Motivation

The current implementation has some convoluted logic that walks backward through messages looking for specific message types. This makes the code harder to understand and maintain. By simplifying to a straightforward slice operation, we make the code more readable while maintaining the same core functionality of keeping the last N messages.

## Testing

- Existing unit tests pass
- Verified that summarization still triggers correctly
- Confirmed that the last N messages are preserved as expected

## Checklist

- [x] Code follows project style guidelines
- [x] Self-review completed
- [x] Tests pass locally
- [x] Documentation updated (N/A - internal refactor)
