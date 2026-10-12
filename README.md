# OLM Operator Upgrade Path Analyzer

Two tools that read OLM catalogs and work out how operators need to move.

| Tool | Question it answers |
|------|---------------------|
| `ocp_upgrade_planner.py` | I am upgrading a cluster. Can the target catalog upgrade my operators, and which catalogs must I mirror? |
| `operator_interactive.py` | How do I get one operator from version A to the latest, in one catalog? |

Both are generic. No operator names, versions or channel naming schemes are
hardcoded anywhere.

## Install

```bash
pip install -r requirements.txt
```

---

# 1. Cluster upgrade planner

Pulls the target release's catalog, and earlier ones, from the cluster's
catalog images, checks every installed operator against the target catalog
first, and works backwards to an intermediate catalog only when the target
does not cover it. Writes one HTML report per operator, a cluster summary and
the oc-mirror configuration for the catalogs a disconnected cluster has to
mirror, and prints the plan as JSON.

```bash
# catalogs pulled from the cluster's catalog images at run time
python ocp_upgrade_planner.py -i examples/catalog_mirror_check.json

# catalogs already on disk, data-v<major>.<minor>.json
python ocp_upgrade_planner.py -i examples/catalog_mirror_check.json --catalog-dir /path/to/catalogs

# the installed Portworx Enterprise release is known: check it exactly
# (otherwise it is narrowed down from the operator version)
python ocp_upgrade_planner.py -i outputs/catalog_mirror_check.json \
    --component-version portworx-enterprise=3.6.0
```

## Input: catalog mirror check

`outputs/catalog_mirror_check.json`, written by
[ocp_preupgrade_health_check](../ocp_preupgrade_health_check) (task 89b);
`examples/catalog_mirror_check.json` is a template.
Operators are grouped by the catalog index image they were installed from.

```json
{
  "cluster_name": "ocp5",
  "cluster": { "current": "4.18.28", "target": "4.20", "channel": "eus",
               "ocp_path": ["4.18", "4.19", "4.20"] },
  "operators": [
    { "pull_image": "registry.redhat.io/redhat/redhat-operator-index:v4.18",
      "packages": [
        { "name": "openshift-cert-manager-operator", "channel": "stable-v1",
          "version": "1.19.2", "max_ocp_version": "", "main": true,
          "required_by": [] },
        { "name": "devworkspace-operator", "channel": "fast",
          "version": "0.43.0", "max_ocp_version": "", "main": false,
          "required_by": ["web-terminal"] }
      ] }
  ]
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `cluster_name` | no | Names the cluster's output folder, `<output-dir>/<cluster_name>/`. `cluster-name` is accepted too. Characters other than letters, digits, `.`, `_` and `-` become `_`. Without it the outputs go straight into `<output-dir>` |
| `cluster.current` | yes | OCP release the cluster is on now; the patch level is ignored |
| `cluster.target` | yes | OCP release to reach |
| `cluster.channel` | no | `eus` or anything else (`stable`, `fast`, ...). Default `stable` |
| `cluster.ocp_path` | no | When given it must match the path derived from `current`, `target` and `channel` |
| `cluster.upgrade_path` | no | The versions the cluster steps through, e.g. `["4.18.14", "4.18.30", "4.19.33", "4.20.34"]`: ascending, starting at `current`, ending at `target`, covering every release of `ocp_path`. Used for vendor support matrices |
| `operators[].pull_image` | yes | Catalog index image, at its mirror location if the cluster redirects it. Must carry a `:v<major>.<minor>` tag |
| `packages[].name` | yes | OLM **package** name, as it appears in the catalog |
| `packages[].channel` | yes | Subscription channel currently in use |
| `packages[].version` | yes | Version currently installed |
| `packages[].main` | no | `false` marks a dependency installed by another operator. It is not planned, but is kept in the oc-mirror configuration. Default `true` |
| `packages[].max_ocp_version` | no | The installed bundle's `olm.maxOpenShiftVersion`. A value below the target is reported |
| `packages[].required_by` | no | Operators that depend on this one; shown in the oc-mirror configuration |
| `packages[].component_versions` | no | Versions of what the operator manages, for vendor support matrices, e.g. `{"portworx-enterprise": "3.6.0"}` |

### Pulling the catalogs

For each `pull_image` and each release on the path, the tag is moved to that
release (`:v4.18` becomes `:v4.19`, `:v4.20`) and only the installed packages
are pulled:

```bash
oc image extract <image>:v4.19 --filter-by-os=linux/amd64 -a <authfile> \
    --path /configs/<package>/:<dir> ...
