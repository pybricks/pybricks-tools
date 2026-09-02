# Environment variables

## Any option, spelled `PB_`

Every option of every command can be set in the environment by putting `PB_` in
front of its name in capitals. `--hub` is `PB_HUB`, `--slot` is `PB_SLOT`,
`--lines` is `PB_LINES`. What you type on the command line always wins.

```bash
export PB_HUB="Rover"
pb run demo.py              # on Rover
pb run demo.py --hub Bumblebee   # on Bumblebee after all
```

One prefix is easier to remember than a list of blessed variables, which is why
it is every option rather than a chosen few. It costs little to be generous
here: an environment variable belongs to one shell and one moment, unlike the
[project file](pyproject.md), which is committed and read by everybody.

A variable that means nothing to the command being run is ignored, so `PB_HUB`
can sit in your shell profile without upsetting `pb check`, which has no hub.

`PB_HUB` is the one worth knowing. It sits between what you type and what the
project file says, which makes it the right way to point one shell, or one CI
job, at a different hub without editing anything.

`PB_TRANSPORT` is the other one: `usb` or `ble`, for a shell that should always
reach the hub the same way. Left alone, `pb` uses whichever transport works and
prefers USB where a hub is reachable both ways.

!!! note "Flags take a word, and only once"
    An option that is just a flag needs a value here: `PB_FOLLOW=true`, not an
    empty string. `PB_VERBOSE=1` gives you what `-v` gives you, and there is no
    way to spell `-vv` this way — repeating a flag is something only the command
    line can express.

## Variables that are not options

These are for tests, sandboxes and the odd bit of troubleshooting. You should
not need any of them.

| Variable | What it does |
| --- | --- |
| `PB_ENDPOINT` | Where the background service listens: `unix:///path`, `pipe://name` or `tcp://host:port`. Both halves have to agree, so set it for the service as well as for `pb`. |
| `PB_RUNTIME_DIR` | Moves the service's socket, lock and shared secret together, so a sandbox can have its own without touching yours. |
| `PB_STATE_DIR` | Moves what the service remembers between runs, which today is the hub chosen with `pb use`. |
| `PB_SERVICE_CMD` | What to run when a command has to start the service. |
| `PB_HUB_BACKEND` | Which transports the service uses at all: `usb`, `ble`, or `fake` for a simulated hub, so the whole tool can be exercised on a machine with no radio and nothing plugged in. Both real transports by default. Not the same as `PB_TRANSPORT`, which chooses between the transports the service has. The service says so in its log when the simulated hub is on. |

On Linux the usual `XDG_RUNTIME_DIR` and `XDG_STATE_HOME` are honoured, and
everything written goes where the XDG Base Directory specification says it
should, so `PB_RUNTIME_DIR` and `PB_STATE_DIR` are only for putting things
somewhere unusual.
