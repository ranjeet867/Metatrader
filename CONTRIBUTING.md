# Contributing to mt5_quant_trader_v2

Thank you for considering a contribution. This document is short and
opinionated; please read it before opening a PR.

## The 13 invariants are non-negotiable

Read them in [`README.md`](README.md). Every PR must keep them all
enforced. PRs that loosen an invariant ("only in this case", "tolerance
widened", etc.) will be rejected on principle.

## Local setup

```bash
make setup
make test
make lint
```

## What we accept

- Bug fixes (please include a regression test)
- New strategies (with `make screen STRATEGY=<name>` output)
- Dashboard improvements (with screenshots)
- Documentation
- Test coverage

## What we don't accept

- Untested code — full stop
- Strategy that wins on in-sample but loses on test (the grid sweep filters these out)
- "Skip the reconciliation gate just this once" — see invariant #1
- New strategies that don't have a corresponding `tests/test_<strategy>.py`

## Style

- Ruff for linting (`make lint`)
- Type hints everywhere
- Docstrings explain WHY, not WHAT
- Pure functions where possible — they're trivially testable

## Tests

- Use synthetic fixtures (see `tests/fixtures/synthetic.py`) — we
  don't ship broker data
- Backtest tests must verify reconciliation
- Strategy tests must include an analytic edge case (i.e. a hand-built
  series where the expected signal is provable)

## Commit messages

- One line summary, present tense imperative ("add X", not "added X")
- Reference the invariant if relevant ("invariant-3: ...")
- Include test count if you added tests ("+5 tests, 544 total")

## Reporting issues

For bugs: include the failing test or the smallest reproducer. For
feature requests: explain the user story first, the implementation
second.

## License

By contributing you agree that your contributions are licensed under
the MIT License (see [`LICENSE`](LICENSE)).