```

The `olm.channel` objects are kept and written as
`<fetch-dir>/<image>/data-v4.19.json`, the same format as a hand-supplied
catalog. Each package's default channel is recorded beside it in
`packages-v4.19.json`, with each bundle's `olm.maxOpenShiftVersion`. A later
run reuses a pulled catalog when it already covers every package; `--refresh`
pulls again.

Registry credentials come from `--authfile`, or else from the cluster pull
secret (`oc extract secret/pull-secret -n openshift-config`), which needs a
logged-in `oc`.

### How operators are planned

Customers mirror only their own operators, and only from the target release's
catalog. And an operator bundle is only expected to be compatible one minor
either side of its own, while an EUS upgrade jumps two, even to even. Every
operator is therefore planned **backwards from the target catalog**, never
forwards hop by hop:

1. **Target catalog first.** It covers the installed version when it still
   ships it, or has an upgrade edge from it — `skipRange`, `replaces` or
   `skips`. The shortest path to the latest bundle is taken from that catalog,
   fewest upgrades first; switching channel at the same version is free.
2. **Intermediate catalog only when the target does not cover it.** Earlier
   catalogs are added working backwards: one extra catalog first, closest to
   the target first (4.19 on a 4.18 to 4.20 EUS path, then 4.18), then two.
   That catalog must be mirrored and deployed as well, and the operator
   upgraded from it before the target catalog takes over. Reported CRITICAL.

**An installed bundle whose `max_ocp_version` is below the target** cannot stay
through the jump. When the target catalog covers it, a bundle valid on every
release of the path is looked for: present in every catalog on it, with its own
`olm.maxOpenShiftVersion` (recorded when the catalogs are pulled) reaching the
target, and reachable from the installed version with the current catalog. The
operator is upgraded to it **before the cluster upgrade**, and need not move
during the jump:

```
loki-operator, installed stable-6.2 / 6.2.3, maxOpenShiftVersion 4.19

  stable-6.4 6.4.6 is in the 4.18, 4.19 and 4.20 catalogs, max 4.21
  -> operator_upgrade_required:
     from 4.18, before the upgrade: stable-6.4 6.4.6
     from 4.20, optional afterwards: stable-6.6 6.6.1
```

Without such a bundle it is upgraded from the target catalog while the cluster
is still on a release the installed bundle supports.

| Verdict | When |
|---------|------|
| `no_action_required` | The target catalog still ships the installed bundle, and `max_ocp_version` reaches the target. A newer version is noted as optional |
| `operator_upgrade_required` | Covered by the target catalog, but the installed channel/version is gone from it, or `max_ocp_version` is below the target. The upgrade comes from the target catalog |
| `intermediate_catalog_required` | **CRITICAL.** The target catalog does not cover the installed version, or the operator is release-pinned on an EUS path; the intermediate catalog is named |
| `blocked` | The package is gone from the target catalog |
| `manual_review` | The operator is not in its catalog image, or no combination of catalogs covers it |

```
redhat-oadp-operator, installed stable-1.4 / 1.4.3, OCP 4.18 -> 4.20 EUS

  4.20 catalog: stable 1.5.0 .. 1.5.8, skipRange >=1.4.0 <1.5.x
  1.4.3 is gone from it, but covered by skipRange
  -> operator_upgrade_required: stable-1.4 1.4.3 -> stable 1.5.8, from 4.20

advanced-cluster-management, installed release-2.12 / 2.12.8

  4.20 catalog has no edge from 2.12.8; the 4.19 catalog does
  -> intermediate_catalog_required:
     from 4.19: release-2.13 2.13.0
     from 4.20: release-2.15 2.15.0 -> release-2.17 2.17.1
```

**Release-pinned operators follow the strict EUS path.** An operator whose
versions track the OCP release — `odf-operator` 4.18.x on OCP 4.18, `nfd`,
`kubernetes-nmstate-operator`, `kubevirt-hyperconverged`, detected from the
version data — is upgraded with the cluster: each release's own version from
that release's catalog, in turn, even when a target bundle's skipRange would
allow the jump. On an EUS path that always needs the intermediate catalog:

```
nfd, installed stable / 4.18.0-202602261953, OCP 4.18 -> 4.20 EUS

  from 4.19: stable 4.19.0-202609200358
  from 4.20: stable 4.20.0-202609201357
  -> intermediate_catalog_required: mirror the 4.19 catalog for it too
