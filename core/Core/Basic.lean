/-!
# Agent Meter — the decision core (proved)

This file is the trusted part of the meter. It never touches the operating
system: the Python shell reads `/proc` and `nvidia-smi`, hands one `Sample`
per tick to the compiled binary, and draws whatever comes back. Everything
that *decides* — which level we are at, how much room is left, whether the
advice is sound — lives here, and the theorems below are checked by the Lean
compiler every time this builds. A build that passes is the proof.

Reading guide (the only three things you need to recognise):
* `def`      — code. It runs.
* `theorem`  — a claim: hypotheses, then `:`, then the conclusion.
* `by ...`   — the proof. You may skip it; the compiler accepting it is the
               part you trust, not your own following of the steps.
-/

namespace AgentMeter

/-- One sample of the machine as the shell reports it.
All sizes are MiB. `pressure` is the kernel's PSI "some avg10" figure in
tenths of a percent, so `100` means 10.0 % of the last ten seconds were spent
stalled on memory — the signal that rises while thrashing begins, before
`available` reaches zero. -/
structure Sample where
  total     : Nat   -- MiB of RAM in the machine
  available : Nat   -- MiB the kernel says can still be handed out
  pressure  : Nat   -- PSI some avg10, tenths of a percent
  deriving Repr

/-- The thresholds. The shell derives these from the machine's specs (RAM,
swap size, VRAM) and passes them in; the defaults are the Legion's: with only
2 GB of swap, red fires at 15 % free rather than 5 %. Memory thresholds are
"percent of total still available"; pressure thresholds are PSI tenths. -/
structure Policy where
  amberFreePct  : Nat := 30   -- amber when less than 30 % of RAM is available
  redFreePct    : Nat := 15   -- red when less than 15 % is available
  amberPressure : Nat := 10   -- or when stall pressure reaches 1.0 %
  redPressure   : Nat := 100  -- or 10.0 %
  deriving Repr

/-- The traffic light the user sees. -/
inductive Level
  | green | amber | red
  deriving Repr, DecidableEq

/-- Severity as a number, so "never calmer" can be stated as `≤`. -/
def Level.rank : Level → Nat
  | .green => 0
  | .amber => 1
  | .red   => 2

def Level.name : Level → String
  | .green => "GREEN"
  | .amber => "AMBER"
  | .red   => "RED"

/-- The classification. Written with `available * 100 < total * pct` instead
of a division so that no rounding can hide a crossing. Red is checked first:
a sample that satisfies both red and amber conditions is red. -/
def level (p : Policy) (s : Sample) : Level :=
  if s.available * 100 < s.total * p.redFreePct ∨ p.redPressure ≤ s.pressure then .red
  else if s.available * 100 < s.total * p.amberFreePct ∨ p.amberPressure ≤ s.pressure then .amber
  else .green

/-! ## Theorems about the light -/

/-- Red arrives in the very sample where headroom drops under the red line —
never one tick late. -/
theorem red_on_time (p : Policy) (s : Sample)
    (h : s.available * 100 < s.total * p.redFreePct) : level p s = .red := by
  simp [level, h]

/-- The same for pressure: stall pressure at the red threshold is red at once. -/
theorem red_on_pressure (p : Policy) (s : Sample)
    (h : p.redPressure ≤ s.pressure) : level p s = .red := by
  simp [level, h]

/-- Green is an actual promise: at least the amber share of RAM is available
AND stall pressure is below the amber threshold. No green with a hidden problem. -/
theorem green_means_room (p : Policy) (s : Sample) (h : level p s = .green) :
    s.total * p.amberFreePct ≤ s.available * 100 ∧ s.pressure < p.amberPressure := by
  unfold level at h
  by_cases h1 : s.available * 100 < s.total * p.redFreePct ∨ p.redPressure ≤ s.pressure
  · simp [h1] at h
  · by_cases h2 : s.available * 100 < s.total * p.amberFreePct ∨ p.amberPressure ≤ s.pressure
    · simp [h1, h2] at h
    · exact ⟨by omega, by omega⟩

