# pybricks-client

Talk to a [Pybricks](https://pybricks.com) hub from your own Python program.

```python
import anyio
import pb_client


async def main() -> None:
    async with pb_client.connect() as pb:
        hub = await pb.connect_hub(name="Rover")
        print(f"connected to {hub.name}, firmware {hub.firmware}")


anyio.run(main)
```

The connection is held by a background service that outlives your program, so
the next one to run finds the hub already there. It is async throughout, and
everything it opens is closed for you when the `async with` block ends.

**Looking for the command line?** That is `pybricks-cli`, which installs `pb`
and uses this library underneath.

> **Status:** beta. Commands and APIs may still change before 1.0.0.