```

### Vendor support matrices

Some vendors certify their product only on specific OpenShift z-streams, which
OLM metadata does not carry. `constraints/vendor-support.json` records those
matrices, keyed by OLM package; it holds the Portworx Enterprise matrix
(3.5.3 to 3.7.1) from the
[Portworx support matrix](https://docs.portworx.com/portworx-enterprise/support-matrix/operator-openshift-upgrade-path).
Another vendor is added the same way; nothing in the code is vendor-specific.

```json
"portworx-certified": {
  "component": "portworx-enterprise",
  "label": "Portworx Enterprise",
  "source": "https://docs.portworx.com/...",
  "releases": [
    { "version": "3.6.2", "operator_min": "26.3.0",
      "openshift": { "4.17": "4.17.55", "4.18": "4.18.54", "4.19": "4.19.45",
                     "4.20": "4.20.36", "4.21": "4.21.31", "4.22": "4.22.13" } }
  ]
}
```

The installed release - `component_versions` on the package in the input, or
`--component-version portworx-enterprise=3.6.0` - must be certified on every
OpenShift release the cluster passes through:

- with `cluster.upgrade_path`, each release certified up to at least the
  highest version the path reaches on it (4.18.30, 4.19.33, 4.20.34), the
  intermediate release included
- without it: the current release up to the cluster's current version, the
  intermediate release listed, the target up to the target version, so give
  `cluster.target` with its z-stream (`4.20.34`)
- the installed operator at least the release's minimum, including a
  per-release one (`operator_min_per_openshift`, e.g. 25.6.0 for 4.21)

Otherwise the operator is **blocked**, and the lowest release at or above the
installed one that covers the whole path is recommended, with its operator
minimum and the current catalog's version meeting it, preferring the
subscribed channel.

When the installed release is not given, it is narrowed down from the matrix:
a release needs at least its operator minimum, so the installed operator rules
out every release needing a newer one. With operator 25.5.2 only 3.5.3 is
possible (later releases need 26.1.0); with 26.1.5, 3.5.3 or 3.6.0. If none of
the possible releases is certified for the path the operator is blocked;
otherwise they are reported, marked in the table, with the upgrade each
non-covering one would need, for confirmation.

```
portworx-certified, Portworx Enterprise 3.6.0, OCP 4.18.14 -> 4.20.34 EUS

  4.18: certified 4.18.42  ok
  4.19: certified 4.19.31  ok
  4.20: certified 4.20.23  below 4.20.34
  -> blocked: upgrade Portworx Enterprise to 3.6.2 with operator 26.3.0 or
     later before the cluster upgrade (certified 4.18.54, 4.19.45, 4.20.36)