/-- Less headroom never yields a calmer light. If sample `t` has no more RAM
available and no less pressure than `s` (same machine), its level is at least
as severe. A swarm ramping up can therefore never flip the display from red
back toward green. -/
theorem level_monotone (p : Policy) (s t : Sample) (ht : t.total = s.total)
    (ha : t.available ≤ s.available) (hp : s.pressure ≤ t.pressure) :
    (level p s).rank ≤ (level p t).rank := by
  unfold level
  rw [ht]
  by_cases hRs : s.available * 100 < s.total * p.redFreePct ∨ p.redPressure ≤ s.pressure
  · -- s is red, so t is red too: both of t's numbers are at least as bad.
    have hRt : t.available * 100 < s.total * p.redFreePct ∨ p.redPressure ≤ t.pressure := by
      cases hRs with
      | inl h => exact Or.inl (by omega)
      | inr h => exact Or.inr (by omega)
    simp [hRs, hRt]
  · by_cases hAs : s.available * 100 < s.total * p.amberFreePct ∨ p.amberPressure ≤ s.pressure
    · -- s is amber; t is amber or red.
      have hAt : t.available * 100 < s.total * p.amberFreePct ∨ p.amberPressure ≤ t.pressure := by
        cases hAs with
        | inl h => exact Or.inl (by omega)
        | inr h => exact Or.inr (by omega)
      by_cases hRt : t.available * 100 < s.total * p.redFreePct ∨ p.redPressure ≤ t.pressure
      · simp [hRs, hAs, hRt, Level.rank]
      · simp [hRs, hAs, hRt, hAt, Level.rank]
    · -- s is green: rank 0 is below everything.
      simp [hRs, hAs, Level.rank]

/-! ## Headroom, in the user's units -/

/-- The red line in MiB: the smallest `available` that is not red by memory.
Rounded UP, so the line itself is safe. -/
def redLine (p : Policy) (s : Sample) : Nat :=
  (s.total * p.redFreePct + 99) / 100

/-- The amber line in MiB, same rounding. The widget draws both lines on the
headroom bar so the user sees where the zones begin. -/
def amberLine (p : Policy) (s : Sample) : Nat :=
  (s.total * p.amberFreePct + 99) / 100

/-- The zones are ordered: with red below amber in the policy, the red line
sits at or below the amber line, so the bar can never show them crossed. -/
theorem redLine_le_amberLine (p : Policy) (s : Sample) (h : p.redFreePct ≤ p.amberFreePct) :
    redLine p s ≤ amberLine p s := by
  unfold redLine amberLine
  have : s.total * p.redFreePct ≤ s.total * p.amberFreePct :=
    Nat.mul_le_mul_left (k := s.total) h
  omega

/-- "Room for N more jobs like these": how many jobs of `jobSize` MiB fit
between what is available now and the red line. This is the number the
widget shows instead of gigabytes. -/
def roomFor (p : Policy) (s : Sample) (jobSize : Nat) : Nat :=
  (s.available - redLine p s) / jobSize

/-- Soundness of that advice. If we are not already under the red line and
the user starts `n ≤ roomFor` jobs of `jobSize` MiB, memory alone cannot make
the light red. (Pressure is a separate trigger by design: it reacts to
thrashing the shell cannot predict.) -/
theorem room_sound (p : Policy) (s : Sample) (jobSize n : Nat)
    (h0 : redLine p s ≤ s.available) (hn : n ≤ roomFor p s jobSize) :
    s.total * p.redFreePct ≤ (s.available - n * jobSize) * 100 := by
  unfold roomFor at hn
  -- n ≤ q  ⇒  n·k ≤ q·k ≤ (available − redLine)
  have h1 : n * jobSize ≤ (s.available - redLine p s) / jobSize * jobSize :=
    Nat.mul_le_mul_right (k := jobSize) hn
  have h2 : (s.available - redLine p s) / jobSize * jobSize ≤ s.available - redLine p s :=
    Nat.div_mul_le_self _ _
  unfold redLine at *
  omega

end AgentMeter
