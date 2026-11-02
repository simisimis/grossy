# Grossy

Manually invoked, single-user AB.gr grocery-history learning and cart preparation.
Python owns validated local state and planning; the project Pi skill coordinates
supervised rendered-browser actions. No CLI command launches a browser.

- [Setup, commands, corrections and recovery](docs/operations.md)
- [Coordinator-owned, bounded smoke verification](docs/smoke-test.md)
- Skill contract: [`skills/grossy/SKILL.md`](skills/grossy/SKILL.md)
- Coding-agent instructions: [`AGENTS.md`](AGENTS.md)

## Skill layout

The canonical Agent Skills directory is `skills/grossy/`, with `SKILL.md` and its
supporting scripts together. Pi discovers it through `.pi/settings.json`; resource
paths there resolve relative to `.pi/`. Start Pi from the repository root and
invoke `/skill:grossy`. Pi loads project settings only when project trust is
granted. Other compatible agents can load the same skill directory using their
own discovery configuration; `/skill:grossy` is Pi syntax.
The workflow still requires this repository's Nix shell, Grossy CLI and approved
headed-browser setup; moving the skill does not make it a standalone application.

## Nix development shell

All tooling runs through the Nix development shell. The flake pins nixpkgs;
`flake.lock` is the authoritative dependency lock. No virtualenv, pip, or uv
is used. Nix closure provisioning (`nix develop`) is the sole package source;
once the pinned closure is in the local store, `--offline` avoids Nix downloads.
It does not prevent arbitrary subprocesses from accessing the network.

```sh
# Enter the development shell (interactive; provisions closure if needed)
nix develop

# Run a single command (offline after provisioning):
nix develop --offline --no-write-lock-file -c grossy --help
nix develop --offline --no-write-lock-file -c just check
```

The `grossy` executable is store-built; `python3`, `pytest`, `mypy`, `ruff`,
and `just` are Nix-provided. Source tests use `PYTHONPATH=$PWD/src` to load
the current working tree while third-party dependencies resolve from the store.
All Just quality/operational recipes depend on `setup`, verifying tools against
this project's declared shell closure. Entry scrubs inherited Python/venv
settings and disables user-site imports; re-enter rather than modifying those
settings afterwards. Direct CLI calls do not rerun this guard and a newly set
`PYTHONPATH` can shadow the installed package. The guard permits only the
explicit repository `src` path for source-test/helper subprocesses.

Live compatibility and final release remain pending user verification. Grossy
never progresses checkout, chooses fulfilment, pays or submits an order. The
coordinator may open AB.gr `/checkout` solely for an explicitly approved cart-list
read, excluding private sections and checkout-stage controls. Do not run
unattended or treat cart preparation as purchase evidence.
