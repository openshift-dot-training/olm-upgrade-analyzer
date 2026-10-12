#!/usr/bin/env python3
"""
OCP cluster upgrade planner - CLI.

Reads the installed operators as JSON, checks each one against the target
release's catalog first, working backwards to an intermediate catalog only when
the target does not cover it, and writes one HTML report per operator, a
cluster summary and an oc-mirror ImageSetConfiguration. Machine-readable JSON
goes to stdout.

    python ocp_upgrade_planner.py -i examples/catalog_mirror_check.json

Input is the catalog mirror check written by ocp_preupgrade_health_check,
grouped by catalog index image. The catalogs are pulled from those images at
run time, or read from --catalog-dir. Packages with main=false are
dependencies: they are not planned, but are kept in the oc-mirror
configuration.

    {
      "cluster_name": "ocp5",
      "cluster": { "current": "4.18.28", "target": "4.20", "channel": "eus",
                   "ocp_path": ["4.18", "4.19", "4.20"] },
      "operators": [
        { "pull_image": "registry.redhat.io/redhat/redhat-operator-index:v4.18",
          "packages": [ { "name": "odf-operator", "channel": "stable-4.18",
                          "version": "4.18.3", "max_ocp_version": "",
                          "main": true, "required_by": [] } ] }
      ]
    }

The outputs - html/, imageset-config.yaml and plan.json - go to
<output-dir>/<cluster_name>/, so many clusters can share one output directory.
Pulled catalogs are shared between them in <output-dir>/catalogs/. Catalog
files are named data-v<major>.<minor>.json, one per OCP release.

Exit codes:
    0  no action required, or operator upgrades are required and planned
    2  manual review required
    3  an operator is gone from the target catalog
    4  an intermediate catalog must be mirrored as well as the target catalog
    1  usage or input error
"""

import argparse
import json
import re
import sys
from pathlib import Path

from catalog_fetch import (
    FetchError,
    fetch_all,
    load_bundle_max_ocp,
    load_default_channels,
    retag,
)
from mirror_plan import (
    BLOCKED,
    INTERMEDIATE,
    REVIEW,
    UPGRADE,
    build_imageset,
    build_matrix,
    column_checks,
    declared_max_ocp,
    mirror_sets,
    pick_default_channel,
    plan_operator,
)
from ocp_planner import (
    version_sort_key as _version_key,
    build_ocp_path,
    check_catalog_dir,
    load_catalogs,
    catalog_filename,
)
from ocp_report import generate_operator_report, generate_summary_report
from vendor_constraints import BLOCKED as VENDOR_BLOCKED
from vendor_constraints import evaluate as evaluate_vendor
from vendor_constraints import load_constraints

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REVIEW = 2
EXIT_BLOCKED = 3
EXIT_CRITICAL = 4


def parse_payload(payload):
    """
    Return (cluster, groups) from a catalog mirror check.

    Each group is {'pull_image': str or None, 'operators': [...]}, where every
    operator carries name, channel, version, max_ocp_version, main and
    required_by.
    """
    cluster = payload.get('cluster') or {}
    entries = payload.get('operators') or []
    if not entries:
        raise ValueError("at least one entry in 'operators' is required")

    groups = []
    for e in entries:
        image = e.get('pull_image')
        if not image:
            raise ValueError(
                "each operators[] entry requires 'pull_image' and 'packages' "
                "(catalog mirror check format from ocp_preupgrade_health_check)")
        ops = [{'name': p.get('name'),
                'channel': p.get('channel', ''),
                'version': p.get('version', ''),
                'max_ocp_version': p.get('max_ocp_version') or '',
                'main': p.get('main', True),
                'required_by': p.get('required_by') or [],
                'component_versions': p.get('component_versions') or {}}
               for p in e.get('packages') or []]
        groups.append({'pull_image': image, 'operators': ops})

    for g in groups:
        for op in g['operators']:
            if not op['name']:
                raise ValueError("each operator requires a 'name'")
    if not any(op['main'] for g in groups for op in g['operators']):
        raise ValueError("no operator with main=true to plan")
    return cluster, groups


def cluster_name(payload):
    """
    The cluster's name, made safe for use as a folder name, or None.

    The health check writes it as cluster_name; cluster-name is accepted too.
    """
    raw = payload.get('cluster_name') or payload.get('cluster-name')
    if not raw:
        return None
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', str(raw).strip()).strip('._')
    if not name:
        raise ValueError(f"cluster_name {raw!r} cannot be used as a folder name")
    if name == 'catalogs':
        raise ValueError("cluster_name 'catalogs' would clash with the shared "
                         "catalog folder")
    return name


