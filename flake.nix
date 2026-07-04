{
  description = "james — a personal AI chief-of-staff";

  # The Python deps live in uv.lock (installed by uv), not in Nix — mirroring the
  # flox stance. This flake provides the *toolchain* (a dev shell identical to the
  # flox env) and a NixOS module that runs `james serve` as a systemd service via
  # flox, so nix-native hosts integrate james without re-packaging its deps.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # `nix develop` — the same toolchain flox provides (deps still via uv).
      # NOTE: flox is the canonical env; protoc here follows nixpkgs and may
      # drift from the flox-pinned protoc 33.x that produced the committed
      # gencode (protobuf>=6.33,<7 in pyproject) — regenerate with flox.
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python313
            pkgs.uv
            pkgs.buf
            pkgs.protobuf
            pkgs.go
            pkgs.sops
            pkgs.age
          ]
          # chromium for the `shot` backend (linux-only in nixpkgs).
          ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.chromium ];
          UV_PYTHON = "3.13";
          shellHook = ''
            # libstdc++ for grpcio (pulled in by invariant-protocol), as in flox.
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
            echo "james dev shell — run: uv sync && ./james cli --backend gpt 'hi'"
          '';
        };
      });

      # NixOS module: run `james serve` as a systemd service. Point it at a
      # checked-out repo; flox provides the app runtime, this provides the
      # system integration (user, unit, secrets). See deploy/README.md.
      nixosModules.james =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.services.james;
        in
        {
          options.services.james = {
            enable = lib.mkEnableOption "james AI chief-of-staff";
            workingDir = lib.mkOption {
              type = lib.types.path;
              description = "Checked-out james repo (contains .flox/, james, config.yaml).";
            };
            user = lib.mkOption {
              type = lib.types.str;
              default = "james";
            };
            group = lib.mkOption {
              type = lib.types.str;
              default = "james";
            };
            environmentFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
              description = "Decrypted dotenv (bot tokens / API keys), mode 0400. Use sops at deploy time.";
            };
            floxPackage = lib.mkOption {
              type = lib.types.package;
              default = pkgs.flox;
              description = "flox package providing the app runtime.";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.${cfg.user} = {
              isSystemUser = true;
              group = cfg.group;
              home = cfg.workingDir;
              # cli backends (claude/codex/grok/opencode) keep login state in
              # $HOME.
            };
            users.groups.${cfg.group} = { };

            systemd.services.james = {
              description = "james — AI chief-of-staff";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];
              path = [
                cfg.floxPackage
                pkgs.git
                pkgs.bash
              ];
              serviceConfig = {
                User = cfg.user;
                Group = cfg.group;
                WorkingDirectory = cfg.workingDir;
                EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;
                ExecStart = "${pkgs.bash}/bin/bash -lc 'flox activate -- ./james serve'";
                Restart = "on-failure";
                RestartSec = "5s";
                # Durable, private state for session memory + browser profiles
                # (point sessions.store_path / browser.profiles_dir at
                # /var/lib/james) — matches deploy/systemd/james.service.
                StateDirectory = "james";
                StateDirectoryMode = "0700";
              };
            };
          };
        };

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