```

Under the operator's row the summary adds the vendor's whole support matrix
for this path, and the operator page repeats it: every release with its
certified z-stream on each OpenShift release of the path, whether it covers
the path, and the installed and recommended releases marked. Each matrix
column also shows the vendor's verdict for that release. The plan JSON
carries it as `vendor.table`.

```
Portworx Enterprise | Operator | 4.18 (>= 4.18.14) | 4.19 (listed) | 4.20 (>= 4.20.34) | Result
3.5.3               | 25.5.1+  | 4.18.54           | 4.19.45       | 4.20.36           | covers
3.6.0  installed    | 26.1.0+  | 4.18.42           | 4.19.31       | 4.20.23  x        | does not cover
3.6.1               | 26.2.0+  | 4.18.46           | 4.19.35       | 4.20.27  x        | does not cover
3.6.2  recommended  | 26.3.0+  | 4.18.54           | 4.19.45       | 4.20.36           | covers
```

### oc-mirror configuration

`imageset-config.yaml` (oc-mirror v2) lists, per catalog version, every
package, channel and `minVersion`/`maxVersion` range the operators pass
through. An operator needing no action keeps just its installed bundle, so the
package still exists in the mirrored catalog. Dependencies (`main: false`) are
added at the head of their channel. Where the package's default channel is not
one of the mirrored channels, `defaultChannel` is set, as oc-mirror requires.

### Matching against the catalogs

Names and versions are matched leniently against the catalogs, since a
subscription rarely records them exactly as the catalog does. A leading `v` is
optional on any version. Package names are resolved past vendor prefixes and
role suffixes (`openshift-mtv` finds `mtv-operator`). Build and vendor suffixes
such as `-rhodf` or `-202608142236` are kept and compared.

The installed version is looked up in the current release's catalog, then the
target, then any release between: in the subscribed channel first, then in
whichever channel holds it. A substitution is recorded in that operator's
`notes` so it can be checked against the real subscription. A version that no
catalog lists is still checked, by version, since upgrade edges do not need it
to be listed.

`cluster.channel` decides the path length and nothing else:

- `eus` — jumps two releases, from an even minor to the next even minor:
  `4.18 -> 4.19 -> 4.20`. An odd starting release is rejected.
- anything else — jumps one: `4.18 -> 4.19`

A mismatch is rejected rather than guessed:

```
'stable' upgrade expects a 1-release jump, but 4.18 -> 4.20 spans 2
```

## Command-line flags

| Flag | Default | Description |
|------|---------|-------------|
| `-i`, `--input` | stdin | Input JSON file |
| `--catalog-dir` | — | Use hand-supplied catalogs from this directory for every catalog image instead of pulling them |
| `--fetch-dir` | `<output-dir>/catalogs` | Where pulled catalogs are kept, shared by every cluster |
| `--refresh` | off | Pull catalogs again even if a previous pull covers the packages |
| `-a`, `--authfile` | pull secret | Registry credentials for pulling catalogs |
| `--filter-by-os` | `linux/amd64` | Platform of the catalog image to pull |
| `--jobs` | `4` | Catalog pulls to run at once |
| `--constraints` | `constraints/vendor-support.json` | Vendor support matrices |
| `--component-version` | — | `NAME=VERSION`, e.g. `portworx-enterprise=3.6.0`; overrides the input. Repeatable |
| `--insecure-registry` | off | Pull catalogs over HTTP or with an untrusted certificate |
| `-d`, `--output-dir` | `output` | Holds one folder per cluster and the shared `catalogs/` |
| `--imageset-out` | `<output-dir>/<cluster_name>/imageset-config.yaml` | oc-mirror ImageSetConfiguration path |
| `-j`, `--json-out` | — | Also write the plan JSON to this file |
| `-q`, `--quiet` | off | Suppress progress output on stderr |

## Catalogs

Named `data-v<major>.<minor>.json`, one per OCP release, holding the
`olm.channel` objects of an OLM file-based catalog. They are normally pulled at
run time into `<fetch-dir>/<image>/`. To work offline, point `--catalog-dir` at
a directory of them instead, holding every release on the path; the underscore
form `data-v4_18.json` is also accepted there. `catalogs/data-v4.22.json` is
an example of the format.

Only releases on the path are read. A 4.18 to 4.20 EUS run opens 4.18, 4.19 and
4.20 and ignores any other catalogs sitting there.

## Output

One folder per cluster, named after `cluster_name`, so a single output
directory can hold every cluster. The pulled catalogs are shared between them:

```
output/
  catalogs/                       shared, pulled once per image and release
    registry.redhat.io_redhat_redhat-operator-index/
      data-v4.18.json  packages-v4.18.json  ...
  ocp5/
    html/index.html               cluster summary
    html/<operator>/index.html    per-operator report
    imageset-config.yaml          oc-mirror configuration
    plan.json                     the plan, also printed to stdout
  <next cluster>/
    ...