def resolve_ocp_path(cluster):
    current = cluster.get('current')
    target = cluster.get('target')
    channel = cluster.get('channel', 'stable')
    if not current or not target:
        raise ValueError("cluster.current and cluster.target are required")
    ocp_path = build_ocp_path(current, target, channel)
    given = cluster.get('ocp_path')
    if given and [str(o) for o in given] != ocp_path:
        raise ValueError(
            f"cluster.ocp_path {' -> '.join(map(str, given))} does not match "
            f"the {channel} path {' -> '.join(ocp_path)}")
    return current, target, channel, ocp_path


def resolve_upgrade_path(cluster, ocp_path):
    """
    cluster.upgrade_path, the versions the cluster steps through
    (["4.18.14", "4.18.30", "4.19.33", "4.20.34"]), validated, or None.
    """
    path = cluster.get('upgrade_path')
    if not path:
        return None
    path = [str(v).strip().lstrip('v') for v in path]
    if any(not re.match(r'^\d+\.\d+\.\d+', v) for v in path):
        raise ValueError(f"cluster.upgrade_path needs full versions: {path}")
    keys = [tuple(int(x) for x in re.findall(r'\d+', v)[:3]) for v in path]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ValueError(f"cluster.upgrade_path is not ascending: {path}")
    minors = {f"{k[0]}.{k[1]}" for k in keys}
    if minors != set(ocp_path):
        raise ValueError(
            f"cluster.upgrade_path {' -> '.join(path)} does not cover the "
            f"releases {' -> '.join(ocp_path)}")
    for end, ver in (('current', path[0]), ('target', path[-1])):
        given = str(cluster.get(end, '')).lstrip('v')
        if given.count('.') >= 2 and given != ver:
            raise ValueError(f"cluster.upgrade_path starts or ends at {ver}, "
                             f"but cluster.{end} is {given}")
    return path


def _imageset_entries(catalogs, ocp_path, group, results, catalog_dir):
    """oc-mirror catalog entries for one catalog index image."""
    wanted = {}  # ocp -> {pkg: {channel: [versions]}}
    for res in results:
        for ocp, chans in mirror_sets(res).items():
            slot = wanted.setdefault(ocp, {}).setdefault(res['operator'], {})
            for chan, versions in chans.items():
                slot.setdefault(chan, []).extend(versions)

    entries = []
    for ocp in sorted(wanted, key=ocp_path.index):
        defaults = load_default_channels(catalog_dir, ocp)
        packages = []
        for name, chans in sorted(wanted[ocp].items()):
            packages.append({'name': name, 'channels': chans,
                             'defaultChannel': pick_default_channel(
                                 chans, defaults.get(name))})
        # Dependencies are not planned, but the mirrored catalog must still
        # carry them or OLM cannot resolve the operators that need them.
        for op in group['operators']:
            if op['main']:
                continue
            chans = catalogs[ocp].get(op['name'], {})
            chan = (op['channel'] if op['channel'] in chans
                    else defaults.get(op['name']))
            if not chan or chan not in chans:
                continue
            by = ', '.join(op['required_by']) or 'another operator'
            packages.append({'name': op['name'], 'channels': {chan: None},
                             'defaultChannel': pick_default_channel(
                                 {chan: None}, defaults.get(op['name'])),
                             'comment': f"dependency of {by}; channel head"})
        entries.append({'catalog': retag(group['pull_image'], ocp),
                        'packages': packages})
    return entries


def _apply_vendor(res, op, constraints, component_overrides, cluster_info,
                  catalogs):
    """
    Check an operator against its vendor support matrix, if it has one. A
    blocked result makes the operator block the cluster upgrade.
    """
    constraint = (constraints.get(res['operator'])
                  or constraints.get(op['name']))
    if not constraint:
        return
    comp = constraint['component']
    version = (component_overrides.get(comp)
               or op['component_versions'].get(comp))
    inp = res['input']
    vendor = evaluate_vendor(
        constraint, cluster_info['ocp_path'], cluster_info['current'],
        cluster_info['target'], version,
        inp.get('resolved_version', inp['version']),
        cluster_info.get('upgrade_path'))
    res['vendor'] = vendor
    res['notes'] += vendor['notes']

    # Where the operator version the vendor requires can come from.
    need = (vendor.get('recommended') or {}).get('operator_min')
    installed = inp.get('resolved_version', inp['version'])
    if need and _version_key(installed) < _version_key(need):
        current = cluster_info['ocp_path'][0]
        offered = sorted(
            ((v, c) for c, vs in catalogs[current].get(res['operator'], {}).items()
             for v in vs if _version_key(v) >= _version_key(need)),
            # the subscribed channel first, then the lowest version
            key=lambda vc: (vc[1] != inp.get('resolved_channel', inp['channel']),
                            _version_key(vc[0])))
        if offered:
            v, c = offered[0]
            res['notes'].append(
                f"The current {current} catalog carries operator {v} (channel "
                f"{c}), the lowest meeting {need}.")
        else:
            res['notes'].append(
                f"The current {current} catalog has no operator {need} or "
                f"later; it must be mirrored before the cluster upgrade.")
    if vendor['status'] == VENDOR_BLOCKED:
        res.update(verdict=BLOCKED, blocking=True)


