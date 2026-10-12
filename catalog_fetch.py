#!/usr/bin/env python3
"""
Fetch OLM catalogs at run time from catalog index images.

For every catalog index image and every OCP release on the upgrade path, the
image tag is moved to that release (:v4.18 -> :v4.19, :v4.20) and only the
installed packages are pulled out of it:

    oc image extract <image>:v4.19 --filter-by-os=linux/amd64 -a <authfile> \\
        --path /configs/<pkg>/:<dir> [--path ...]

A package directory mixes a catalog.json with per-bundle files. Only the
olm.channel objects are kept, which is exactly the data-v<major>.<minor>.json
format the planner reads. Beside the catalog, packages-v<major>.<minor>.json
records each package's defaultChannel, for the oc-mirror configuration, and
each bundle's olm.maxOpenShiftVersion, for the planner.

The registry credentials come from --authfile, or from the cluster pull secret
when no authfile is given.

One catalog directory is shared by every cluster planned against it, so a pull
adds its packages to those already there instead of replacing them, under a
per-catalog lock so that clusters can be planned in parallel.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ocp_planner import catalog_filename, extract_version_from_name, parse_ocp

try:
    import yaml
except ImportError:  # only needed for catalogs shipped as YAML
    yaml = None

OCP_TAG = re.compile(r'^v?\d+\.\d+$')
DEFAULT_OS = 'linux/amd64'
DEFAULT_JOBS = 4


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Image references
# ---------------------------------------------------------------------------

def split_image(image: str):
    """Split an image reference into (repository, tag). Digests are refused."""
    if '@' in image:
        raise FetchError(
            f"{image} is pinned by digest, so it cannot be retagged per OCP "
            f"release. Use a :v<major>.<minor> tag.")
    slash = image.rfind('/')
    colon = image.rfind(':')
    if colon > slash:
        return image[:colon], image[colon + 1:]
    return image, None


def retag(image: str, ocp: str) -> str:
    """Move a catalog index image to the tag of another OCP release."""
    repo, tag = split_image(image)
    if tag is None or not OCP_TAG.match(tag):
        raise FetchError(
            f"{image} does not carry an OCP release tag such as :v4.18, so the "
            f"matching catalog for {ocp} cannot be derived from it.")
    major, minor = parse_ocp(ocp)
    return f"{repo}:v{major}.{minor}"


def image_slug(image: str) -> str:
    """A directory name for one catalog index, independent of its tag."""
    repo, _ = split_image(image)
    return re.sub(r'[^A-Za-z0-9._-]+', '_', repo)


def packages_filename(ocp: str) -> str:
    major, minor = parse_ocp(ocp)
    return f"packages-v{major}.{minor}.json"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def cluster_authfile(dest_dir: str) -> str:
    """Extract the cluster pull secret to dest_dir and return its path."""
    cmd = ['oc', 'extract', 'secret/pull-secret', '-n', 'openshift-config',
           '--keys=.dockerconfigjson', f'--to={dest_dir}', '--confirm']
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(
            "Could not read the cluster pull secret "
            "(oc extract secret/pull-secret -n openshift-config). Log in to "
            "the cluster or pass --authfile.\n" + proc.stderr.strip())
    path = Path(dest_dir) / '.dockerconfigjson'
    if not path.is_file():
        raise FetchError(f"oc extract did not write {path}")
    return str(path)


# ---------------------------------------------------------------------------
# File-based catalog parsing
# ---------------------------------------------------------------------------

def _json_stream(text: str) -> Iterable[Dict]:
    """Yield every JSON object in a file holding one or more of them."""
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        obj, i = decoder.raw_decode(text, i)
        if isinstance(obj, dict):
            yield obj


def _read_objects(path: Path) -> Iterable[Dict]:
    text = path.read_text()
    if path.suffix == '.json':
        yield from _json_stream(text)
    elif path.suffix in ('.yaml', '.yml'):
        if yaml is None:
            raise FetchError(
                f"{path} is YAML; install PyYAML to read it")
        for obj in yaml.safe_load_all(text):
            if isinstance(obj, dict):
                yield obj


def _bundle_max_ocp(bundle: Dict) -> Optional[str]:
    """A bundle's olm.maxOpenShiftVersion property, if it declares one."""
    for prop in bundle.get('properties') or []:
        if prop.get('type') == 'olm.maxOpenShiftVersion':
            value = str(prop.get('value', '')).strip().strip('"\'')
            return value or None
    return None


