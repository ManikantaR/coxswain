# Spike 1 — billing proof: PASS (2026-07-14)

`coxd/.venv/bin/python coxd/spike_billing.py` with `ANTHROPIC_API_KEY` unset:

- Ran a real haiku-4-5 session on the **subscription** (claude login OAuth), no API key.
- `ResultMessage.is_error=False`, session_id returned, total_cost_usd=0.025.
- usage is STRUCTURED: input_tokens=10, cache_creation_input_tokens=12067,
  cache_read_input_tokens=0, output_tokens=49 — separate fields, no double-count possible.

Confirms DESIGN-V35 D19 (SDK foundation) + the billing invariant. coxd must keep
ANTHROPIC_API_KEY out of its env (spike asserts this).
