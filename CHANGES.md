# Changes

## Version 1.0.0b2

- Every instrumentation declares its aspects, as `wrapture.Aspect`
  values in its settings: `statements` (primary, carrying the
  `statement` setting) for the queries, prepared statements, cursors
  and COPY, and `connections` for the open and the transaction
  boundaries. Each is a leaf by its declared default, which the
  bindings used to hardcode, so `leaf = false` is newly possible on a
  driver. An aspect is addressed in the config as a sub-table of the
  entry (`[instrument.connections]`) and takes the recording keys an
  `[[observe]]` entry does; a bare boolean under its name is its
  switch. `statement = true` written flat on the entry reads as
  before, being the primary aspect's key.

- Requires wrapture 1.0.0b4, for `Aspect`.

## Version 1.0.0b1

The first beta. Everything is new in this version, so rather than
listing changes, see the
[README](README.md) for what the package provides: the table of
covered targets there links to each target's own notes describing
what it records and its settings.
