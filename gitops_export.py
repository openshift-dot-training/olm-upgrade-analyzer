#!/usr/bin/env python3
"""
GitOps versions export.

Turns a cluster plan into the `versions:` block read by an OLM GitOps
repository that installs every operator at an exact version with
installPlanApproval: Manual and approves only the InstallPlan carrying that
exact CSV (and its exact OLM dependencies):

    versions:
      <component>:
        channel: stable-4.20
        version: 4.20.2-rhodf
        csv: odf-operator.v4.20.2-rhodf
        dependencies:
          - mcg-operator.v4.20.2-rhodf
          - ocs-operator.v4.20.2-rhodf

For each planned operator the goal (channel and version in the target
catalog) is looked up in the bundle metadata recorded when the catalogs were
pulled, and its OLM dependencies - olm.package.required, olm.gvk.required and
olm.constraint - are resolved transitively, as OLM would: the highest bundle
satisfying a version range, the highest bundle providing a required GVK.
Dependency packages that were not pulled yet (new dependencies of the target
version) are pulled on demand.

Component names: with a GitOps repository checkout (--gitops-repo), packages
are mapped to its component names through components/*/values.yaml
(subscription.name). Packages installed by another operator at runtime
(approver.extra, e.g. multicluster-engine for ACM) become that component's
versionKey / channelKey. Without it, the package name is the key.
"""

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None


class ExportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# OLM version ranges (blang/semver, as OLM evaluates versionRange)
# ---------------------------------------------------------------------------

_SEMVER = re.compile(r'^v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?'
                     r'(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')


def _parse(ver: str):
    m = _SEMVER.match(str(ver).strip())
    if not m:
        raise ExportError(f"not a semver version: {ver!r}")
    core = tuple(int(n) if n and n.isdigit() else 0
                 for n in (m.group(1), m.group(2), m.group(3)))
    pre = tuple(m.group(4).split('.')) if m.group(4) else ()
    return core, pre


def semver_cmp(a: str, b: str) -> int:
    (ca, pa), (cb, pb) = _parse(a), _parse(b)
    if ca != cb:
        return -1 if ca < cb else 1
    if pa == pb:
        return 0
    if not pa or not pb:            # a release ranks above its pre-releases
        return 1 if not pa else -1
    for x, y in zip(pa, pb):
        if x == y:
            continue
        if x.isdigit() and y.isdigit():
            return -1 if int(x) < int(y) else 1
        if x.isdigit() != y.isdigit():
            return -1 if x.isdigit() else 1
        return -1 if x < y else 1
    return (len(pa) > len(pb)) - (len(pa) < len(pb))


def _wildcard(ver: str) -> List[Tuple[str, str]]:
    """'4.18.x' -> >=4.18.0 <4.19.0 ; '4.x' -> >=4.0.0 <5.0.0"""
    m = _SEMVER.match(ver)
    major, minor = m.group(1), m.group(2)
    if minor in (None, 'x', 'X', '*'):
        return [('>=', f"{major}.0.0"), ('<', f"{int(major) + 1}.0.0")]
    return [('>=', f"{major}.{minor}.0"), ('<', f"{major}.{int(minor) + 1}.0")]


def in_range(ver: str, rng: str) -> bool:
    """OR of '||' alternatives, each an AND of comparators."""
    if not rng or not rng.strip():
        return True
    for alt in rng.split('||'):
        ok = True
        for tok in re.split(r'[\s,]+', alt.strip()):
            if not tok:
                continue
            m = re.match(r'^(>=|<=|!=|==|=|>|<)?\s*(.+)$', tok)
            op, bound = m.group(1) or '=', m.group(2)
            checks = (_wildcard(bound) if op == '=' and re.search(r'\.[xX*]', bound)
                      else [(op, bound)])
            for op2, b2 in checks:
                c = semver_cmp(ver, b2)
                if not {'>': c > 0, '>=': c >= 0, '<': c < 0, '<=': c <= 0,
                        '=': c == 0, '==': c == 0, '!=': c != 0}[op2]:
                    ok = False
        if ok:
            return True
    return False


