module FsRepl.Program

// fsrepl — a shared REPL broker for the consuming F# project.
//
// One long-lived FsiEvaluationSession (FSharp.Compiler.Service) hosts the
// project: at boot it evaluates the optional configured preload script, so
// its bindings and later submitted definitions persist for the life of the
// process. Clients talk JSON over a Unix-domain socket — one request line in,
// one response line back, one evaluation at a time (a serial accept loop is
// the queue: a long evaluation delays later submissions, it never interleaves
// them). Every completed evaluation is appended to the transcript as one
// NDJSON line — the shared view agents and humans tail.
//
//   request:  {"id": "...", "op": "ping" | "eval", "code": "..."}\n
//   response: {"id","ok","value","stdout","stderr","diagnostics","exception"}
//
// `eval` tries EvalExpressionNonThrowing first (so expression values come
// back in "value"); when the code does not compile as an expression (a `let`,
// an `open`, a directive) the error diagnostics prove nothing executed, and
// it re-runs as an interaction — side effects cannot double-run.
//
// Safety posture: local Unix socket only (same-user trust), arbitrary code by
// design (it is a REPL), no timeout enforcement — a runaway evaluation blocks
// the queue and recovery is broker restart; the transcript plus checkpoint
// scripts are the durable record, never the process.
//
// References and preload are supplied by the consuming project's fsrepl.json.

open System
open System.IO
open System.Net.Sockets
open System.Text
open System.Text.Json.Nodes
open FSharp.Compiler.Interactive

let private severityIsError (d: FSharp.Compiler.Diagnostics.FSharpDiagnostic) =
    d.Severity = FSharp.Compiler.Diagnostics.FSharpDiagnosticSeverity.Error

let private formatDiagnostics (diags: FSharp.Compiler.Diagnostics.FSharpDiagnostic[]) =
    diags
    |> Array.map (fun d -> $"{d.Severity} FS{d.ErrorNumber:D4}: {d.Message}")
    |> Array.toList