def run(payload, catalog_dirs, output_dir, quiet=False, imageset_out=None,
        fetched=False, constraints=None, component_overrides=None):
    """
    catalog_dirs maps each group's pull_image to the directory holding its
    catalogs. fetched marks catalogs pulled at run time, which are empty when
    the image carries none of the packages.
    """
    cluster, groups = parse_payload(payload)
    current, target, channel, ocp_path = resolve_ocp_path(cluster)
    cluster_info = {'name': cluster_name(payload), 'current': current,
                    'target': target, 'channel': channel, 'ocp_path': ocp_path,
                    'upgrade_path': resolve_upgrade_path(cluster, ocp_path)}

    if not quiet:
        print(f"Cluster: OCP {current} -> {target} ({channel})", file=sys.stderr)
        print(f"Path:    {' -> '.join(ocp_path)}", file=sys.stderr)

    results = []
    imageset = []
    for group in groups:
        catalog_dir = catalog_dirs[group['pull_image']]
        if not quiet:
            print(f"{group['pull_image']}: {len(ocp_path)} catalog(s) from "
                  f"{catalog_dir}", file=sys.stderr)
        catalogs = load_catalogs(catalog_dir, ocp_path, allow_empty=fetched)
        bundle_max = {o: load_bundle_max_ocp(catalog_dir, o) for o in ocp_path}

        group_results = []
        for op in group['operators']:
            if not op['main']:
                continue
            res = plan_operator(catalogs, ocp_path, op, bundle_max)
            for ph in res['phases']:
                ph['to']['max_ocp_version'] = declared_max_ocp(
                    bundle_max, ocp_path, res['operator'], ph['to']['version'])
            if res['phases']:
                column_checks(catalogs, ocp_path, res)
            _apply_vendor(res, op, constraints or {},
                          component_overrides or {}, cluster_info, catalogs)
            build_matrix(ocp_path, res)
            res['input_name'] = op['name']
            res['catalog_image'] = group['pull_image']
            group_results.append(res)
            if not quiet:
                print(f"  {op['name']:44} {res['verdict']:30} catalogs "
                      f"{', '.join(res['catalogs']) or '-'}", file=sys.stderr)

        imageset += _imageset_entries(catalogs, ocp_path, group,
                                      group_results, catalog_dir)
        for res in group_results:
            res['html'] = generate_operator_report(
                catalogs, res, cluster_info, output_dir)
        results += group_results

    def named(verdict):
        return [r['operator'] for r in results if r['verdict'] == verdict]

    blocking = named(BLOCKED)
    intermediate = named(INTERMEDIATE)
    review = named(REVIEW)
    upgrades = named(UPGRADE)

    if blocking:
        verdict = BLOCKED
    elif intermediate:
        verdict = INTERMEDIATE
    elif review:
        verdict = REVIEW
    elif upgrades:
        verdict = UPGRADE
    else:
        verdict = 'no_action_required'

    to_mirror = {}
    for entry in imageset:
        ocp = entry['catalog'].rsplit(':v', 1)[-1]
        to_mirror.setdefault(ocp, []).append(entry['catalog'])

    path = Path(imageset_out or Path(output_dir) / 'imageset-config.yaml')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_imageset(imageset))

    plan = {
        'cluster': cluster_info,
        'verdict': verdict,
        'blocking_operators': blocking,
        'intermediate_catalog_operators': intermediate,
        'manual_review_operators': review,
        'operators_requiring_upgrade': upgrades,
        'catalogs': {o: catalog_filename(o) for o in ocp_path},
        'catalogs_to_mirror': to_mirror,
        'imageset_config': str(path),
        'operators': results,
    }
    plan['summary_html'] = generate_summary_report(plan, output_dir)
    return plan