def _highest(versions: List[str]) -> Optional[str]:
    best = None
    for v in versions:
        try:
            if best is None or semver_cmp(v, best) > 0:
                best = v
        except ExportError:
            continue
    return best


# ---------------------------------------------------------------------------
# dependency resolution
# ---------------------------------------------------------------------------

def resolve_dependencies(bundles: Dict[str, Dict[str, Dict]], package: str,
                         version: str,
                         fetch: Optional[Callable[[List[str]], Dict]] = None
                         ) -> Tuple[Optional[str], List[str], List[str]]:
    """
    (csv, [dependency csv, ...], [note, ...]) for package at version.

    bundles: {package: {version: {'csv', 'requires', 'provides'}}} of the
    target catalog(s); fetch(packages) pulls missing packages and returns
    their bundles, merged into bundles.
    """
    notes: List[str] = []

    def package_bundles(pkg):
        if pkg not in bundles and fetch:
            bundles.update(fetch([pkg]))
        return bundles.get(pkg) or {}

    root = package_bundles(package).get(version)
    if root is None:
        return None, [], [f"{package} {version}: no bundle metadata in the "
                          "target catalog - dependencies not exported"]
    seen, deps, queue = {root['csv']}, {}, [(package, root)]
    while queue:
        pkg, bundle = queue.pop(0)
        for req in bundle.get('requires') or []:
            if 'package' in req:
                name = req['package']
                cands = [v for v in package_bundles(name) if in_range(v, req['range'])]
                ver = _highest(cands)
                if ver is None:
                    notes.append(f"{bundle['csv']} requires {name} "
                                 f"{req['range'] or '(any)'}: not in the target catalog")
                    continue
                dep = (name, bundles[name][ver])
            elif 'gvk' in req:
                providers = [(p, v) for p, vs in bundles.items()
                             for v, b in vs.items() if req['gvk'] in b.get('provides', [])]
                if not providers:
                    notes.append(f"{bundle['csv']} requires {req['gvk']}: no pulled "
                                 "package provides it - add the providing package "
                                 "to the input (main: false)")
                    continue
                top = _highest([v for _, v in providers])
                name = next(p for p, v in providers if v == top)
                dep = (name, bundles[name][top])
            else:
                notes.append(f"{bundle['csv']}: olm.constraint "
                             f"{req.get('unsupported')} not evaluated - check by hand")
                continue
            if dep[1]['csv'] not in seen:
                seen.add(dep[1]['csv'])
                deps[dep[1]['csv']] = dep
                queue.append(dep)
    return root['csv'], sorted(deps), notes


# ---------------------------------------------------------------------------
# GitOps repository component map
# ---------------------------------------------------------------------------

def component_map(repo: str) -> Dict[str, Dict]:
    """
    {package: {'component', 'versionKey', 'channelKey', 'csvFormat'}} from a
    GitOps checkout's components/*/values.yaml. Main subscriptions use the
    'version' / 'channel' keys; approver.extra entries their own keys.
    """
    if yaml is None:
        raise ExportError("--gitops-repo needs PyYAML (pip install -r requirements.txt)")
    comps = Path(repo) / 'components'
    if not comps.is_dir():
        raise ExportError(f"{repo}: no components/ directory")
    out: Dict[str, Dict] = {}
    for values in sorted(comps.glob('*/values.yaml')):
        data = yaml.safe_load(values.read_text()) or {}
        name = values.parent.name
        sub = (data.get('subscription') or {})
        if sub.get('name'):
            out[sub['name']] = {'component': name, 'versionKey': 'version',
                                'channelKey': 'channel', 'main': True,
                                'csvFormat': sub.get('csvFormat') or f"{sub['name']}.v%s"}
        for extra in (data.get('approver') or {}).get('extra') or []:
            if extra.get('subscription') and extra.get('versionKey'):
                out[extra['subscription']] = {
                    'component': name, 'versionKey': extra['versionKey'],
                    'channelKey': extra.get('channelKey'), 'main': False,
                    'csvFormat': extra.get('csvFormat')}
    return out


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------

