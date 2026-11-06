{
  description = "Grossy: weekly grocery shopping automation for your local store";

  # Pinned to the NixOS unstable channel per owner preference (task 01 tooling).
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forEachSystem =
        f:
        nixpkgs.lib.genAttrs supportedSystems (system: f system);

      # Build source: only what the Python build backend needs.
      # pyproject.toml declares the package layout (src/grossy) and build
      # requirements (setuptools). The package data (migrations, seed fixtures)
      # are inside src/grossy/ and are included automatically.
      buildSource =
        pkgs:
        pkgs.runCommand "grossy-build-source" { } ''
          mkdir -p $out/src
          cp ${self}/pyproject.toml $out/
          cp -r ${self}/src/grossy $out/src/grossy
        '';
    in
    {
      # Store-backed grossy application with package resources.
      # The installed executable is self-contained: it finds its modules and
      # resources (migrations, seed fixtures) from the Nix store, not from
      # the working tree.
      packages =
        forEachSystem
        (system:
          let
            pkgs = nixpkgs.legacyPackages.${system};
            py312 = pkgs.python312;
            pyPkgs = py312.pkgs;
          in
          {
            default = pyPkgs.buildPythonApplication {
              pname = "grossy";
              version = "0.5.0";
              src = buildSource pkgs;
              pyproject = true;
              nativeBuildInputs = [ pyPkgs.setuptools ];
              dependencies = [
                pyPkgs.pydantic
                pyPkgs.pyyaml
                pyPkgs.typer
              ];
            };
          });

      devShells =
        forEachSystem
        (system:
          let
            pkgs = nixpkgs.legacyPackages.${system};
            py312 = pkgs.python312;
            grossyApp = self.packages.${system}.default;

            # Coherent Python 3.12 environment with all runtime and test
            # dependencies in a single interpreter environment.
            testPython = py312.withPackages (ps: [
              ps.pydantic
              ps.pyyaml
              ps.typer
              ps.pytest
              ps.pytest-xdist
              ps.mypy
              ps.types-pyyaml
            ]);
          in
          {
            default = pkgs.mkShell {
              packages = [
                # Store-backed grossy CLI (self-contained, finds resources from store).
                grossyApp
                # Coherent Python 3.12 environment for tests and tools.
                testPython
                # Quality tools.
                pkgs.ruff
                pkgs.just
                # Basic utilities required for shell operations and scrubbing.
                pkgs.coreutils
                pkgs.gnugrep
                pkgs.gnused
              ];

              # Operator wrapper: scrubs ambient Python environment BEFORE
              # establishing Nix store paths. This prevents .venv, user-site,
              # and inherited PYTHONPATH from leaking into the shell.
              #
              # Step 1: Remove any .venv/bin entries from PATH so a stale
              #         virtualenv cannot shadow Nix store tools.
              # Step 2: Unset hostile Python environment variables.
              # Step 3: Prepend the withPackages env's bin to PATH so its
              #         python3 (with all site-packages) is the default.
              #
              # PYTHONPATH is NOT set globally: source tests and the
              # weekly-add recipe set it explicitly via the Justfile.
              # The store-backed `grossy` executable is unaffected (it uses
              # its own installed path from the Nix store).
              shellHook = ''
                # Scrub .venv/bin from PATH using sed (available via gnused).
                # Removes entries ending in /.venv/bin from the colon-separated PATH.
                export PATH=$(echo "$PATH" | sed -e ':loop' -e 's|:[^:]*\.venv/bin|:|g' -e 's|^[^:]*\.venv/bin:||g' -e 't loop' -e 's|::|:|g' -e 's|^:||' -e 's|:$||')
                # Unset hostile Python environment variables.
                unset PYTHONHOME
                unset VIRTUAL_ENV
                unset __PYVENV_LAUNCHER__
                unset PYTHONPATH
                export PYTHONNOUSERSITE=1
                if [ -f ./.env ]; then set -a; . ./.env; set +a; fi
                # Establish Nix store Python as the default interpreter.
                export GROSSY_SHELL_PROJECT="grossy-python312-v1"
                export GROSSY_SHELL_PYTHON="${testPython}"
                export GROSSY_SHELL_GROSSY="${grossyApp}/bin/grossy"
                export GROSSY_SHELL_RUFF="${pkgs.ruff}/bin/ruff"
                export GROSSY_SHELL_JUST="${pkgs.just}/bin/just"
                export PATH="${testPython}/bin:${grossyApp}/bin:${pkgs.ruff}/bin:${pkgs.just}/bin:$PATH"
              '';
            };
          });
    };
}
