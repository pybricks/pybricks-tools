# Contributing

## Getting set up

```bash
uv sync --all-packages --all-groups
```

That installs all four packages editable plus every tool. VS Code will pick up
`.venv` automatically and format, fix and sort imports on save.

The full gate, identical to CI:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv run properdocs build --strict
```

## Layout

```
pb ──> pb-client ──> pb-core
 └───> pbd        ──> pb-core
```

`pb` depends on `pbd` so that installing the CLI gives a working daemon
autostart, but it must never *import* it — importing is what costs startup time.
There is a test asserting `bleak` is not in `sys.modules` after `pb --help`.

## Async rules

All async code in this repository is **structured**, in the sense of
[Notes on structured concurrency][nsc]. These are not style preferences; several
are enforced by ruff and by tests.

1. **The black-box rule.** When a function returns, every task it started has
   finished. Control flow is a tree. `asyncio.create_task` is the `go` statement
   that article argues against and is banned.
2. **Ongoing events are `@asynccontextmanager` yielding an `AsyncIterator`** —
   never `on_event(callback)`, never a returned task. This is what makes rule 1
   unbreakable from outside the library.
3. **No `timeout=` parameters in public APIs.** Cancellation belongs to a scope.
   Callers use `anyio.fail_after` / `move_on_after`. Where the CLI exposes
   `--timeout`, the command body opens the cancel scope.
4. **Never swallow cancellation.** It is level-triggered, so it keeps arriving
   until the scope exits. Use `except anyio.get_cancelled_exc_class(): ... raise`
   — never a bare `except Exception` around an await. Enforced by ruff `BLE001`.
5. **Teardown that must not be cancelled is a *sync* context manager** where
   possible: a sync `__exit__` cannot be cancelled at all, which is strictly
   stronger than `shield=True`, which merely defers. Restoring the terminal from
   raw mode works this way. Where async teardown is unavoidable, use
   `anyio.CancelScope(shield=True)` with a bounded `move_on_after`.
6. **Async functions all the way down.** No futures, no `call_soon`, no
   Protocol/Transport callback layering in our own code.

### The two sanctioned exceptions

Both are confined to a single documented module, and nowhere else may take a
callback from a third-party library:

- **bleak's notification and disconnect callbacks** are invoked outside our task
  tree. They may only `send_nowait` into a memory object stream drained by an
  owned pump task. They never spawn tasks and never block — blocking there
  stalls the BLE stack.
- **asyncio's proactor named-pipe API on Windows** is exactly the
  Protocol/Transport style rule 6 rejects, but is the only way to get named
  pipes. It is wrapped into an `anyio.abc.ByteStream`.

Separately, and by design, **hub connections outlive the client session that
created them** — that is the entire point of the daemon. This is contained by
having the `HubManager` own the hub's task group, so connecting is *handle
acquisition* against a running supervisor rather than a spawn.

### Why anyio rather than `asyncio.TaskGroup`

Portability is not the reason; bleak pins us to the asyncio backend regardless.
anyio is used for shielded cancel scopes (no stdlib equivalent —
`asyncio.shield()` still cancels the awaiter, which breaks clean hub disconnect
and terminal restore), level-triggered cancellation, and memory object streams
with real close/EOF semantics.

[nsc]: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/

## Errors the user sees

Two kinds of failure, presented differently.

An **expected** failure -- the hub is switched off, nothing is connected, the
program is too big -- is something the user can act on. It is reported as one
sentence saying what happened plus, where there is one, a suggestion of how to
fix it. No traceback: it would only bury the sentence that matters. The wording
follows Pybricks Code, which pairs every alert with a `suggestion` for the same
reason.

An **unexpected** failure is a bug in the tools. Its traceback is printed in
full and unaltered, because that is the most useful thing that can be shown to
whoever ends up reading the report.

Both live in `pb/errors.py`. Commands do not catch their own failures: a task
group wraps whatever its body raised, so `except JsonRpcError` around an
`async with pb_client.connect()` block silently stops matching. `errors.run`
flattens the group, and anything it cannot explain it re-raises. The only
`except` clauses left in `pb/cli.py` are for failures that are a normal answer
to the question asked -- listing hubs when none are connected, stopping a
service that is not running -- and they use `errors.find` for the same reason.

Adding a JSON-RPC error code means adding its wording to `_RULES`; a test walks
`ErrorCode` and fails if one is missing. Dispatch is keyed on the code, never on
message text, so daemon-side wording can be changed freely. Keep daemon messages
factual and let the CLI give the advice: the daemon does not know whether the
user was trying to connect to a hub or to use one.

## Docs

Reference documentation is generated from the code — docstrings for the API,
the cyclopts app for the CLI, the typed method registry for the JSON-RPC
protocol. Do not hand-maintain reference pages; they drift. Narrative pages are
written by hand. Docstrings are Google style.
