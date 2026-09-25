{
  description = "Grossy: weekly grocery shopping automation for AB.gr";

  # Pinned to the NixOS unstable channel per owner preference (task 01 tooling).
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forEachSystem =
        f:
        nixpkgs.lib.genAttrs supportedSystems (system: f system);
    in
    {
      devShells =
        forEachSystem
        (system:
          let
            pkgs = nixpkgs.legacyPackages.${system};
          in
          {
            # Pinned development environment for the grossy Python package.
            # `just setup` creates the uv-managed virtualenv from uv.lock;
            # all quality targets in the Justfile run fully offline.
            default =
              pkgs.mkShell {
                packages = with pkgs; [
                  python312
                  uv
                  just
                  git
                ];
              };
          });
    };
}