def _scalar(value) -> str:
    """Plain scalar unless YAML would read it as something other than a string."""
    text = str(value)
    if re.fullmatch(r'[0-9]+(\.[0-9]+)?|true|false|yes|no|on|off|null|~|', text,
                    re.IGNORECASE) or re.search(r'[:#{}\[\],&*!|>\'"%@`]', text):
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return text


def build_versions(plan: Dict, bundles_for: Callable[[Dict], Dict],
                   fetch_for: Callable[[Dict], Optional[Callable]],
                   mapping: Optional[Dict[str, Dict]] = None) -> Tuple[str, List[str]]:
    """
    Render the versions block for every planned operator with a goal.
    Returns (yaml text, notes). bundles_for(result) / fetch_for(result) give
    the target catalog bundles and a puller for that operator's catalog image.
    """
    cluster = plan['cluster']
    entries: Dict[str, Dict] = {}
    comments: Dict[str, List[str]] = {}
    notes: List[str] = []

    for res in plan['operators']:
        goal = res.get('goal')
        pkg = res['operator']
        if not goal or not goal.get('version'):
            notes.append(f"{pkg}: no goal in the target catalog ({res['verdict']}) - not exported")
            continue
        csv, deps, dep_notes = resolve_dependencies(
            bundles_for(res), pkg, goal['version'], fetch_for(res))
        notes += dep_notes
        m = (mapping or {}).get(pkg)
        if mapping is not None and m is None:
            notes.append(f"{pkg}: no component in the GitOps repository - exported under its package name")
        comp = m['component'] if m else pkg
        entry = entries.setdefault(comp, {})
        comments.setdefault(comp, [])
        if m and not m['main']:
            entry[m['versionKey']] = goal['version']
            if m.get('channelKey'):
                entry[m['channelKey']] = goal['channel']
            if m.get('csvFormat') and csv and m['csvFormat'] % goal['version'] != csv:
                notes.append(f"{pkg}: {m['csvFormat']} gives {m['csvFormat'] % goal['version']}, "
                             f"the catalog CSV is {csv} - fix csvFormat in components/{comp}/values.yaml")
            if deps:
                comments[comp].append(f"{pkg} dependencies, not approved by the GitOps "
                                      f"extra approver: {', '.join(deps)}")
            continue
        entry['channel'] = goal['channel']
        entry['version'] = goal['version']
        if csv:
            entry['csv'] = csv
        entry['dependencies'] = deps
        comments[comp].append(f"{pkg}: {res['verdict']}")

    lines = [f"# Generated by olm-upgrade-analyzer for cluster "
             f"{cluster.get('name') or '-'}: OCP {cluster['current']} -> "
             f"{cluster['target']} ({cluster['channel']}).",
             "# Exact versions in the target catalog with their exact OLM dependencies.",
             "# Merge into the GitOps repository: components/versions.yaml (defaults),",
             "# group-values/<group>/versions.yaml (a group) or the cluster environment",
             "# repository's config/main-values.yaml (one cluster).",
             "versions:" + ("" if entries else " {}")]
    order = ('channel', 'version', 'csv')
    for comp in sorted(entries):
        for c in comments[comp]:
            lines.append(f"  # {c}")
        lines.append(f"  {comp}:")
        entry = entries[comp]
        keys = [k for k in order if k in entry] + \
            sorted(k for k in entry if k not in order and k != 'dependencies')
        for k in keys:
            lines.append(f"    {k}: {_scalar(entry[k])}")
        if 'dependencies' in entry:
            if entry['dependencies']:
                lines.append("    dependencies:")
                lines += [f"      - {_scalar(d)}" for d in entry['dependencies']]
            else:
                lines.append("    dependencies: []")
    if notes:
        lines.append("")
        lines += [f"# NOTE: {n}" for n in notes]
    return "\n".join(lines) + "\n", notes
