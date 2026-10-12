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
    return _resolve_from(bundles, package_bundles, root, notes)


def resolve_csv_dependencies(bundles, package: str, csv: str, fetch=None
                             ) -> Tuple[bool, List[str], List[str]]:
    """(found, [dependency csv, ...], [note, ...]) for an exact CSV of package."""
    def package_bundles(pkg):
        if pkg not in bundles and fetch:
            bundles.update(fetch([pkg]))
        return bundles.get(pkg) or {}
    root = next((b for b in package_bundles(package).values() if b['csv'] == csv), None)
    if root is None:
        return False, [], [f"{package}: {csv} is not in the target catalog"]
    _, deps, notes = _resolve_from(bundles, package_bundles, root, [])
    return True, deps, notes


def _resolve_from(bundles, package_bundles, root, notes):
    seen, deps, queue = {root['csv']}, {}, [(None, root)]
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
    Runtime Subscriptions whose exact dependency list is generated
    (approver.extra with dependenciesKey, e.g. odf-dependencies for ODF) are
    listed under the main package's 'extras'.
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
        extras = (data.get('approver') or {}).get('extra') or []
        if sub.get('name'):
            out[sub['name']] = {'component': name, 'versionKey': 'version',
                                'channelKey': 'channel', 'main': True,
                                'csvFormat': sub.get('csvFormat') or f"{sub['name']}.v%s",
                                'extras': [e for e in extras if e.get('dependenciesKey')]}
        for extra in extras:
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
        # Subscriptions this operator creates at runtime, with a generated
        # dependency list (e.g. odf-operator -> odf-dependencies)
        for extra in (m or {}).get('extras') or []:
            epkg = extra.get('package') or extra['subscription']
            if extra.get('csvFromMain') and csv:
                ecsv = f"{extra['csvFromMain']}.v" + re.sub(r'^.*?\.v(?=[0-9])', '', csv, count=1)
            elif extra.get('csv'):
                ecsv = extra['csv']
            elif extra.get('versionKey') in entry and extra.get('csvFormat'):
                ecsv = extra['csvFormat'] % entry[extra['versionKey']]
            else:
                notes.append(f"{pkg}: cannot name the {epkg} CSV - {extra['dependenciesKey']} not exported")
                continue
            found, edeps, enotes = resolve_csv_dependencies(
                bundles_for(res), epkg, ecsv, fetch_for(res))
            notes += enotes
            if found:
                key = extra['dependenciesKey']
                entry[key] = sorted(set(entry.get(key, [])) | set(edeps))
                comments[comp].append(f"{epkg} ({ecsv}): created by {pkg} at runtime; "
                                      f"its dependencies are in {key}")

    notes = list(dict.fromkeys(notes))                 # same package listed twice
    comments = {c: list(dict.fromkeys(v)) for c, v in comments.items()}
    lines = [f"# Generated by olm-upgrade-analyzer for cluster "
             f"{cluster.get('name') or '-'}: OCP {cluster['current']} -> "
             f"{cluster['target']} ({cluster['channel']}).",
             "# Exact versions in the target catalog with their exact OLM dependencies.",
             "# Merge into the GitOps config repository: components/versions.yaml (defaults),",
             "# group-values/<group>/versions.yaml (a group) or",
             "# clusters/<cluster>/main-values.yaml (one cluster).",
             "versions:" + ("" if entries else " {}")]
    order = ('channel', 'version', 'csv')
    for comp in sorted(entries):
        for c in comments[comp]:
            lines.append(f"  # {c}")
        lines.append(f"  {comp}:")
        entry = entries[comp]
        lists = sorted(k for k in entry if isinstance(entry[k], list))
        keys = [k for k in order if k in entry] + \
            sorted(k for k in entry if k not in order and k not in lists)
        for k in keys:
            lines.append(f"    {k}: {_scalar(entry[k])}")
        for k in lists:
            if entry[k]:
                lines.append(f"    {k}:")
                lines += [f"      - {_scalar(d)}" for d in entry[k]]
            else:
                lines.append(f"    {k}: []")
    if notes:
        lines.append("")
        lines += [f"# NOTE: {n}" for n in notes]
    return "\n".join(lines) + "\n", notes
