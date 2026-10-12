# Changelog

## [1.0.0] - 2026-09-07

First stable release.

### Features
- Generic upgrade path calculation for any operator in an OLM catalog, driven by
  `skipRange` and `replaces` metadata. No operator-specific logic.
- Top-down channel traversal by max version, selecting the highest reachable
  version at each hop.
- Optional `-c/--target-channel` to restrict the path to a single channel,
  enabling same-channel upgrades (e.g. `6.2.9 -> 6.2.12`).
- Optional `-t/--target-version` to stop at a specific version instead of the latest.
- networkx graph rendered as inline SVG in a self-contained HTML report.
- Graph edges from three sources: START via `skipRange`, the `replaces` chain
  within a channel, and each upgrade-path version via `skipRange`.
- HTML info table showing operator, current version, channel(s) and target version.
- Handles operators with no `skipRange` by walking the `replaces` chain.

### Validated against
- loki-operator (multi-channel and same-channel)
- openshift-gitops-operator
- compliance-operator
- devspaces
- jws-operator

## [2.0.0] - 2026-09-07

Adds OCP cluster upgrade planning across one catalog per OCP release.

### Added
- `ocp_upgrade_planner.py` — CLI taking a cluster plus an operator list as JSON.
- `ocp_planner.py` — planning engine.
- `ocp_report.py` — one HTML report per operator, one row group per phase,
  plus a cluster summary page.
- EUS (current+2) and single-release (current+1) upgrade paths, derived from
  `cluster.channel`. Catalogs outside the path are never read.
- Generic detection of version-pinned operators (versions tracking the OCP
  release, e.g. odf-operator 4.18.x on OCP 4.18).
- Non-monotonic catalog entries are excluded from planning and reported as notes
  for manual verification.
- JSON on stdout and exit codes: 0 ok, 2 manual review, 3 blocked, 1 input error.
- Catalogs are named `data-v<major>.<minor>.json`, one per OCP release. The
  underscore variant `data-v<major>_<minor>.json` is also accepted, since some
  transfer paths rewrite dots in filenames. Other files in the same directory,
  such as a combined `data.json`, are ignored.
- Catalog auto-discovery. `--catalog-dir` is optional. Catalogs are commonly
  kept outside the project that consumes them, so the search covers the
  `OCP_CATALOG_DIR` environment variable, several conventional directory names
  (`data`, `catalogs`, `catalog`, `data-catalogs`, `ocp-catalogs`) beside the
  input file and the current directory and walking up their parents, and
  finally a bounded recursive scan. Missing releases are reported alongside
  the ones that were found, and a failed search lists every location tried.

### Input handling
- Cluster versions accept `x.y.z`; the patch level is ignored when selecting
  catalogs, so `4.18.14 -> 4.20.32` resolves to the 4.18/4.19/4.20 catalogs.
- Operator names are resolved against the catalog. Subscription names often
  differ from the OLM package name (`openshift-mtv` for `mtv-operator`,
  `cert-manager-operator` for `openshift-cert-manager-operator`); the vendor
  prefix and role suffix are normalized away. Ambiguous matches are reported
  rather than guessed, and every resolution is recorded in the notes.
- Build and vendor version suffixes are preserved (`4.18.27-rhodf`,
  `4.18.0-202608142236`). They are part of an operator's identity: several
  operators publish many `4.18.0-<timestamp>` builds that would otherwise
  collapse into a single version. Ordering falls back to the numeric core,
  then to the suffix, so later dated builds rank higher. skipRange bounds are
  still evaluated against the numeric core.
- Catalog entries are matched with or without the `v` prefix, since both
  `package.v4.18.3` and `package.4.18.3` appear in real catalogs.
- The channel already in use is preferred when a release offers several, so an
  operator on `stable` is never quietly moved onto `candidate`.
- A requested channel that no longer exists no longer fails the operator. It
  falls back to the channel actually holding the installed version, then to the
  newest channel named `stable` or `latest`, then to the newest channel ending
  in a version number, then to the channel carrying the highest version. Every
  substitution is recorded in the notes.
- A leading `v` is optional on cluster and operator versions on input, matching
  the existing tolerance for catalog entry names.

### Design
- Two planning models, chosen by whether the operator's version stream is
  pinned to the OCP release.
