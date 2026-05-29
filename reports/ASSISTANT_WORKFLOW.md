# Simple Workflow With Assistant

Use this instead of running many commands manually.

## 1) Morning check

Message:

`morning check`

What happens:
- Assistant runs scout.
- If verdict is `SKIP`: no action.
- If verdict is `RUN`: assistant runs full agent and gives one clear action.

## 2) After you execute any trade

Message:

`i executed this`

Then share fill details (ticker, buy/sell, shares, price).

What happens:
- Assistant updates `profile.yaml`.
- Assistant logs execution in DB linked to decision IDs.

## 3) Weekly performance review

Message:

`weekly scorecard`

What happens:
- Assistant runs weekly attribution.
- Assistant summarizes:
  - executed trades,
  - pending outcomes,
  - what worked / did not work,
  - what to adjust next week.

## Optional quick commands (chat phrases)

- `check scout`
- `run agent`
- `update portfolio`
- `review performance`

---

This keeps the process low-friction: you mostly chat, assistant handles commands.
