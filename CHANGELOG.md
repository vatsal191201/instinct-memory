# Changelog

## Unreleased

- Refuse record writes when the existing record cannot be read, parsed, or validated, preserving its original bytes and fact history.
- Propagate lock system errors before storage mutations; distinguish nonblocking contention from acquisition failures.
- Account for every nonblank JSONL input line. Malformed input now stops reconciliation with file/line diagnostics and exit code 1 instead of silently succeeding with partial input.
- Preserve malformed and unselected inbox bytes during snapshot consumption, including invalid UTF-8 and CRLF endings.
- Keep JSONL objects separated when appending to a valid file without a final newline. Reject malformed unterminated destinations before append or consumption, and lock both inbox and archive during archival.