- **Release-pinned operators follow the cluster.** Each catalog carries the
  previous release's channel as well as its own, so an operator at
  `stable-<N>` stays valid when the cluster moves to N+1. The operator is
  therefore upgraded *after* each hop: move the cluster, switch to that
  release's channel, take its latest version. One operator upgrade per OCP
  upgrade. A pre-upgrade phase appears only when the installed version
  predates the release window and cannot survive the first hop.
- **Floating operators lead the cluster.** Constraint is **pairwise per hop**,
  not a global intersection across all catalogs. For a hop from OCP N to N+1 the operator must sit at a
  (channel, version) present in both catalogs; it may be moved again while the
  cluster sits at an intermediate release. Version-pinned operators carry only
  `stable-<N-1>` and `stable-<N>` per catalog, so no tuple exists in all three
  catalogs of an EUS jump — a global model would wrongly report the whole ODF
  family as blocked.
- Hops are counted per upgrade. Switching channel at the same version is free.
- Objective: fewest hops before the cluster can move, then the highest
  channel/version among equal-hop options.
- Presence is modelled as explicit version sets, not min/max floors, because
  channels are not always contiguous.

### Unchanged
- `operator_interactive.py` and the single-catalog v1.0.0 behaviour.

### Added (packaging)
- `LICENSE` - Apache License 2.0.

### Removed
- The algorithm document predating the planner, generated HTML reports checked
  in as samples, and inputs captured during development. The example input is a
  single template.

## [Unreleased]

Catalogs are pulled at run time instead of being supplied by hand, and every
operator is planned backwards from the target catalog, which also gives the
catalogs a disconnected cluster must mirror.

### Added
- The catalog mirror check written by ocp_preupgrade_health_check (task 89b),
  `outputs/catalog_mirror_check.json`, is accepted as input. Operators are
  grouped by catalog index image; packages with `main: false` are dependencies
  and are not planned.
- `catalog_fetch.py` pulls the catalogs. For each catalog image and each OCP
  release on the path the tag is moved to that release and only the installed
  packages are extracted (`oc image extract --path /configs/<pkg>/`). The
  `olm.channel` objects are written as `data-v<major>.<minor>.json`, the format
  previously supplied by hand. Credentials come from `--authfile` or the
  cluster pull secret. Pulls run in parallel (`--jobs`) and are reused on later
  runs unless `--refresh` is given.
- `mirror_plan.py` plans each operator against the target catalog first: it is
  covered when that catalog still ships the installed version or has a
  skipRange, replaces or skips edge from it, and the shortest path comes from
  there. Only when the target does not cover it are earlier catalogs added,
  working backwards (4.19 on a 4.18 to 4.20 EUS path, then 4.18); that is
  reported CRITICAL as `intermediate_catalog_required`.
- Verdicts `no_action_required` (still shipped by the target catalog),
  `operator_upgrade_required` (gone from it but covered, or `max_ocp_version`
  below the target), `intermediate_catalog_required`, `blocked` (package gone
  from the target catalog) and `manual_review`.
- An oc-mirror v2 `ImageSetConfiguration` (`imageset-config.yaml`) listing,
  per catalog version, the packages, channels and version ranges to mirror.
  Dependencies are included at their channel head.
- The summary page has one column per catalog and lists the catalogs to
  mirror; the plan JSON carries `catalogs_to_mirror` and `imageset_config`.
  Exit code 4 when an intermediate catalog is needed; 3 now means a package is
  gone from the target catalog.

### Removed
- The flat operator-list input (`name`/`channel`/`version` per operator). The
  catalog mirror check is the only input format.
- Catalog auto-discovery (`OCP_CATALOG_DIR`, conventional directory names, the
  parent walk and recursive scan). Catalogs are pulled, or read from an explicit
  `--catalog-dir`.
- `plan_cluster()` in `ocp_planner.py`, the flat-input entry point.
- Forward, hop-by-hop planning: the pairwise rule that an operator must sit at
  a version present in both catalogs of each hop, the release-pinned and
  floating models, and non-monotonic catalog detection. The pairwise rule
  reported operators such as OADP 1.4 as blocked although the target catalog's
  skipRange covers them.

### Changed
- `skips` is now an upgrade edge alongside `replaces` and `skipRange`.
- A row needing no action reads "no action" in every column but the target,
  which shows the newest version as "available, optional" (or "no action"
  when already the newest), instead of each column naming the newest version
  of its own catalog.
