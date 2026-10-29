import Core
/-!
The command-line face of the core.

Two modes:
* no arguments → a request/answer loop on stdin/stdout, one line each. The
  Python shell keeps this process open and sends one sample per tick:
      <totalMiB> <availableMiB> <pressureTenths> [jobSizeMiB] [amberPct redPct amberPSI redPSI]
  and reads back
      level=GREEN rank=0 redLine=9560 amberLine=19119 room=35 jobSize=1024
* arguments → the same request once, for the command line.

Everything printed here is computed by the proved definitions in Core; this
file only parses and formats.
-/
open AgentMeter

/-- Format one answer. `p` and `s` are the proved core's inputs. -/
def fmt (p : Policy) (s : Sample) (j : Nat) : String :=
  let lv := level p s
  s!"level={lv.name} rank={lv.rank} redLine={redLine p s} amberLine={amberLine p s} room={roomFor p s j} jobSize={j}"

/-- Parse one request line into a Policy, a Sample and a job size. -/
def answer (line : String) : String :=
  let ws := ((line.replace "\n" " ").splitOn " ").filter (· ≠ "")
  match ws.map String.toNat? with
  | [some t, some a, some pr] => fmt {} ⟨t, a, pr⟩ 1024
  | [some t, some a, some pr, some j] => fmt {} ⟨t, a, pr⟩ (max j 1)
  | [some t, some a, some pr, some j, some ap, some rp, some aps, some rps] =>
      fmt { amberFreePct := ap, redFreePct := rp, amberPressure := aps, redPressure := rps }
          ⟨t, a, pr⟩ (max j 1)
  | _ => "error=usage total available pressure [jobSize] [amberPct redPct amberPSI redPSI]"

partial def loop (stdin : IO.FS.Stream) (stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return          -- EOF: the shell closed us
  stdout.putStrLn (answer line)
  stdout.flush
  loop stdin stdout

def main (args : List String) : IO UInt32 := do
  match args with
  | [] => loop (← IO.getStdin) (← IO.getStdout); return 0
  | _  => IO.println (answer (" ".intercalate args)); return 0
