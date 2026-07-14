# Rust Ripper

The next-generation asset extractor for **Rust** by Facepunch Studios — built to answer one question well: *"I want the hot air balloon, with its textures, materials, and animations — as one file."*

```
ripper find "hot air balloon"     search the whole game in player vocabulary (instant)
ripper export hotairballoon       fully textured GLB, dependencies auto-resolved
ripper serve                      resident daemon: load once, export in seconds, feed Blender
```

## Best of all worlds

This project deliberately combines three lineages:

| From | We take |
|---|---|
| **[AssetRipper](https://github.com/AssetRipper/AssetRipper)** (engine, via submodule + NuGet) | version-proof Unity parsing — every class generated from Unity's own type layouts (3.5 → Unity 6+), prefab hierarchy reconstruction, GLB export, managed texture decoding |
| **[Rust Asset Studio](https://github.com/CalvFletch/Rust-Asset-Studio)** (our sibling project) | the Rust layer: Steam install auto-detection, GameManifest + ItemDefinition knowledge, the player-vocabulary object catalog |
| Architecture lessons from prior art | build-ID-keyed persistent caching, lazy loading, UI-before-data, resident-session exports |

**Rust Asset Studio remains maintained** as the familiar AssetStudio-style browser for everyday use. Rust Ripper is the ground-up rebuild aimed at the player-vocabulary workflow.

## What works today

- **Catalog**: `ripper catalog` parses GameManifest (16,800+ prefabs) + all ItemDefinitions (1,243 items with display names) into a build-ID-keyed cache
- **Search**: `ripper find <query> [--kind item|prefab] [--category <n>] [--path <s>]` — instant, player vocabulary
- **Export**: `ripper export <query>` — GLB with **automatic texture dependency closure**: material references are resolved through per-CAB dependency tables and exactly the needed texture bundles are loaded
- **Materials introspection**: `ripper mat <query>` — per-material shader ("method"), every texture slot, floats, colors — derived entirely from game data
- **Daemon**: `ripper serve` — bundles stay resident; repeat exports take seconds; HTTP API (`/find`, `/export`, `/mat`, `/status`) ready for the UI and Blender integration

## Building

```
git clone --recursive https://github.com/CalvFletch/Rust-Ripper.git
dotnet build RustRipper.sln -c Release
```

Requires the .NET 9 SDK. The `--recursive` matters: the AssetRipper engine is a git submodule pinned to a known-good tag, and `dev_data/server` references the community-maintained decompiled Rust server source ([Zaddish/rust-changes](https://github.com/Zaddish/rust-changes)) used as schema documentation for game structures (LOD systems, manifests, item definitions).

## Roadmap

1. Decision-aware GLB builder: empties pruning, LOD selection from `RendererLOD`/`LODGroup` data, shadow-proxy exclusion via `ShadowCastingMode` — all from actual component data, never name matching
2. Full PBR materials: normal reconstruction, specular/metallic workflow conversion per shader method, occlusion/emission, color factors
3. Animations (Unity AnimationClip → glTF channels)
4. Local web UI + Blender addon ("send to Blender" via the daemon)

## License

[GPL-3.0-or-later](LICENSE.md) — inherited from the AssetRipper engine this project builds on. Not affiliated with Facepunch Studios; use only with game files you legitimately own.