def main():
    ap = argparse.ArgumentParser(
        description="Plan OLM operator upgrades around an OCP cluster upgrade.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i outputs/catalog_mirror_check.json
  %(prog)s -i outputs/catalog_mirror_check.json --authfile ~/pull-secret.json
  %(prog)s -i outputs/catalog_mirror_check.json --catalog-dir /path/to/catalogs
  %(prog)s -i outputs/catalog_mirror_check.json \\
      --component-version portworx-enterprise=3.6.0
""")
    ap.add_argument('-i', '--input', help='Input JSON file (default: stdin)')
    ap.add_argument('--catalog-dir',
                    help='Use the data-v<major>.<minor>.json catalogs in this '
                         'directory for every catalog image instead of '
                         'pulling them')
    ap.add_argument('--fetch-dir',
                    help='Where pulled catalogs are kept, one subdirectory per '
                         'catalog index (default: <output-dir>/catalogs)')
    ap.add_argument('--refresh', action='store_true',
                    help='Pull catalogs again even when a previous pull '
                         'already covers the packages')
    ap.add_argument('-a', '--authfile',
                    help='Registry credentials for pulling catalogs (default: '
                         'the cluster pull secret, read with oc extract)')
    ap.add_argument('--filter-by-os', default='linux/amd64',
                    help='Platform of the catalog image to pull '
                         '(default: linux/amd64)')
    ap.add_argument('--constraints',
                    help='Vendor support matrices (default: '
                         'constraints/vendor-support.json beside this tool)')
    ap.add_argument('--component-version', action='append', default=[],
                    metavar='NAME=VERSION',
                    help='Version of a component a vendor matrix is keyed '
                         'on, e.g. portworx-enterprise=3.6.0; overrides the '
                         'input\'s component_versions. Repeatable')
    ap.add_argument('--jobs', type=int, default=4,
                    help='Catalog pulls to run at once (default: 4)')
    ap.add_argument('--insecure-registry', action='store_true',
                    help='Allow pulling catalogs over HTTP or with an '
                         'untrusted certificate')
    ap.add_argument('-d', '--output-dir', default='output',
                    help='Where to write the outputs, in a folder named after '
                         'the cluster_name, and the shared pulled catalogs '
                         '(default: output)')
    ap.add_argument('--imageset-out',
                    help='Where to write the oc-mirror ImageSetConfiguration '
                         '(default: <output-dir>/<cluster_name>/imageset-config.yaml)')
    ap.add_argument('-j', '--json-out',
                    help='Also write the plan JSON to this file')
    ap.add_argument('-q', '--quiet', action='store_true',
                    help='Suppress progress output on stderr')
    args = ap.parse_args()

    try:
        raw = (Path(args.input).read_text() if args.input
               else sys.stdin.read())
    except OSError as e:
        print(f"Cannot read input: {e}", file=sys.stderr)
        return EXIT_ERROR

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Input is not valid JSON: {e}", file=sys.stderr)
        return EXIT_ERROR

    try:
        cluster, groups = parse_payload(payload)
        name = cluster_name(payload)
        cluster_dir = Path(args.output_dir) / name if name else Path(args.output_dir)

        fetched = not args.catalog_dir
        if not fetched:
            catalog_dir = check_catalog_dir(args.catalog_dir)
            if not args.quiet:
                print(f"Catalogs: {catalog_dir}", file=sys.stderr)
            catalog_dirs = {g['pull_image']: catalog_dir for g in groups}
        else:
            _, _, _, ocp_path = resolve_ocp_path(cluster)
            fetch_dir = args.fetch_dir or str(Path(args.output_dir) / 'catalogs')
            if not args.quiet:
                print(f"Pulling catalogs into {fetch_dir}", file=sys.stderr)
            catalog_dirs = fetch_all(
                [{'pull_image': g['pull_image'],
                  'packages': [op['name'] for op in g['operators']]}
                 for g in groups],
                ocp_path, fetch_dir, args.authfile, args.refresh,
                args.filter_by_os, args.insecure_registry, args.quiet,
                args.jobs)

        overrides = {}
        for item in args.component_version:
            name, sep, ver = item.partition('=')
            if not sep or not name or not ver:
                raise ValueError(f"--component-version expects NAME=VERSION, "
                                 f"got {item!r}")
            overrides[name.strip()] = ver.strip()
        plan = run(payload, catalog_dirs, str(cluster_dir), args.quiet,
                   args.imageset_out, fetched,
                   load_constraints(args.constraints), overrides)
    except (ValueError, FileNotFoundError, FetchError) as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_ERROR

    out = json.dumps(plan, indent=2)
    print(out)
    (cluster_dir / 'plan.json').write_text(out + "\n")
    if args.json_out:
        Path(args.json_out).write_text(out)

    if not args.quiet:
        print(f"\nSummary: {plan['summary_html']}", file=sys.stderr)
        print(f"oc-mirror: {plan['imageset_config']}", file=sys.stderr)

    return {BLOCKED: EXIT_BLOCKED,
            INTERMEDIATE: EXIT_CRITICAL,
            REVIEW: EXIT_REVIEW}.get(plan['verdict'], EXIT_OK)


if __name__ == '__main__':
    sys.exit(main())