```

The reports use the same PatternFly 6 look as the ocp_preupgrade_health_check
report (OpenShift console style, light and dark mode): a dark masthead with
the red accent, Red Hat Display/Text/Mono fonts, filled status labels, cards
and compact tables. The values are hand-picked from the @patternfly/patternfly
6.6.1 tokens instead of loading its ~2MB stylesheet. The fonts (`fonts/`, SIL
OFL 1.1) are copied once into each cluster's `html/fonts/` and shared by its
pages, so a cluster's `html/` folder keeps the look offline; without them the
pages fall back to system fonts.

The cluster summary has one column per catalog and lists the catalogs to
mirror. A column an operator is not upgraded from validates the bundle it is
on by then, in this order:

1. its metadata declares `maxOpenShiftVersion`: "supports 4.18 to 4.21"
2. no max declared, but that catalog ships the same channel and version: it
   works there, "✓ no action"
3. not shipped, but the target catalog's planned version is: the planned
   upgrade happens on that release, once, and the target column shows
   "✓ no action, upgraded on 4.19" (the bundles still come from the target
   catalog); otherwise, if a newer version of its channel is shipped, that
   upgrade is shown
4. none of these: a ⚠ warning, also added to the operator's notes; likewise
   when no bundle metadata is available (hand-supplied catalogs) or the
   operator is not found in its catalog image

When nothing has to be done, the row reads "✓ no action" in every column but
the target, which shows the newest version instead, "1.15.2 available,
optional", with its channel when that differs; "✓ no action" there only when
the installed version is already the newest.

A final pass over each finished row removes duplicates: an upgrade to the same
channel and version shown in more than one column is kept in the lowest one,
and the higher ones become "✓ no action, upgraded on" it.

When the input has no `max_ocp_version`, the installed bundle's value from the
pulled catalog is used. The plan JSON carries each row as `matrix`, and the
checks behind it as `columns`; the per-operator report has one row group per catalog the operator is
upgraded from — info table, graph, steps.

A shared catalog only grows: a cluster needing packages it lacks pulls it
again for the existing packages plus the new ones. Clusters can be planned in
parallel; each catalog is locked while it is checked and pulled, and written
atomically.

Abbreviated — each object carries more keys than shown:

```json
{
  "cluster": { "current": "4.18.14", "target": "4.20",
               "channel": "eus", "ocp_path": ["4.18", "4.19", "4.20"] },
  "verdict": "intermediate_catalog_required",
  "blocking_operators": [],
  "intermediate_catalog_operators": ["advanced-cluster-management"],
  "manual_review_operators": [],
  "operators_requiring_upgrade": ["redhat-oadp-operator"],
  "catalogs_to_mirror": {
    "4.19": ["registry.redhat.io/redhat/redhat-operator-index:v4.19"],
    "4.20": ["registry.redhat.io/redhat/redhat-operator-index:v4.20"]
  },
  "imageset_config": "imageset-config.yaml",
  "operators": [
    {
      "operator": "advanced-cluster-management",
      "verdict": "intermediate_catalog_required",
      "catalogs": ["4.19", "4.20"],
      "goal": { "channel": "release-2.17", "version": "2.17.1" },
      "phases": [
        { "phase": 1, "kind": "intermediate", "on_ocp": "4.19",
          "status": "upgrade_required", "hops": 1,
          "from": { "channel": "release-2.12", "version": "2.12.8" },
          "to":   { "channel": "release-2.13", "version": "2.13.0" },
          "steps": [ { "catalog": "4.19", "to_channel": "release-2.13",
                       "to_version": "2.13.0", "via": "skipRange" } ] },
        { "phase": 2, "kind": "target", "on_ocp": "4.20", "...": "..." }
      ],
      "notes": ["CRITICAL: the 4.20 catalog does not cover ..."],
      "html": "html/advanced-cluster-management/index.html"
    }
  ]
}
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | No action required, or operator upgrades are required and planned |
| 1 | Usage or input error |
| 2 | Manual review required |
| 3 | An operator is gone from the target catalog |
| 4 | An operator needs an intermediate catalog mirrored as well as the target catalog |

---

# 2. Single-catalog analyzer

The original tool. One operator, one catalog, shortest path to a target.

```bash
python operator_interactive.py -f catalogs/data-v4.22.json -o OPERATOR -v VERSION [-c CHANNEL] [-t TARGET]
```

| Flag | Required | Description |
|------|----------|-------------|
| `-f`, `--file` | yes | OLM catalog JSON file |
| `-o`, `--operator` | yes | Operator (package) name |
| `-v`, `--version` | yes | Current / start version |
| `-c`, `--target-channel` | no | Restrict the path to this channel |
| `-t`, `--target-version` | no | Stop here instead of the latest |
| `-d`, `--output-dir` | no | Output directory (default `.`) |

Output: `html/<operator>/index.html`

```bash
# latest version, crossing channels as needed
python operator_interactive.py -f catalogs/data-v4.22.json -o loki-operator -v 6.0.0
#   6.0.0 -> 6.5.0 (stable-6.5) -> 6.6.0 (stable-6.6)

# stay inside one channel
python operator_interactive.py -f catalogs/data-v4.22.json -o loki-operator -v 6.5.0 -c stable-6.5
#   6.5.0 -> 6.5.2

# target a specific version
python operator_interactive.py -f catalogs/data-v4.22.json -o compliance-operator -v 0.1.32 -t 1.9.2
#   0.1.32 -> 1.7.0 -> 1.9.2
```

Graph edges come from three sources: START via `skipRange`, the `replaces` chain
within a channel, and each upgrade-path version via `skipRange` — the last is
what draws jumps such as `1.21.0 -> 1.21.4`.

---

## Files

```
LICENSE                   Apache License 2.0
ocp_upgrade_planner.py    cluster planner CLI
ocp_planner.py            planning engine
ocp_report.py             HTML reports
catalog_fetch.py          pulls catalogs from catalog index images
mirror_plan.py            catalog mirroring check and oc-mirror configuration
vendor_constraints.py     vendor support matrices
fonts/                    Red Hat fonts for the reports (SIL OFL 1.1)
constraints/vendor-support.json  Portworx Enterprise support matrix
operator_interactive.py   single-catalog analyzer
examples/catalog_mirror_check.json  input template
catalogs/data-v4.22.json  example catalog, format of a pulled catalog
requirements.txt
```

---

## License

Apache License 2.0. See `LICENSE`.