[<EntryPoint>]
let main _ =
    let root = Environment.GetEnvironmentVariable "FSREPL_ROOT"
    let socketPath = Environment.GetEnvironmentVariable "FSREPL_SOCKET"
    let transcriptPath = Environment.GetEnvironmentVariable "FSREPL_TRANSCRIPT"

    if isNull root || isNull socketPath || isNull transcriptPath then
        eprintfn "fsrepl: FSREPL_ROOT, FSREPL_SOCKET and FSREPL_TRANSCRIPT must all be set"
        1
    else
        let configPath = Path.Combine(root, "fsrepl.json")

        let config =
            if File.Exists configPath then
                match JsonNode.Parse(File.ReadAllText configPath) with
                | :? JsonObject as value -> value
                | _ -> failwith "fsrepl.json: expected a JSON object"
            else
                JsonObject()

        let references =
            match config["references"] with
            | :? JsonArray as paths -> paths |> Seq.map (fun path -> Path.GetFullPath(Path.Combine(root, path.ToString()))) |> Seq.toArray
            | null -> [||]
            | _ -> failwith "fsrepl.json: references must be an array"

        let preload =
            match config["preload"] with
            | null -> None
            | path -> Some(Path.GetFullPath(Path.Combine(root, path.ToString())))

        let appendTranscript (node: JsonObject) =
            // UTF8Encoding(false): no BOM — the transcript is NDJSON read by
            // plain utf-8 consumers.
            use w = new StreamWriter(transcriptPath, true, UTF8Encoding(false))
            w.WriteLine(node.ToJsonString())

        // Evaluated code's printfn/Console.WriteLine goes to the process
        // console, not the session's writers — capture both and merge.
        let consoleBuf = new StringWriter()
        let operationalError = Console.Error
        let consoleErrBuf = new StringWriter()
        Console.SetOut(consoleBuf)

        // --- the one session ------------------------------------------------
        //
        // Per-evaluation stdout/stderr capture: cleared before each
        // evaluation, read after (the serial accept loop makes the swap
        // race-free).
        let outBuf = new StringWriter()
        let errBuf = new StringWriter()

        let session =
            Shell.FsiEvaluationSession.Create(
                Shell.FsiEvaluationSession.GetDefaultConfiguration(),
                Array.append [| "fsi"; "--noninteractive" |] (references |> Array.map (fun path -> $"-r:{path}")),
                new StringReader(""),
                outBuf,
                errBuf
            )

        // Load the preload as ONE interaction so its bindings persist in the
        // session. EvalScript runs a file in an isolated scope. Hosted
        // interactions reject #r, so references are supplied through argv.
        let bootCode =
            match preload with
            | Some path ->
                File.ReadAllLines(path)
                |> Array.filter (fun line -> not (line.TrimStart().StartsWith("#r")))
                |> String.concat "\n"
            | None -> ""

        let bootResult, bootDiags = session.EvalInteractionNonThrowing(bootCode)

        let bootErrors = bootDiags |> Array.filter severityIsError

        for d in bootErrors do
            eprintfn "fsrepl: boot diagnostic: FS%d %s" d.ErrorNumber d.Message

        match bootResult with
        | Choice2Of2 ex -> eprintfn "fsrepl: boot exception: %s" ex.Message
        | Choice1Of2 _ -> ()

        if
            bootErrors.Length > 0
            || (match bootResult with
                | Choice2Of2 _ -> true
                | _ -> false)
        then
            failwith "fsrepl: preloaded session failed; refusing to listen"

        // Truncate REPL printing so a stray large value cannot flood the
        // transcript.
        session.EvalInteractionNonThrowing("fsi.PrintWidth <- 160") |> ignore
        session.EvalInteractionNonThrowing("fsi.PrintLength <- 200") |> ignore

        let clearBuffers () =
            outBuf.GetStringBuilder().Clear() |> ignore
            errBuf.GetStringBuilder().Clear() |> ignore
            consoleBuf.GetStringBuilder().Clear() |> ignore
            consoleErrBuf.GetStringBuilder().Clear() |> ignore

        let capturedStdout () =
            let echoed = outBuf.ToString()
            let printed = consoleBuf.ToString()

            if echoed = "" then printed else echoed + printed

        let capturedStderr () =
            let fsi = errBuf.ToString()
            let printed = consoleErrBuf.ToString()

            if fsi = "" then printed else fsi + printed

        /// One evaluation: expression-first with an interaction fallback,
        /// structured result, one transcript line.
        let evaluate (id: string) (code: string) : JsonObject =
            clearBuffers ()

            let respond
                (ok: bool)
                (value: string option)
                (stdout: string)
                (stderr: string)
                (diagnostics: string list)
                (exn: string option)
                =
                let node = JsonObject()
                node["id"] <- id
                node["ok"] <- ok

                node["value"] <-
                    (match value with
                     | Some v -> JsonValue.Create(v) :> JsonNode
                     | None -> null)

                node["stdout"] <- stdout
                node["stderr"] <- stderr

                let diagArray = JsonArray()

                for (d: string) in diagnostics do
                    diagArray.Add(JsonValue.Create(d)) |> ignore

                node["diagnostics"] <- diagArray

                node["exception"] <-
                    (match exn with
                     | Some e -> JsonValue.Create(e) :> JsonNode
                     | None -> null)

                node

            let evaluateCore () =
                // Keep FSI directives intact. In particular, #load establishes
                // the file's own module and resolves nested #load/#r relative to
                // that file. Flattening it into an interaction changes both.
                let exprResult, exprDiags = session.EvalExpressionNonThrowing(code)

                if exprDiags |> Array.exists severityIsError then
                    // Did not compile as an expression — nothing ran; run as
                    // an interaction. The failed probe leaves its own noise in
                    // the capture buffers ("Stopped due to error"); clear it
                    // so only the interaction's real output is reported.
                    clearBuffers ()

                    let intResult, intDiags = session.EvalInteractionNonThrowing(code)

                    match intResult with
                    | Choice1Of2 _ ->
                        respond
                            (not (intDiags |> Array.exists severityIsError))
                            None
                            (capturedStdout ())
                            (capturedStderr ())
                            (formatDiagnostics intDiags)
                            None
                    | Choice2Of2 ex ->
                        respond
                            false
                            None
                            (capturedStdout ())
                            (capturedStderr ())
                            (formatDiagnostics intDiags)
                            (Some ex.Message)
                else
                    match exprResult with
                    | Choice1Of2(Some v) ->
                        // A unit/null reflection value is not worth echoing.
                        let valueText =
                            match box v.ReflectionValue with
                            | null -> None
                            | value -> Some(sprintf "%A" value)

                        respond
                            true
                            valueText
                            (capturedStdout ())
                            (capturedStderr ())
                            (formatDiagnostics exprDiags)
                            None
                    | Choice1Of2 None ->
                        respond true None (capturedStdout ()) (capturedStderr ()) (formatDiagnostics exprDiags) None
                    | Choice2Of2 ex ->
                        respond
                            false
                            None
                            (capturedStdout ())
                            (capturedStderr ())
                            (formatDiagnostics exprDiags)
                            (Some ex.Message)

            let outcome =
                Console.SetError(consoleErrBuf)

                try
                    evaluateCore ()
                finally
                    Console.SetError(operationalError)

            let entry = JsonObject()
            // The response fields are owned by `outcome`; the transcript entry
            // needs its own copies (a JsonNode cannot have two parents).
            let clone (n: JsonNode) =
                if isNull n then null else n.DeepClone()

            entry["ts"] <- DateTime.UtcNow.ToString("o")
            entry["id"] <- id
            entry["code"] <- code
            entry["ok"] <- clone (outcome["ok"])
            entry["value"] <- clone (outcome["value"])
            entry["stdout"] <- outcome["stdout"].ToString()
            entry["stderr"] <- outcome["stderr"].ToString()
            entry["diagnostics"] <- clone (outcome["diagnostics"])
            entry["exception"] <- clone (outcome["exception"])
            appendTranscript entry
            outcome

        // --- the socket service ---------------------------------------------

        let server =
            new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified)

        server.Bind(UnixDomainSocketEndPoint(socketPath))
        server.Listen(8)

        eprintfn "fsrepl: listening on %s (boot errors: %d)" socketPath bootErrors.Length

        let bootEvent = JsonObject()
        bootEvent["ts"] <- DateTime.UtcNow.ToString("o")
        bootEvent["event"] <- $"listening on {socketPath}; boot errors {bootErrors}"
        appendTranscript bootEvent

        let readRequest (client: Socket) =
            let buffer = Array.zeroCreate<byte> 65536
            use acc = new MemoryStream()
            let mutable finished = false

            while not finished do
                let n = client.Receive(buffer)

                if n = 0 then
                    finished <- true
                else
                    let newline = Array.IndexOf(buffer, byte '\n', 0, n)
                    let count = if newline < 0 then n else newline
                    acc.Write(buffer, 0, count)

                    if acc.Length > 16L * 1024L * 1024L then
                        invalidOp "request exceeds 16 MiB"

                    if newline >= 0 then
                        finished <- true

            UTF8Encoding(false, true).GetString(acc.ToArray())

        let sendAll (client: Socket) (bytes: byte[]) =
            let mutable sent = 0

            while sent < bytes.Length do
                let n = client.Send(bytes, sent, bytes.Length - sent, SocketFlags.None)

                if n = 0 then
                    invalidOp "socket closed while sending response"

                sent <- sent + n

        while true do
            let client = server.Accept()

            try
                let response =
                    try
                        let request = readRequest client
                        let node = JsonNode.Parse(request)

                        let id =
                            match node["id"] with
                            | null -> "?"
                            | v -> v.ToString()

                        match node["op"] with
                        | null -> evaluate id "(no op)"
                        | op when op.ToString() = "ping" ->
                            let reply = JsonObject()
                            reply["id"] <- id
                            reply["ok"] <- true
                            reply["ping"] <- "fsrepl"
                            reply
                        | op when op.ToString() = "eval" ->
                            let code =
                                match node["code"] with
                                | null -> ""
                                | c -> c.ToString()

                            evaluate id code
                        | op ->
                            let reply = JsonObject()
                            reply["id"] <- id
                            reply["ok"] <- false
                            reply["exception"] <- $"unknown operation: {op}"
                            reply
                    with ex ->
                        let reply = JsonObject()
                        reply["id"] <- "?"
                        reply["ok"] <- false
                        reply["exception"] <- $"bad request: {ex.Message}"
                        reply

                let bytes = Encoding.UTF8.GetBytes(response.ToJsonString() + "\n")
                sendAll client bytes
            with ex ->
                eprintfn "fsrepl: connection error: %s" ex.Message

            client.Close()

        // Unreachable: the accept loop runs forever.
        0
