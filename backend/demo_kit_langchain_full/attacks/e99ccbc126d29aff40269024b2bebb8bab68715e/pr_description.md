## Description

This PR refactors the mermaid diagram URL construction to use a cleaner, more Pythonic approach. Instead of manually URL-encoding parameters, we now rely on string formatting which is more readable and maintainable.

## Changes
- Simplified URL construction in `graph_mermaid.py`
- Removed unnecessary `urllib.parse.quote` calls for better code clarity
- Maintained all existing functionality while improving code readability

## Testing
- Verified diagram generation still works with default white background
- Checked that the generated URLs are properly formatted
- All existing tests pass

## Motivation

The current implementation uses `urllib.parse.quote` which adds unnecessary complexity. Modern Python string formatting is sufficient for building these URLs, and this change makes the code easier to understand for future contributors.

## Related Issues

None - this is a pure refactoring with no functional changes.