- Vendor support matrices (`constraints/vendor-support.json`, starting with
  Portworx Enterprise 3.5.3 to 3.7.1): the installed release, from the
  input's `component_versions` or `--component-version`, must be certified
  on every OpenShift release of the path, up to the z-stream for the current
  and target releases, with its operator minimum met. Otherwise the operator
  blocks the upgrade and the lowest covering release at or above the
  installed one is recommended. The summary shows the vendor's whole support
  matrix for the path under the operator's row, and the operator page repeats
  it, with the installed and recommended releases marked; each matrix column
  shows the vendor verdict.
- `cluster.upgrade_path` (e.g. 4.18.14, 4.18.30, 4.19.33, 4.20.34): each
  release must be certified up to the highest version the path reaches on
  it, the intermediate release included. Validated against `current`,
  `target` and `ocp_path`, and shown as the cluster path in the reports.
- An installed vendor release that is not given is narrowed down from the
  matrix by the installed operator (a release needs at least its operator
  minimum): blocked when none of the possible releases covers the path,
  otherwise reported for confirmation with the upgrade each non-covering one
  would need.
- The HTML reports follow PatternFly 6, like the ocp_preupgrade_health_check
  report: OpenShift console masthead, Red Hat fonts, PatternFly status
  colors and filled labels, cards and compact tables, light and dark mode.
  The fonts are copied once into each cluster's `html/fonts/`.
- Catalogs already pulled are reused without reading the cluster pull secret,
  so a rerun needs no cluster login; credentials are read on the first pull.
- Report graphs are deterministic: the SVG has fixed element ids and no
  timestamp, so rerunning on the same data leaves the reports unchanged.
- An EUS path must start on an even minor, since EUS releases are the even
  ones.
- Release-pinned operators (versions tracking the OCP release, e.g. nfd,
  kubevirt-hyperconverged, ODF) follow the strict EUS path: each release's own
  version from that release's catalog, so the intermediate catalog is required
  even when a target bundle's skipRange would allow the jump.
- In the summary matrix, a catalog column an operator is not upgraded from
  validates the bundle it is on by then instead of showing a dash: its
  declared `maxOpenShiftVersion` ("supports 4.18 to X"); else, when that
  catalog ships the same channel and version, "no action"; else, when it ships
  the target's planned version, the planned upgrade on that release, with the
  target column then "no action, upgraded on" it; else the upgrade to a newer
  version of its channel; else a warning, also in the operator's
  notes, as when no bundle metadata is available or the operator is not found.
  A final pass over each row keeps an upgrade shown in several columns only in
  the lowest one; the higher become "no action, upgraded on" it. The rows are
  in the plan JSON as `matrix`, the checks behind them as `columns`. An empty input
  `max_ocp_version` falls back to the installed bundle's value in the pulled
  catalog.
- When several channels carry the latest version, the subscribed channel is
  kept instead of switching to the one whose name sorts higher
  (cert-manager `stable-v1` 1.19.2 now goes to `stable-v1` 1.20.1, not
  `stable-v1.20`).
- Each bundle's `olm.maxOpenShiftVersion` is recorded when the catalogs are
  pulled (`packages-v<major>.<minor>.json`); catalogs pulled before are pulled
  again once.
- An installed bundle whose `max_ocp_version` is below the target is upgraded,
  from the current catalog and before the cluster upgrade, to a bundle present
  in every catalog on the path whose own `maxOpenShiftVersion` reaches the
  target, when the target catalog covers it.
- Outputs go to `<output-dir>/<cluster_name>/` (html, `imageset-config.yaml`
  and `plan.json`), taking `cluster_name` (or `cluster-name`) from the input,
  so 150+ clusters can share one output directory. `--output-dir` defaults to
  `output`. Pulled catalogs stay shared in `<output-dir>/catalogs/`: a pull
  adds to the packages already there, under a per-catalog lock, with atomic
  writes, so clusters can be planned in parallel.
- The example input is now `examples/catalog_mirror_check.json`, replacing
  `examples/cluster.json` and `cluster_operators_installed.json`.
- `catalogs/` keeps only `data-v4.22.json`, as an example of the catalog
  format and for `operator_interactive.py`. `data.json` (a copy of 4.20) and
  the 4.18 to 4.21 catalogs are removed, since catalogs are pulled at run time.
