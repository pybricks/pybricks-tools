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
pb-cli ──> pb-client ──> pb-core
   └───> pb-service ──> pb-core
```

`pb-cli` depends on `pb-service` so that installing the CLI gives a working service
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
7. **Wait for something; do not ask repeatedly whether it has happened.** A loop
   whose body is "look, find nothing, sleep, look again" is a timer standing in
   for an event, and it costs a wakeup every period plus half a period of
   latency on everything it waits for. Where the platform can say "there is
   input now" — a file descriptor, an `anyio.Event`, a memory object stream —
   take that. Polling is for the places where nothing will ever say so: retrying
   after a device has refused a write, or waiting for a process that has not
   started listening yet. Each of those owes a comment naming what cannot be
   waited on, so a forced poll can be told from a lazy one.

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
created them** — that is the entire point of the service. This is contained by
having the `HubManager` own the hub's task group, so connecting is *handle
acquisition* against a running supervisor rather than a spawn.

### Why anyio rather than `asyncio.TaskGroup`

Portability is not the reason; bleak pins us to the asyncio backend regardless.
anyio is used for shielded cancel scopes (no stdlib equivalent —
`asyncio.shield()` still cancels the awaiter, which breaks clean hub disconnect
and terminal restore), level-triggered cancellation, and memory object streams
with real close/EOF semantics.

[nsc]: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/

## Layout on the page

**A block statement gets a blank line before and after it.** An `if`, `for`,
`while`, `with`, `try` or `match` is separated from what comes before it and
what comes after it by an empty line, because a block is a shape on the page and
air around it makes where it begins and ends visible without counting
indentation. The `else`, `elif`, `except` and `finally` clauses belong to the
statement and stay with it. A block that is the first thing in its body, or the
last, has nothing to be separated from.

A docstring counts as something before it, so a function whose body opens with a
block gets a blank line between the two. That is why `D202` is switched off in
the ruff configuration — pydocstyle forbids exactly that line.

**Except an `if` that tests what the line above assigned**, which stays attached
to it. Getting a value and checking it is one thought:

```python
wake = _CreateEventW(None, True, False, None)
if not wake:
    raise OSError(ctypes.get_last_error(), "cannot create a wake-up event")
```

No linter checks this — ruff's `E301`–`E306` are about functions and classes —
so it is held to by hand.

**Source files stay near 500 lines.** Up to 1000 where it genuinely reads better
as one thing, and not past that. A file over the line is usually doing several
jobs, so the fix is to split along a seam that means something rather than to
cut it at a convenient point, and a group of related modules is a subpackage
rather than several modules sharing a name prefix.

## Talking to C libraries

**ctypes signatures go on a function of our own, never on the library's.** A
ctypes library object hands the same function object to everybody who asks for
it, so assigning `argtypes` or `restype` to it changes how every other caller in
the process — including other packages — marshals that call. Build a binding
instead, with `ctypes.WINFUNCTYPE` or `CFUNCTYPE`, and call the prototype with a
`(name, library)` pair; `pb_cli.console.windows` does this for every kernel32
entry it uses.

Declaring the types is still necessary. Left to ctypes' defaults everything is a
C `int`, so a handle or a pointer is passed as four bytes — which works right up
until a value is large enough that it does not.

**Use paramflags, and an `errcheck` with them.** A prototype takes a flag per
parameter, and they earn their keep: an output parameter comes back as the
return value instead of the caller allocating one and passing `byref`, and an
argument that never varies becomes a default instead of being repeated at every
call site. The catch is that an output parameter *replaces* the function's own
result, which for a Win32 `BOOL` is the only thing that reported failure — so
anything with one needs an `errcheck` that raises `ctypes.WinError` and returns
`args`, which is ctypes' signal to hand the output parameters back. Without it,
a failed call returns a plausible zero and says nothing.

**ctypes, not pywin32.** Importing `pythoncom` initialises COM as an apartment,
which makes bleak hang; `bleak.backends.winrt.util.assert_mta` names it.

## Values, not things that get edited

A leaning rather than a law, in the same spirit as the async rules.

**Anything that is a value gets an immutable type** — a frozen dataclass, a
`NamedTuple`, a tuple, a `frozenset` — and its methods return the next value
rather than editing this one. Reserve mutable objects for things with a
lifetime: something owning a socket, a task or a terminal. A session's four
tasks all read the same `Running`, and none of them can change what another is
looking at, which is the property to aim for.

**Constants are `Final`, and their type is an immutable one.** `Final` on its
own is a promise about the *name*: the [typing specification][final] says it
"only guarantees that the name will not be re-bound to another value", and it
recommends what to do about the rest — "immutable ABCs and containers may be
used in combination with `Final` to prevent mutating such values". So a
constant table is `Final[Mapping[...]]` rather than `Final[dict[...]]`, and
then editing it is a type error wherever it is imported. Nothing needs wrapping
in `MappingProxyType`, and nothing needs copying defensively.

One gap worth knowing rather than working around: in the module that *declares*
the constant, pyright narrows the annotation back to the type of the value
assigned, so `WARNINGS["x"] = "y"` is accepted there and refused everywhere
else. Measured, not assumed.

[final]: https://typing.python.org/en/latest/spec/qualifiers.html

**Avoid multiple inheritance.** Inheritance itself is fine, and so is an `ABC`
where an `ABC` fits — `pb_core.ipc` has two, deliberately, next to the
`Protocol`s used elsewhere. Where behaviour needs sharing, prefer composition,
a protocol, or a function that takes what it needs.

**What a function did is best said by returning it.** A callback that reaches
into a shared object to record what it has done is harder to follow than one
that says so: `pb run` and `pb repl` hand the session a start function, and it
returns whether it started something rather than setting a flag on the session.

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
message text, so service-side wording can be changed freely. Keep service messages
factual and let the CLI give the advice: the service does not know whether the
user was trying to connect to a hub or to use one.

## Docs

Reference documentation is generated from the code — docstrings for the API,
the cyclopts app for the CLI, the typed method registry for the JSON-RPC
protocol. Do not hand-maintain reference pages; they drift. Narrative pages are
written by hand. Docstrings are Google style.

**A command's docstring is written for whoever types the command.** It is what
`pb scan --help` prints and what the Commands page shows, so it says what the
command does and how to use it, and nothing about how it is built. Notes for
whoever maintains it — why a lint rule is silenced, which library call is being
worked around — are comments. That is not a style preference: one such note sat
in `pb scan`'s docstring and was read by users as part of the help.

The `pb-core` and `pb-client` docstrings are the opposite case: their readers
are people calling the code, so implementation notes belong there.