def read_package_dir(pkg_dir: Path):
    """
    Collect the olm.channel objects, the defaultChannel and each bundle's
    olm.maxOpenShiftVersion ({version: max}) of one package.

    The package directory holds a catalog.json plus per-bundle files, possibly
    in subdirectories. Channels are de-duplicated by name.
    """
    channels: Dict[str, Dict] = {}
    default = None
    max_ocp: Dict[str, str] = {}
    for path in sorted(pkg_dir.rglob('*')):
        if not path.is_file() or path.suffix not in ('.json', '.yaml', '.yml'):
            continue
        for obj in _read_objects(path):
            schema = obj.get('schema')
            if schema == 'olm.channel':
                channels[obj.get('name')] = obj
            elif schema == 'olm.package':
                default = obj.get('defaultChannel') or default
            elif schema == 'olm.bundle':
                ver = extract_version_from_name(obj.get('name', ''))
                top = _bundle_max_ocp(obj)
                if ver and top:
                    max_ocp[ver] = top
    return [channels[k] for k in sorted(channels)], default, max_ocp


def _write_atomic(path: Path, text: str):
    """Replace path in one step, so a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_catalog(path: Path, channels: List[Dict]):
    """Write olm.channel objects in the data-v<major>.<minor>.json format."""
    _write_atomic(path, "\n".join(json.dumps(c, indent=2)
                                  for c in channels) + "\n")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_packages(image: str, packages: List[str], authfile: str,
                     work_dir: Path, os_filter: str = DEFAULT_OS,
                     insecure: bool = False) -> Dict[str, Path]:
    """
    Pull /configs/<pkg>/ for each package out of one catalog image.

    Returns {package: local directory}. Packages the image does not carry come
    back as empty directories.
    """
    cmd = ['oc', 'image', 'extract', image, f'--filter-by-os={os_filter}',
           '-a', authfile, '--confirm']
    if insecure:
        cmd.append('--insecure=true')
    dirs = {}
    for pkg in packages:
        dest = work_dir / pkg
        dest.mkdir(parents=True, exist_ok=True)  # oc requires it to exist
        dirs[pkg] = dest
        cmd += ['--path', f'/configs/{pkg}/:{dest}']

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FetchError(f"oc image extract {image} failed:\n"
                         + proc.stderr.strip())
    return dirs


def fetch_catalog(image: str, ocp: str, packages: List[str], dest_dir: Path,
                  authfile, refresh: bool = False,
                  os_filter: str = DEFAULT_OS, insecure: bool = False,
                  log=None) -> Dict:
    """
    Make sure dest_dir holds the catalog for one image at one OCP release,
    covering at least the given packages.

    A catalog fetched earlier is reused when it already covers every package.
    Otherwise it is pulled again for its existing packages plus the new ones,
    so a directory shared between clusters only grows; --refresh pulls only
    the given packages. Returns {'image', 'path', 'missing', 'reused'};
    'missing' lists packages the image does not carry.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    data_path = dest_dir / catalog_filename(ocp)
    pkgs_path = dest_dir / packages_filename(ocp)
    ref = retag(image, ocp)

    with open(dest_dir / f".lock-{packages_filename(ocp)}", 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _fetch_locked(ref, packages, data_path, pkgs_path, authfile,
                             refresh, os_filter, insecure, log)


def _fetch_locked(ref, packages, data_path, pkgs_path, authfile, refresh,
                  os_filter, insecure, log) -> Dict:
    known = {}
    if data_path.is_file() and pkgs_path.is_file():
        known = json.loads(pkgs_path.read_text())
    # entries pulled before bundle metadata was recorded do not count
    complete = {p for p, m in known.items() if 'maxOpenShiftVersion' in m}
    if not refresh and set(packages) <= complete:
        return {'image': ref, 'path': str(data_path), 'reused': True,
                'missing': sorted(p for p in packages
                                  if not known[p].get('present'))}
    wanted = packages
    if not refresh:
        packages = sorted(set(packages) | set(known))

    if log:
        log(f"  extracting {len(packages)} package(s) from {ref}")

    work = Path(tempfile.mkdtemp(prefix='olm-extract-'))
    try:
        # credentials are fetched only when something has to be pulled
        auth = authfile() if callable(authfile) else authfile
        dirs = extract_packages(ref, packages, auth, work,
                                os_filter, insecure)
        channels, meta = [], {}
        for pkg in packages:
            chans, default, max_ocp = read_package_dir(dirs[pkg])
            channels.extend(chans)
            meta[pkg] = {'present': bool(chans), 'defaultChannel': default,
                         'maxOpenShiftVersion': max_ocp}
    finally:
        shutil.rmtree(work, ignore_errors=True)

    write_catalog(data_path, channels)
    _write_atomic(pkgs_path, json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return {'image': ref, 'path': str(data_path), 'reused': False,
            'missing': sorted(p for p in wanted if not meta[p]['present'])}


def load_default_channels(catalog_dir: str, ocp: str) -> Dict[str, Optional[str]]:
    """{package: defaultChannel} recorded beside a fetched catalog, if any."""
    path = Path(catalog_dir) / packages_filename(ocp)
    if not path.is_file():
        return {}
    return {p: m.get('defaultChannel')
            for p, m in json.loads(path.read_text()).items()}


def load_bundle_max_ocp(catalog_dir: str, ocp: str) -> Dict[str, Dict[str, str]]:
    """{package: {version: olm.maxOpenShiftVersion}} beside a pulled catalog."""
    path = Path(catalog_dir) / packages_filename(ocp)
    if not path.is_file():
        return {}
    return {p: m.get('maxOpenShiftVersion') or {}
            for p, m in json.loads(path.read_text()).items()}


def fetch_all(groups: List[Dict], ocp_path: List[str], fetch_dir: str,
              authfile: Optional[str], refresh: bool = False,
              os_filter: str = DEFAULT_OS, insecure: bool = False,
              quiet: bool = False, jobs: int = DEFAULT_JOBS) -> Dict[str, str]:
    """
    Fetch every catalog index for every release on the path, several pulls
    at a time, since each one mostly waits on the registry.

    groups: [{'pull_image': str, 'packages': [name, ...]}]
    Returns {pull_image: catalog directory}.
    """
    def log(msg):
        if not quiet:
            print(msg, file=sys.stderr)

    if shutil.which('oc') is None:
        raise FetchError("'oc' is not on PATH; it is needed to pull catalogs")

    tmp = []
    lock = threading.Lock()
    given = authfile

    def credentials():
        """The authfile, reading the pull secret only on the first pull."""
        nonlocal authfile
        with lock:
            if not authfile:
                tmp.append(tempfile.mkdtemp(prefix='olm-auth-'))
                authfile = cluster_authfile(tmp[0])
                log("Registry credentials: cluster pull secret")
            return authfile

    try:
        if given:
            log(f"Registry credentials: {given}")

        dirs = {}
        tasks = []
        for group in groups:
            image = group['pull_image']
            dest = Path(fetch_dir) / image_slug(image)
            dirs[image] = str(dest)
            tasks += [(image, ocp, group['packages'], dest) for ocp in ocp_path]

        def one(task):
            image, ocp, packages, dest = task
            return fetch_catalog(image, ocp, packages, dest, credentials,
                                 refresh, os_filter, insecure, log)

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for res in pool.map(one, tasks):
                if res['reused']:
                    log(f"  reusing {res['path']}")
                for pkg in res['missing']:
                    log(f"  warning: {pkg} is not in {res['image']}")
        return dirs
    finally:
        for d in tmp:
            shutil.rmtree(d, ignore_errors=True)
