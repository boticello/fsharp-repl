# F# REPL

A local F# Interactive service for a project. It offers a persistent session shared by terminals and agents, plus a fresh one-shot evaluation command. The tool does not depend on a particular domain assembly: the consuming project supplies an optional `fsrepl.json` file.

The persistent broker uses FSharp.Compiler.Service and a Unix socket. Submissions are serialised, so definitions remain available to later submissions and evaluations do not interleave. Each completed evaluation is recorded in `.fsrepl/transcript.ndjson`. The one-shot command starts `dotnet fsi` each time and keeps no state.

## Requirements

- .NET 10 SDK
- Python 3.10 or later
- POSIX `sh`, `ps` and Unix sockets (macOS and Linux)
- `git` to install or update from this repository

`just` is optional. It provides short project commands; it is not the installer or runtime. A project can instead call the scripts directly. mise can provide the required tools without changing the REPL interface.

## Add it to a project

From the project root:

```sh
mkdir -p .tools
git clone --depth 1 https://github.com/boticello/fsharp-repl.git .tools/fsharp-repl
printf '\n.tools/fsharp-repl/\n.fsrepl/\n' >> .gitignore
```

Create `fsrepl.json` at the project root. All paths are relative to that root. The `build` argv runs before a new persistent session and before each one-shot call. `references` are passed to the hosted session before loading `preload`; they are especially useful for compiled project modules. All three fields are optional.

```json
{
  "build": ["dotnet", "build", "src/Example.Core/Example.Core.fsproj", "--nologo", "-v", "q"],
  "references": ["src/Example.Core/bin/Debug/net10.0/Example.Core.dll"],
  "preload": "repl.fsx"
}
```

A preload script can contain `open` statements and helper bindings. For example, `repl.fsx` can contain `open Example.Core` after the assembly has been built. For one-shot calls, normal `dotnet fsi --use:repl.fsx` semantics apply. For the hosted session, the preload is evaluated as one interaction so its bindings persist. Its `#r` lines are omitted there; list those assemblies in `references` instead. Paths in `#load` directives in the preload are resolved from the project root; use absolute paths when loading elsewhere. Scripts submitted with `send --file` retain their own normal relative `#load` behaviour.

## Use it

From the project root:

```sh
.tools/fsharp-repl/scripts/fsi-repl.sh start
.tools/fsharp-repl/scripts/fsi-repl.sh send 'let answer = 19'
.tools/fsharp-repl/scripts/fsi-repl.sh send 'answer + 1'
printf '%s\n' 'printfn "hello"' | .tools/fsharp-repl/scripts/fsi-repl.sh send -
.tools/fsharp-repl/scripts/fsi-repl.sh send --file path/to/script.fsx
.tools/fsharp-repl/scripts/fsi-repl.sh status
.tools/fsharp-repl/scripts/fsi-repl.sh log
.tools/fsharp-repl/scripts/fsi-repl.sh stop

.tools/fsharp-repl/scripts/fsi-eval.sh '1 + 2'
printf '%s\n' 'printfn "hello"' | .tools/fsharp-repl/scripts/fsi-eval.sh -
.tools/fsharp-repl/scripts/fsi-eval.sh --file path/to/script.fsx
```

`send` accepts one F# interaction. `send -` reads UTF-8 source from stdin; `send --file` uses FSI's native `#load`, retaining the script's module and relative references. A one-shot argument is an expression whose value is printed; stdin and `--file` are complete scripts. Errors are written to stderr and produce a nonzero exit status. A stuck evaluation blocks later submissions; `stop` terminates the broker so a new session can be started. State inside the process is lost on restart, while the transcript remains.

For `just`, add these recipes to the project's `justfile`:

```just
set positional-arguments := true

fsi *ARGS:
    ./.tools/fsharp-repl/scripts/fsi-repl.sh "$@"

repl-eval *ARGS:
    ./.tools/fsharp-repl/scripts/fsi-eval.sh "$@"
```

Then use `just fsi start`, `just fsi send '1 + 2'`, and `just repl-eval '1 + 2'`. Positional arguments preserve F# quotes and shell metacharacters. For complex multiline input, use stdin.

To update deliberately to the latest default branch after installation:

```sh
git -C .tools/fsharp-repl pull --ff-only
```

Restart a running session after changing the tool or `fsrepl.json`. The [F# and C# starter](https://github.com/boticello/fsharp-csharp-starter) includes the config and `just` install/update recipes in generated projects.

## Development

Build the broker and run the focused tests from this repository:

```sh
dotnet build dotnet/tools/fsrepl/fsrepl.fsproj --nologo -v q -p:TreatWarningsAsErrors=true
python3 dotnet/tools/fsrepl/test_broker.py
python3 scripts/test_fsi_commands.py
```

The broker protocol is newline-delimited JSON over a project-specific local socket. The socket is for code execution by the current user, not a network service. The state directory defaults to `<project>/.fsrepl`; `FSREPL_PROJECT_ROOT`, `FSREPL_STATE_DIR` and `FSREPL_RUNTIME_DIR` can override paths for scripts and tests. `FSREPL_RUNTIME_DIR` defaults to `/tmp`, with a fallback to `/tmp` when an override would exceed the platform's Unix socket path limit.

## Licence

MIT. See [LICENSE](LICENSE).
