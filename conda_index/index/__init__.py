# Copyright (C) 2018 Anaconda, Inc

import bz2
import copy
import functools
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from concurrent.futures import Executor, ProcessPoolExecutor
from datetime import datetime
from itertools import chain
from numbers import Number
from os.path import abspath, basename, dirname, getmtime, getsize, isfile, join
from typing import NamedTuple
from uuid import uuid4

import conda_package_handling.api
import pytz

#  BAD BAD BAD - conda internals
from conda.core.subdir_data import SubdirData
from conda.exports import (
    CondaHTTPError,
    MatchSpec,
    VersionOrder,
    get_index,
    human_bytes,
    url_path,
)
from conda.models.channel import Channel
from conda_build.conda_interface import Resolve, TemporaryDirectory, context
from jinja2 import Environment, PackageLoader
from tqdm import tqdm

from .. import utils
from ..utils import (
    CONDA_PACKAGE_EXTENSION_V1,
    CONDA_PACKAGE_EXTENSION_V2,
    CONDA_PACKAGE_EXTENSIONS,
)
from . import sqlitecache

log = logging.getLogger(__name__)


def logging_config():
    """Called by package extraction subprocesses to re-configure logging."""
    import conda_index.index.logutil

    conda_index.index.logutil.configure()


def ensure_binary(value):
    try:
        return value.encode("utf-8")
    except AttributeError:  # pragma: no cover
        # AttributeError: '<>' object has no attribute 'encode'
        # In this case assume already binary type and do nothing
        return value


# use this for debugging, because ProcessPoolExecutor isn't pdb/ipdb friendly
class DummyExecutor(Executor):
    def map(self, func, *iterables):
        for iterable in iterables:
            for thing in iterable:
                yield func(thing)


try:
    from conda.base.constants import NAMESPACE_PACKAGE_NAMES, NAMESPACES_MAP
except ImportError:
    NAMESPACES_MAP = {  # base package name, namespace
        "python": "python",
        "r": "r",
        "r-base": "r",
        "mro-base": "r",
        "mro-base_impl": "r",
        "erlang": "erlang",
        "java": "java",
        "openjdk": "java",
        "julia": "julia",
        "latex": "latex",
        "lua": "lua",
        "nodejs": "js",
        "perl": "perl",
        "php": "php",
        "ruby": "ruby",
        "m2-base": "m2",
        "msys2-conda-epoch": "m2w64",
    }
    NAMESPACE_PACKAGE_NAMES = frozenset(NAMESPACES_MAP)
    NAMESPACES = frozenset(NAMESPACES_MAP.values())

local_index_timestamp = 0
cached_index = None
local_subdir = ""
local_output_folder = ""
cached_channels = []
channel_data = {}

MAX_THREADS_DEFAULT = (
    os.cpu_count() if (hasattr(os, "cpu_count") and os.cpu_count() > 1) else 1
)
if (
    sys.platform == "win32"
):  # see https://github.com/python/cpython/commit/8ea0fd85bc67438f679491fae29dfe0a3961900a
    MAX_THREADS_DEFAULT = min(48, MAX_THREADS_DEFAULT)
LOCK_TIMEOUT_SECS = 3 * 3600
LOCKFILE_NAME = ".lock"

# TODO: this is to make sure that the index doesn't leak tokens.  It breaks use of private channels, though.
# os.environ['CONDA_ADD_ANACONDA_TOKEN'] = "false"

try:
    # Cython implementation of the toolz package
    # not itertools.groupby
    from cytoolz.itertoolz import groupby
except ImportError:  # pragma: no cover
    from conda._vendor.toolz.itertoolz import groupby  # NOQA

# XXX conda-build calls its version of get_build_index. Appears to combine
# remote and local packages, updating the local index based on mtime. Standalone
# conda-index does not yet use this function.
def get_build_index(
    subdir,
    bldpkgs_dir,
    output_folder=None,
    clear_cache=False,
    omit_defaults=False,
    channel_urls=None,
    debug=False,
    verbose=True,
    **kwargs,
):
    global local_index_timestamp
    global local_subdir
    global local_output_folder
    global cached_index
    global cached_channels
    global channel_data
    mtime = 0

    channel_urls = list(utils.ensure_list(channel_urls))

    if not output_folder:
        output_folder = dirname(bldpkgs_dir)

    # check file modification time - this is the age of our local index.
    index_file = os.path.join(output_folder, subdir, "repodata.json")
    if os.path.isfile(index_file):
        mtime = os.path.getmtime(index_file)

    if (
        clear_cache
        or not os.path.isfile(index_file)
        or local_subdir != subdir
        or local_output_folder != output_folder
        or mtime > local_index_timestamp
        or cached_channels != channel_urls
    ):

        # priority: (local as either croot or output_folder IF NOT EXPLICITLY IN CHANNEL ARGS),
        #     then channels passed as args (if local in this, it remains in same order),
        #     then channels from condarc.
        urls = list(channel_urls)

        logging_context = utils.LoggingContext()

        with logging_context():
            # this is where we add the "local" channel.  It's a little smarter than conda, because
            #     conda does not know about our output_folder when it is not the default setting.
            if os.path.isdir(output_folder):
                local_path = url_path(output_folder)
                # replace local with the appropriate real channel.  Order is maintained.
                urls = [url if url != "local" else local_path for url in urls]
                if local_path not in urls:
                    urls.insert(0, local_path)
            _ensure_valid_channel(output_folder, subdir)
            update_index(output_folder, verbose=debug)

            # replace noarch with native subdir - this ends up building an index with both the
            #      native content and the noarch content.

            if subdir == "noarch":
                subdir = context.subdir
            try:
                cached_index = get_index(
                    channel_urls=urls,
                    prepend=not omit_defaults,
                    use_local=False,
                    use_cache=context.offline,
                    platform=subdir,
                )
            # HACK: defaults does not have the many subfolders we support.  Omit it and
            #          try again.
            except CondaHTTPError:
                if "defaults" in urls:
                    urls.remove("defaults")
                cached_index = get_index(
                    channel_urls=urls,
                    prepend=omit_defaults,
                    use_local=False,
                    use_cache=context.offline,
                    platform=subdir,
                )

            expanded_channels = {rec.channel for rec in cached_index.values()}

            superchannel = {}
            # we need channeldata.json too, as it is a more reliable source of run_exports data
            for channel in expanded_channels:
                if channel.scheme == "file":
                    location = channel.location
                    if utils.on_win:
                        location = location.lstrip("/")
                    elif not os.path.isabs(channel.location) and os.path.exists(
                        os.path.join(os.path.sep, channel.location)
                    ):
                        location = os.path.join(os.path.sep, channel.location)
                    channeldata_file = os.path.join(
                        location, channel.name, "channeldata.json"
                    )
                    retry = 0
                    max_retries = 1
                    if os.path.isfile(channeldata_file):
                        while retry < max_retries:
                            try:
                                with open(channeldata_file, "r+") as f:
                                    channel_data[channel.name] = json.load(f)
                                break
                            except (OSError, json.JSONDecodeError):
                                time.sleep(0.2)
                                retry += 1
                else:
                    # download channeldata.json for url
                    if not context.offline:
                        try:
                            channel_data[channel.name] = utils.download_channeldata(
                                channel.base_url + "/channeldata.json"
                            )
                        except CondaHTTPError:
                            continue
                # collapse defaults metachannel back into one superchannel, merging channeldata
                if channel.base_url in context.default_channels and channel_data.get(
                    channel.name
                ):
                    packages = superchannel.get("packages", {})
                    packages.update(channel_data[channel.name])
                    superchannel["packages"] = packages
            channel_data["defaults"] = superchannel
        local_index_timestamp = os.path.getmtime(index_file)
        local_subdir = subdir
        local_output_folder = output_folder
        cached_channels = channel_urls
    return cached_index, local_index_timestamp, channel_data


def _ensure_valid_channel(local_folder, subdir):
    for folder in {subdir, "noarch"}:
        path = os.path.join(local_folder, folder)
        if not os.path.isdir(path):
            os.makedirs(path)


class FileInfo(NamedTuple):
    """
    Filename and a bit of stat information.
    """

    fn: str
    st_mtime: Number
    st_size: Number


def update_index(
    dir_path,
    check_md5=False,
    channel_name=None,
    patch_generator=None,
    threads=MAX_THREADS_DEFAULT,
    verbose=False,
    progress=False,
    hotfix_source_repo=None,
    subdirs=None,
    warn=True,
    current_index_versions=None,
    debug=False,
    index_file=None,
):
    """
    If dir_path contains a directory named 'noarch', the path tree therein is treated
    as though it's a full channel, with a level of subdirs, each subdir having an update
    to repodata.json.  The full channel will also have a channeldata.json file.

    If dir_path does not contain a directory named 'noarch', but instead contains at least
    one '*.tar.bz2' file, the directory is assumed to be a standard subdir, and only repodata.json
    information will be updated.

    """
    _, dirname = os.path.split(dir_path)
    if dirname in utils.DEFAULT_SUBDIRS:
        if warn:
            log.warn(
                "The update_index function has changed to index all subdirs at once.  You're pointing it at a single subdir.  "
                "Please update your code to point it at the channel root, rather than a subdir. "
                "Use -s=<subdir> to update a single subdir."
            )
        raise SystemExit()

    return ChannelIndex(
        dir_path,
        channel_name,
        subdirs=subdirs,
        threads=threads,
        deep_integrity_check=check_md5,
        debug=debug,
    ).index(
        patch_generator=patch_generator,
        verbose=verbose,
        progress=progress,
        current_index_versions=current_index_versions,
    )


def _make_seconds(timestamp):
    timestamp = int(timestamp)
    if timestamp > 253402300799:  # 9999-12-31
        timestamp //= (
            1000  # convert milliseconds to seconds; see conda/conda-build#1988
        )
    return timestamp


# ==========================================================================


REPODATA_VERSION = 1
CHANNELDATA_VERSION = 1
REPODATA_JSON_FN = "repodata.json"
REPODATA_FROM_PKGS_JSON_FN = "repodata_from_packages.json"
CHANNELDATA_FIELDS = (
    "description",
    "dev_url",
    "doc_url",
    "doc_source_url",
    "home",
    "license",
    "reference_package",
    "source_url",
    "source_git_url",
    "source_git_tag",
    "source_git_rev",
    "summary",
    "version",
    "subdirs",
    "icon_url",
    "icon_hash",  # "md5:abc123:12"
    "run_exports",
    "binary_prefix",
    "text_prefix",
    "activate.d",
    "deactivate.d",
    "pre_link",
    "post_link",
    "pre_unlink",
    "tags",
    "identifiers",
    "keywords",
    "recipe_origin",
    "commits",
)


def _apply_instructions(subdir, repodata, instructions):
    repodata.setdefault("removed", [])
    utils.merge_or_update_dict(
        repodata.get("packages", {}),
        instructions.get("packages", {}),
        merge=False,
        add_missing_keys=False,
    )
    # we could have totally separate instructions for .conda than .tar.bz2, but it's easier if we assume
    #    that a similarly-named .tar.bz2 file is the same content as .conda, and shares fixes
    new_pkg_fixes = {
        k.replace(CONDA_PACKAGE_EXTENSION_V1, CONDA_PACKAGE_EXTENSION_V2): v
        for k, v in instructions.get("packages", {}).items()
    }

    utils.merge_or_update_dict(
        repodata.get("packages.conda", {}),
        new_pkg_fixes,
        merge=False,
        add_missing_keys=False,
    )
    utils.merge_or_update_dict(
        repodata.get("packages.conda", {}),
        instructions.get("packages.conda", {}),
        merge=False,
        add_missing_keys=False,
    )

    for fn in instructions.get("revoke", ()):
        for key in ("packages", "packages.conda"):
            if fn.endswith(CONDA_PACKAGE_EXTENSION_V1) and key == "packages.conda":
                fn = fn.replace(CONDA_PACKAGE_EXTENSION_V1, CONDA_PACKAGE_EXTENSION_V2)
            if fn in repodata[key]:
                repodata[key][fn]["revoked"] = True
                repodata[key][fn]["depends"].append("package_has_been_revoked")

    for fn in instructions.get("remove", ()):
        for key in ("packages", "packages.conda"):
            if fn.endswith(CONDA_PACKAGE_EXTENSION_V1) and key == "packages.conda":
                fn = fn.replace(CONDA_PACKAGE_EXTENSION_V1, CONDA_PACKAGE_EXTENSION_V2)
            popped = repodata[key].pop(fn, None)
            if popped:
                repodata["removed"].append(fn)
    repodata["removed"].sort()

    return repodata


def _get_jinja2_environment():
    def _filter_strftime(dt, dt_format):
        if isinstance(dt, Number):
            if dt > 253402300799:  # 9999-12-31
                dt //= 1000  # convert milliseconds to seconds; see #1988
            dt = datetime.utcfromtimestamp(dt).replace(tzinfo=pytz.timezone("UTC"))
        return dt.strftime(dt_format)

    def _filter_add_href(text, link, **kwargs):
        if link:
            kwargs_list = [f'href="{link}"']
            kwargs_list.append(f'alt="{text}"')
            kwargs_list += [f'{k}="{v}"' for k, v in kwargs.items()]
            return "<a {}>{}</a>".format(" ".join(kwargs_list), text)
        else:
            return text

    environment = Environment(
        loader=PackageLoader("conda_build", "templates"),
    )
    environment.filters["human_bytes"] = human_bytes
    environment.filters["strftime"] = _filter_strftime
    environment.filters["add_href"] = _filter_add_href
    environment.trim_blocks = True
    environment.lstrip_blocks = True

    return environment


def _maybe_write(path, content, write_newline_end=False, content_is_binary=False):
    # Create the temp file next "path" so that we can use an atomic move, see
    # https://github.com/conda/conda-build/issues/3833
    temp_path = f"{path}.{uuid4()}"

    if not content_is_binary:
        content = ensure_binary(content)
    with open(temp_path, "wb") as fh:
        fh.write(content)
        if write_newline_end:
            fh.write(b"\n")
    if isfile(path):
        if utils.file_contents_match(temp_path, path):
            # No need to change mtimes. The contents already match.
            os.unlink(temp_path)
            return False
    # log.info("writing %s", path)
    utils.move_with_fallback(temp_path, path)
    return True


def _make_subdir_index_html(channel_name, subdir, repodata_packages, extra_paths):
    environment = _get_jinja2_environment()
    template = environment.get_template("subdir-index.html.j2")
    rendered_html = template.render(
        title="{}/{}".format(channel_name or "", subdir),
        packages=repodata_packages,
        current_time=datetime.utcnow().replace(tzinfo=pytz.timezone("UTC")),
        extra_paths=extra_paths,
    )
    return rendered_html


def _make_channeldata_index_html(channel_name, channeldata):
    environment = _get_jinja2_environment()
    template = environment.get_template("channeldata-index.html.j2")
    rendered_html = template.render(
        title=channel_name,
        packages=channeldata["packages"],
        subdirs=channeldata["subdirs"],
        current_time=datetime.utcnow().replace(tzinfo=pytz.timezone("UTC")),
    )
    return rendered_html


def _get_resolve_object(subdir, file_path=None, precs=None, repodata=None):
    packages = {}
    conda_packages = {}
    if file_path:
        with open(file_path) as fi:
            packages = json.load(fi)
            recs = json.load(fi)
            for k, v in recs.items():
                if k.endswith(CONDA_PACKAGE_EXTENSION_V1):
                    packages[k] = v
                elif k.endswith(CONDA_PACKAGE_EXTENSION_V2):
                    conda_packages[k] = v
    if not repodata:
        repodata = {
            "info": {
                "subdir": subdir,
                "arch": context.arch_name,
                "platform": context.platform,
            },
            "packages": packages,
            "packages.conda": conda_packages,
        }

    channel = Channel("https://conda.anaconda.org/dummy-channel/%s" % subdir)
    sd = SubdirData(channel)
    sd._process_raw_repodata_str(json.dumps(repodata))
    sd._loaded = True
    SubdirData._cache_[channel.url(with_credentials=True)] = sd

    index = {prec: prec for prec in precs or sd._package_records}
    r = Resolve(index, channels=(channel,))
    return r


def _add_missing_deps(new_r, original_r):
    """For each package in new_r, if any deps are not satisfiable, backfill them from original_r."""

    expanded_groups = copy.deepcopy(new_r.groups)
    seen_specs = set()
    for g_name, g_recs in new_r.groups.items():
        for g_rec in g_recs:
            for dep_spec in g_rec.depends:
                if dep_spec in seen_specs:
                    continue
                ms = MatchSpec(dep_spec)
                if not new_r.find_matches(ms):
                    matches = original_r.find_matches(ms)
                    if matches:
                        version = matches[0].version
                        expanded_groups[ms.name] = set(
                            expanded_groups.get(ms.name, [])
                        ) | set(
                            original_r.find_matches(MatchSpec(f"{ms.name}={version}"))
                        )
                seen_specs.add(dep_spec)
    return [pkg for group in expanded_groups.values() for pkg in group]


def _add_prev_ver_for_features(new_r, orig_r):
    expanded_groups = copy.deepcopy(new_r.groups)
    for g_name in new_r.groups:
        if not any(m.track_features or m.features for m in new_r.groups[g_name]):
            # no features so skip
            continue

        # versions are sorted here so this is the latest
        latest_version = VersionOrder(str(new_r.groups[g_name][0].version))
        if g_name in orig_r.groups:
            # now we iterate through the list to find the next to latest
            # without a feature
            keep_m = None
            for i in range(len(orig_r.groups[g_name])):
                _m = orig_r.groups[g_name][i]
                if VersionOrder(str(_m.version)) <= latest_version and not (
                    _m.track_features or _m.features
                ):
                    keep_m = _m
                    break
            if keep_m is not None:
                expanded_groups[g_name] = {keep_m} | set(
                    expanded_groups.get(g_name, [])
                )

    return [pkg for group in expanded_groups.values() for pkg in group]


def _shard_newest_packages(subdir, r, pins=None):
    """Captures only the newest versions of software in the resolve object.

    For things where more than one version is supported simultaneously (like Python),
    pass pins as a dictionary, with the key being the package name, and the value being
    a list of supported versions.  For example:

    {'python': ["2.7", "3.6"]}
    """
    groups = {}
    pins = pins or {}
    for g_name, g_recs in r.groups.items():
        # always do the latest implicitly
        version = r.groups[g_name][0].version
        matches = set(r.find_matches(MatchSpec(f"{g_name}={version}")))
        if g_name in pins:
            for pin_value in pins[g_name]:
                version = r.find_matches(MatchSpec(f"{g_name}={pin_value}"))[0].version
                matches.update(r.find_matches(MatchSpec(f"{g_name}={version}")))
        groups[g_name] = matches

    # add the deps of the stuff in the index
    new_r = _get_resolve_object(
        subdir, precs=[pkg for group in groups.values() for pkg in group]
    )
    new_r = _get_resolve_object(subdir, precs=_add_missing_deps(new_r, r))

    # now for any pkg with features, add at least one previous version
    # also return
    return set(_add_prev_ver_for_features(new_r, r))


def _build_current_repodata(subdir, repodata, pins):
    r = _get_resolve_object(subdir, repodata=repodata)
    keep_pkgs = _shard_newest_packages(subdir, r, pins)
    new_repodata = {
        k: repodata[k] for k in set(repodata.keys()) - {"packages", "packages.conda"}
    }
    packages = {}
    conda_packages = {}
    for keep_pkg in keep_pkgs:
        if keep_pkg.fn.endswith(CONDA_PACKAGE_EXTENSION_V2):
            conda_packages[keep_pkg.fn] = repodata["packages.conda"][keep_pkg.fn]
            # in order to prevent package churn we consider the md5 for the .tar.bz2 that matches the .conda file
            #    This holds when .conda files contain the same files as .tar.bz2, which is an assumption we'll make
            #    until it becomes more prevalent that people provide only .conda files and just skip .tar.bz2
            counterpart = keep_pkg.fn.replace(
                CONDA_PACKAGE_EXTENSION_V2, CONDA_PACKAGE_EXTENSION_V1
            )
            conda_packages[keep_pkg.fn]["legacy_bz2_md5"] = (
                repodata["packages"].get(counterpart, {}).get("md5")
            )
        elif keep_pkg.fn.endswith(CONDA_PACKAGE_EXTENSION_V1):
            packages[keep_pkg.fn] = repodata["packages"][keep_pkg.fn]
    new_repodata["packages"] = packages
    new_repodata["packages.conda"] = conda_packages
    return new_repodata


class ChannelIndex:
    def __init__(
        self,
        channel_root,
        channel_name,
        subdirs=None,
        threads=MAX_THREADS_DEFAULT,
        deep_integrity_check=False,
        debug=False,
    ):
        self.channel_root = abspath(channel_root)
        self.channel_name = channel_name or basename(channel_root.rstrip("/"))
        self._subdirs = subdirs
        self.thread_executor = (
            DummyExecutor()
            if (debug or sys.version_info.major == 2 or threads == 1)
            else ProcessPoolExecutor(threads, initializer=logging_config)
        )
        self.debug = debug
        self.deep_integrity_check = deep_integrity_check

    def index(
        self,
        patch_generator,
        verbose=False,
        progress=False,
        current_index_versions=None,
    ):
        if verbose:
            level = logging.DEBUG
        else:
            level = logging.ERROR

        logging_context = utils.LoggingContext(level, loggers=[__name__])

        with logging_context:
            if not self._subdirs:
                detected_subdirs = {
                    subdir.name
                    for subdir in os.scandir(self.channel_root)
                    if subdir.name in utils.DEFAULT_SUBDIRS and subdir.is_dir()
                }
                log.debug("found subdirs %s" % detected_subdirs)
                self.subdirs = subdirs = sorted(detected_subdirs | {"noarch"})
            else:
                self.subdirs = subdirs = sorted(
                    set(self._subdirs)
                )  # XXX noarch removed for testing | {"noarch"})

            # Step 1. Lock local channel.
            with utils.try_acquire_locks(
                [utils.get_lock(self.channel_root)], timeout=900
            ):
                # keeep channeldata in memory, update with each subdir
                channel_data = {}
                channeldata_file = os.path.join(self.channel_root, "channeldata.json")
                if os.path.isfile(channeldata_file):
                    with open(channeldata_file) as f:
                        channel_data = json.load(f)
                # Step 2. Collect repodata from packages, save to pkg_repodata.json file
                t = tqdm(subdirs, disable=(verbose or not progress), leave=False)
                for subdir in t:
                    t.set_description("Subdir: %s" % subdir)
                    t.update()
                    with tqdm(
                        total=8, disable=(verbose or not progress), leave=False
                    ) as t2:
                        t2.set_description("Gathering repodata")
                        t2.update()
                        log.debug("gather repodata")
                        _ensure_valid_channel(self.channel_root, subdir)
                        repodata_from_packages = self.index_subdir(
                            subdir, verbose=verbose, progress=progress
                        )

                        t2.set_description("Writing pre-patch repodata")
                        t2.update()
                        log.debug("write repodata")
                        self._write_repodata(
                            subdir,
                            repodata_from_packages,
                            REPODATA_FROM_PKGS_JSON_FN,
                        )

                        # Step 3. Apply patch instructions.
                        t2.set_description("Applying patch instructions")
                        t2.update()
                        log.debug("apply patch instructions")
                        patched_repodata, _ = self._patch_repodata(
                            subdir, repodata_from_packages, patch_generator
                        )

                        # Step 4. Save patched and augmented repodata.
                        # If the contents of repodata have changed, write a new repodata.json file.
                        # Also create associated index.html.

                        t2.set_description("Writing patched repodata")
                        t2.update()
                        log.debug("write patched repodata")
                        self._write_repodata(subdir, patched_repodata, REPODATA_JSON_FN)
                        t2.set_description("Building current_repodata subset")
                        t2.update()
                        log.debug("build current_repodata")
                        current_repodata = _build_current_repodata(
                            subdir, patched_repodata, pins=current_index_versions
                        )
                        t2.set_description("Writing current_repodata subset")
                        t2.update()
                        log.debug("write current_repodata")
                        self._write_repodata(
                            subdir,
                            current_repodata,
                            json_filename="current_repodata.json",
                        )

                        t2.set_description("Writing subdir index HTML")
                        t2.update()
                        log.debug("write subdir index.html")
                        self._write_subdir_index_html(subdir, patched_repodata)

                        t2.set_description("Updating channeldata")
                        t2.update()
                        log.debug("update channeldata")
                        self._update_channeldata(channel_data, patched_repodata, subdir)

                        log.debug("finish %s", subdir)

                # Step 7. Create and write channeldata.
                self._write_channeldata_index_html(channel_data)
                log.debug("write channeldata")
                self._write_channeldata(channel_data)

    def index_subdir(self, subdir, verbose=False, progress=False):
        return self.index_subdir_unidirectional(
            subdir, verbose=verbose, progress=progress
        )

    def index_subdir_sql(self, subdir, verbose=False, progress=False):
        subdir_path = join(self.channel_root, subdir)

        cache = sqlitecache.CondaIndexCache(
            channel_root=self.channel_root, channel=self.channel_name, subdir=subdir
        )
        if cache.cache_is_brand_new:
            # guaranteed to be only thread doing this?
            cache.convert()

        path_like = cache.database_path_like

        repodata_json_path = join(subdir_path, REPODATA_FROM_PKGS_JSON_FN)

        if verbose:
            log.info("Building repodata for %s" % subdir_path)

        # gather conda package filenames in subdir
        # we'll process these first, because reading their metadata is much faster
        # XXX eliminate all listdir. that and stat calls are significant.
        log.debug("listdir")
        fns_in_subdir = {
            fn for fn in os.listdir(subdir_path) if fn.endswith((".conda", ".tar.bz2"))
        }

        # load current/old repodata XXX with sql cache, where 'select from
        # index_json' is incredibly fast, this code might be easier to reason
        # about without carrying over old repodata.packages
        try:
            with open(repodata_json_path) as fh:
                old_repodata = json.load(fh) or {}
        except (OSError, json.JSONDecodeError):
            # log.info("no repodata found at %s", repodata_json_path)
            old_repodata = {}

        # could go in a temporary table
        old_repodata_packages = old_repodata.get("packages", {})
        old_repodata_conda_packages = old_repodata.get("packages.conda", {})
        old_repodata_fns = set(old_repodata_packages) | set(old_repodata_conda_packages)

        # put filesystem 'ground truth' into stat table
        # will we eventually stat everything on fs, or can we shortcut for new?
        def listdir_stat():
            for fn in fns_in_subdir:
                abs_fn = os.path.join(subdir_path, fn)
                stat = os.stat(abs_fn)
                yield {
                    "path": cache.database_path(fn),
                    "mtime": int(stat.st_mtime),
                    "size": stat.st_size,
                }

        log.debug("save fs state")
        with cache.db:
            cache.db.execute(
                "DELETE FROM stat WHERE stage='fs' AND path like :path_like",
                {"path_like": path_like},
            )
            cache.db.executemany(
                """
            INSERT INTO STAT (stage, path, mtime, size)
            VALUES ('fs', :path, :mtime, :size)
            """,
                listdir_stat(),
            )

        log.debug("load repodata into stat cache")
        # put repodata into stat table. no mtime.
        with cache.db:
            cache.db.execute(
                "DELETE FROM stat WHERE stage='repodata' AND path like :path_like",
                {"path_like": path_like},
            )
            cache.db.executemany(
                """
            INSERT INTO stat (stage, path)
            VALUES ('repodata', :path)
            """,
                ({"path": cache.database_path(fn)} for fn in old_repodata_fns),
            )

        log.debug("load stat cache")
        stat_cache = cache.stat_cache()

        stat_cache_original = stat_cache.copy()

        # in repodata but not on filesystem
        # remove_set = old_repodata_fns - fns_in_subdir
        log.debug("calculate remove set")
        remove_set = {
            cache.plain_path(row["path"])
            for row in cache.db.execute(
                """SELECT path FROM stat WHERE path LIKE :path_like AND stage = 'repodata'
            AND path NOT IN (SELECT path FROM stat WHERE stage = 'fs')""",
                {"path_like": path_like},
            )
        }

        ignore_set = set(old_repodata.get("removed", []))
        try:
            # calculate all the paths and figure out what we're going to do with
            # them

            # add_set: filenames that aren't in the current/old repodata, but
            # exist in the subdir
            log.debug("calculate add set")
            add_set = {
                row[0].rpartition("/")[-1]
                for row in cache.db.execute(
                    """SELECT path FROM stat WHERE path LIKE :path_like AND stage = 'fs'
                AND path NOT IN (SELECT path FROM stat WHERE stage = 'repodata')""",
                    {"path_like": path_like},
                )
            }

            add_set -= ignore_set

            # update_set: Filenames that are in both old repodata and new
            # repodata, and whose contents have changed based on file size or
            # mtime.

            # XXX is the 'WITH' statement the best way to make LEFT JOIN return
            # null rows from cached?
            log.debug("calculate update set")
            update_set_query = cache.db.execute(
                """
                WITH
                cached AS
                    ( SELECT path, mtime FROM stat WHERE stage = 'indexed' ),
                fs AS
                    ( SELECT path, mtime FROM stat WHERE stage = 'fs' )

                SELECT fs.path, fs.mtime from fs LEFT JOIN cached USING (path)

                WHERE fs.path LIKE :path_like AND
                    (floor(fs.mtime) != floor(cached.mtime) OR cached.path IS NULL)
                """,
                {"path_like": path_like},
            )

            update_set = {row["path"].rpartition("/")[-1] for row in update_set_query}

            log.debug("calculate unchanged set")
            # unchanged_set: packages in old repodata whose information can carry straight
            #     across to new repodata
            unchanged_set = old_repodata_fns - update_set - remove_set - ignore_set

            assert isinstance(unchanged_set, set)  # fast `in` query

            log.debug("clean removed")
            # clean up removed files
            removed_set = old_repodata_fns - fns_in_subdir
            for fn in removed_set:
                if fn in stat_cache:
                    del stat_cache[fn]

            log.debug("new repodata packages")
            new_repodata_packages = {
                k: v
                for k, v in old_repodata.get("packages", {}).items()
                if k in unchanged_set
            }
            log.debug("new repodata conda packages")
            new_repodata_conda_packages = {
                k: v
                for k, v in old_repodata.get("packages.conda", {}).items()
                if k in unchanged_set
            }

            log.debug("for k in unchanged set")
            # was sorted before. necessary?
            for k in sorted(unchanged_set):
                if not (k in new_repodata_packages or k in new_repodata_conda_packages):
                    # XXX select more than one index at a time
                    fn, rec = cache.load_index_from_cache(fn)
                    # this is how we pass an exception through.  When fn == rec, there's been a problem,
                    #    and we need to reload this file
                    if fn == rec:
                        update_set.add(fn)
                    else:
                        if fn.endswith(CONDA_PACKAGE_EXTENSION_V1):
                            new_repodata_packages[fn] = rec
                        else:
                            new_repodata_conda_packages[fn] = rec

            # Invalidate cached files for update_set.
            # Extract and cache update_set and add_set, then add to new_repodata_packages.
            # This is also where we update the contents of the stat_cache for successfully
            #   extracted packages.
            # Sorting here prioritizes .conda files ('c') over .tar.bz2 files ('b')
            hash_extract_set = tuple(chain(add_set, update_set))

            log.debug("extraction")
            extract_func = functools.partial(
                cache.extract_to_cache, self.channel_root, subdir
            )
            # split up the set by .conda packages first, then .tar.bz2.  This avoids race conditions
            #    with execution in parallel that would end up in the same place.
            for conda_format in tqdm(
                CONDA_PACKAGE_EXTENSIONS,
                desc="File format",
                disable=(verbose or not progress),
                leave=False,
            ):
                for fn, mtime, size, index_json in tqdm(
                    self.thread_executor.map(  # tries to pickle cache.db = sqlite connection
                        extract_func,
                        (fn for fn in hash_extract_set if fn.endswith(conda_format)),
                    ),
                    desc="hash & extract packages for %s" % subdir,
                    disable=(verbose or not progress),
                    leave=False,
                ):

                    # fn can be None if the file was corrupt or no longer there
                    if fn and mtime:
                        stat_cache[fn] = {"mtime": int(mtime), "size": size}
                        if index_json:
                            if fn.endswith(CONDA_PACKAGE_EXTENSION_V2):
                                new_repodata_conda_packages[fn] = index_json
                            else:
                                new_repodata_packages[fn] = index_json
                        else:
                            log.error(
                                "Package at %s did not contain valid index.json data.  Please"
                                " check the file and remove/redownload if necessary to obtain "
                                "a valid package." % os.path.join(subdir_path, fn)
                            )

            new_repodata = {
                "packages": new_repodata_packages,
                "packages.conda": new_repodata_conda_packages,
                "info": {
                    "subdir": subdir,
                },
                "repodata_version": REPODATA_VERSION,
                "removed": sorted(list(ignore_set)),
            }
        finally:
            if stat_cache != stat_cache_original:
                cache.save_stat_cache(stat_cache)
        return new_repodata

    def save_fs_state(self, cache: sqlitecache.CondaIndexCache, subdir_path):
        """
        stat all files in subdir_path to compare against cached repodata.
        """
        path_like = cache.database_path_like

        # gather conda package filenames in subdir
        log.debug("listdir")
        fns_in_subdir = {
            fn for fn in os.listdir(subdir_path) if fn.endswith((".conda", ".tar.bz2"))
        }

        # put filesystem 'ground truth' into stat table
        # will we eventually stat everything on fs, or can we shortcut for new?
        def listdir_stat():
            for fn in fns_in_subdir:
                abs_fn = os.path.join(subdir_path, fn)
                stat = os.stat(abs_fn)
                yield {
                    "path": cache.database_path(fn),
                    "mtime": int(stat.st_mtime),
                    "size": stat.st_size,
                }

        log.debug("save fs state")
        with cache.db:
            cache.db.execute(
                "DELETE FROM stat WHERE stage='fs' AND path like :path_like",
                {"path_like": path_like},
            )
            cache.db.executemany(
                """
            INSERT INTO STAT (stage, path, mtime, size)
            VALUES ('fs', :path, :mtime, :size)
            """,
                listdir_stat(),
            )

    def changed_packages(self, cache: sqlitecache.CondaIndexCache):
        """
        Compare 'fs' to 'indexed' state.

        Return packages in 'fs' that are changed or missing compared to 'indexed'.
        """
        return cache.db.execute(
            """
            WITH
            fs AS
                ( SELECT path, mtime, size FROM stat WHERE stage = 'fs' ),
            cached AS
                ( SELECT path, mtime FROM stat WHERE stage = 'indexed' )

            SELECT fs.path, fs.mtime, fs.size from fs LEFT JOIN cached USING (path)

            WHERE fs.path LIKE :path_like AND
                (fs.mtime != cached.mtime OR cached.path IS NULL)
        """,
            {"path_like": cache.database_path_like},
        )  # XXX compare checksums if available, and prefer to mtimes

    def index_subdir_unidirectional(self, subdir, verbose=False, progress=False):
        """
        Without reading old repodata.
        """
        subdir_path = join(self.channel_root, subdir)

        cache = sqlitecache.CondaIndexCache(
            channel_root=self.channel_root, channel=self.channel_name, subdir=subdir
        )
        if cache.cache_is_brand_new:
            # guaranteed to be only thread doing this?
            cache.convert()

        if verbose:
            log.info("Building repodata for %s" % subdir_path)

        # exactly these packages (unless they are un-indexable) will be in the
        # output repodata
        self.save_fs_state(cache, subdir_path)

        log.debug("extraction")

        # list so tqdm can show progress
        extract = [
            FileInfo(
                fn=cache.plain_path(row["path"]),
                st_mtime=row["mtime"],
                st_size=row["size"],
            )
            for row in self.changed_packages(cache)
        ]

        # now updates own stat cache
        extract_func = functools.partial(
            cache.extract_to_cache_2, self.channel_root, subdir
        )

        for fn, mtime, _size, index_json in tqdm(
            self.thread_executor.map(
                extract_func,
                extract,
            ),
            desc="hash & extract packages for %s" % subdir,
            disable=(verbose or not progress),
            leave=False,
        ):
            # fn can be None if the file was corrupt or no longer there
            if fn and mtime:
                if index_json:
                    pass  # correctly indexed a package! will fetch below
                else:
                    log.error(
                        "Package at %s did not contain valid index.json data.  Please"
                        " check the file and remove/redownload if necessary to obtain "
                        "a valid package." % os.path.join(subdir_path, fn)
                    )

        new_repodata_packages = {}
        new_repodata_conda_packages = {}

        # XXX delete files in cache but not in save_fs_state / or modified files
        # - before or after reload files step

        # load cached packages we just saw on the filesystem
        # (cache may also contain files that are no longer on the filesystem)
        for row in cache.db.execute(
            """
            SELECT path, index_json FROM stat JOIN index_json USING (path)
            WHERE stat.stage = ?
            ORDER BY path
        """,
            ("fs",),
        ):
            path, index_json = row
            # (convert path to base filename)
            index_json = json.loads(index_json)
            if path.endswith(CONDA_PACKAGE_EXTENSION_V1):
                new_repodata_packages[path] = index_json
            elif path.endswith(CONDA_PACKAGE_EXTENSION_V2):
                new_repodata_conda_packages[path] = index_json
            else:
                log.warn("%s doesn't look like a conda package", path)

        new_repodata = {
            "packages": new_repodata_packages,
            "packages.conda": new_repodata_conda_packages,
            "info": {
                "subdir": subdir,
            },
            "repodata_version": REPODATA_VERSION,
            "removed": [],  # can be added by patch/hotfix process
        }

        return new_repodata

    def _write_repodata(self, subdir, repodata, json_filename):
        """
        Write repodata to :json_filename, but only if changed.
        """
        repodata_json_path = join(self.channel_root, subdir, json_filename)
        new_repodata_binary = json.dumps(
            repodata,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        write_result = _maybe_write(
            repodata_json_path, new_repodata_binary, write_newline_end=True
        )
        if write_result:
            # XXX write bz2 quickly or not at all, delete old one
            repodata_bz2_path = repodata_json_path + ".bz2"
            bz2_content = bz2.compress(new_repodata_binary)
            _maybe_write(repodata_bz2_path, bz2_content, content_is_binary=True)
        return write_result

    def _write_subdir_index_html(self, subdir, repodata):
        repodata_packages = repodata["packages"]
        subdir_path = join(self.channel_root, subdir)

        def _add_extra_path(extra_paths, path):
            if isfile(join(self.channel_root, path)):
                md5sum, sha256sum = utils.checksums(path, ("md5", "sha256"))
                extra_paths[basename(path)] = {
                    "size": getsize(path),
                    "timestamp": int(getmtime(path)),
                    "sha256": sha256sum,
                    "md5": md5sum,
                }

        extra_paths = OrderedDict()
        _add_extra_path(extra_paths, join(subdir_path, REPODATA_JSON_FN))
        _add_extra_path(extra_paths, join(subdir_path, REPODATA_JSON_FN + ".bz2"))
        _add_extra_path(extra_paths, join(subdir_path, REPODATA_FROM_PKGS_JSON_FN))
        _add_extra_path(
            extra_paths, join(subdir_path, REPODATA_FROM_PKGS_JSON_FN + ".bz2")
        )
        # _add_extra_path(extra_paths, join(subdir_path, "repodata2.json"))
        _add_extra_path(extra_paths, join(subdir_path, "patch_instructions.json"))
        rendered_html = _make_subdir_index_html(
            self.channel_name, subdir, repodata_packages, extra_paths
        )
        index_path = join(subdir_path, "index.html")
        return _maybe_write(index_path, rendered_html)

    def _write_channeldata_index_html(self, channeldata):
        rendered_html = _make_channeldata_index_html(self.channel_name, channeldata)
        index_path = join(self.channel_root, "index.html")
        _maybe_write(index_path, rendered_html)

    def _update_channeldata(self, channel_data, repodata, subdir):
        # XXX pass stat_cache into _update_channeldata or otherwise save results of
        # recent stat calls

        cache = sqlitecache.CondaIndexCache(
            channel_root=self.channel_root, channel=self.channel_name, subdir=subdir
        )

        legacy_packages = repodata["packages"]
        conda_packages = repodata["packages.conda"]

        use_these_legacy_keys = set(legacy_packages.keys()) - {
            k[:-6] + CONDA_PACKAGE_EXTENSION_V1 for k in conda_packages.keys()
        }
        all_repodata_packages = conda_packages.copy()
        all_repodata_packages.update(
            {k: legacy_packages[k] for k in use_these_legacy_keys}
        )
        package_data = channel_data.get("packages", {})

        def _append_group(groups, candidate):
            pkg_dict = candidate[1]
            pkg_name = pkg_dict["name"]

            run_exports = package_data.get(pkg_name, {}).get("run_exports", {})
            if (
                pkg_name not in package_data
                or subdir not in package_data.get(pkg_name, {}).get("subdirs", [])
                or package_data.get(pkg_name, {}).get("timestamp", 0)
                < _make_seconds(pkg_dict.get("timestamp", 0))
                or run_exports
                and pkg_dict["version"] not in run_exports
            ):
                groups.append(candidate)

        groups = []
        package_groups = groupby(lambda x: x[1]["name"], all_repodata_packages.items())
        for groupname, group in package_groups.items():
            if groupname not in package_data or package_data[groupname].get(
                "run_exports"
            ):
                # pay special attention to groups that have run_exports - we need to process each version
                # group by version; take newest per version group.  We handle groups that are not
                #    in the index t all yet similarly, because we can't check if they have any run_exports
                for vgroup in groupby(lambda x: x[1]["version"], group).values():
                    candidate = next(
                        iter(
                            sorted(
                                vgroup,
                                key=lambda x: x[1].get("timestamp", 0),
                                reverse=True,
                            )
                        )
                    )
                    _append_group(groups, candidate)
            else:
                # take newest per group
                candidate = next(
                    iter(
                        sorted(
                            group, key=lambda x: x[1].get("timestamp", 0), reverse=True
                        )
                    )
                )
                _append_group(groups, candidate)

        def _replace_if_newer_and_present(pd, data, erec, data_newer, k):
            if data.get(k) and (data_newer or not erec.get(k)):
                pd[k] = data[k]
            else:
                pd[k] = erec.get(k)

        # unzipping
        fns, fn_dicts = [], []
        if groups:
            fns, fn_dicts = zip(*groups)

        load_func = cache.load_all_from_cache
        for fn_dict, data in zip(fn_dicts, self.thread_executor.map(load_func, fns)):
            # like repodata (index_subdir), we do not reach this code when the
            # older channeldata.json matches
            if data:
                data.update(fn_dict)
                name = data["name"]
                # existing record
                erec = package_data.get(name, {})
                data_v = data.get("version", "0")
                erec_v = erec.get("version", "0")
                data_newer = VersionOrder(data_v) > VersionOrder(erec_v)

                package_data[name] = package_data.get(name, {})
                # keep newer value for these
                for k in (
                    "description",
                    "dev_url",
                    "doc_url",
                    "doc_source_url",
                    "home",
                    "license",
                    "source_url",
                    "source_git_url",
                    "summary",
                    "icon_url",
                    "icon_hash",
                    "tags",
                    "identifiers",
                    "keywords",
                    "recipe_origin",
                    "version",
                ):
                    _replace_if_newer_and_present(
                        package_data[name], data, erec, data_newer, k
                    )

                # keep any true value for these, since we don't distinguish subdirs
                for k in (
                    "binary_prefix",
                    "text_prefix",
                    "activate.d",
                    "deactivate.d",
                    "pre_link",
                    "post_link",
                    "pre_unlink",
                ):
                    package_data[name][k] = any((data.get(k), erec.get(k)))

                package_data[name]["subdirs"] = sorted(
                    list(set(erec.get("subdirs", []) + [subdir]))
                )
                # keep one run_exports entry per version of the package, since these vary by version
                run_exports = erec.get("run_exports", {})
                exports_from_this_version = data.get("run_exports")
                if exports_from_this_version:
                    run_exports[data_v] = data.get("run_exports")
                package_data[name]["run_exports"] = run_exports
                package_data[name]["timestamp"] = _make_seconds(
                    max(
                        data.get("timestamp", 0),
                        channel_data.get(name, {}).get("timestamp", 0),
                    )
                )

        channel_data.update(
            {
                "channeldata_version": CHANNELDATA_VERSION,
                "subdirs": sorted(
                    list(set(channel_data.get("subdirs", []) + [subdir]))
                ),
                "packages": package_data,
            }
        )

    def _write_channeldata(self, channeldata):
        # trim out commits, as they can take up a ton of space.  They're really only for the RSS feed.
        for _pkg, pkg_dict in channeldata.get("packages", {}).items():
            if "commits" in pkg_dict:
                del pkg_dict["commits"]
        channeldata_path = join(self.channel_root, "channeldata.json")
        content = json.dumps(channeldata, indent=2, sort_keys=True)
        _maybe_write(channeldata_path, content, True)

    def _load_patch_instructions_tarball(self, subdir, patch_generator):
        instructions = {}
        with TemporaryDirectory() as tmpdir:
            conda_package_handling.api.extract(patch_generator, dest_dir=tmpdir)
            instructions_file = os.path.join(tmpdir, subdir, "patch_instructions.json")
            if os.path.isfile(instructions_file):
                with open(instructions_file) as f:
                    instructions = json.load(f)
        return instructions

    def _create_patch_instructions(self, subdir, repodata, patch_generator=None):
        gen_patch_path = patch_generator or join(self.channel_root, "gen_patch.py")
        if isfile(gen_patch_path):
            log.debug(f"using patch generator {gen_patch_path} for {subdir}")

            # https://stackoverflow.com/a/41595552/2127762
            try:
                from importlib.util import module_from_spec, spec_from_file_location

                spec = spec_from_file_location("a_b", gen_patch_path)
                mod = module_from_spec(spec)

                spec.loader.exec_module(mod)
            # older pythons
            except ImportError:
                import imp

                mod = imp.load_source("a_b", gen_patch_path)

            instructions = mod._patch_repodata(repodata, subdir)

            if instructions.get("patch_instructions_version", 0) > 1:
                raise RuntimeError("Incompatible patch instructions version")

            return instructions
        else:
            if patch_generator:
                raise ValueError(
                    "Specified metadata patch file '{}' does not exist.  Please try an absolute "
                    "path, or examine your relative path carefully with respect to your cwd.".format(
                        patch_generator
                    )
                )
            return {}

    def _write_patch_instructions(self, subdir, instructions):
        new_patch = json.dumps(instructions, indent=2, sort_keys=True)
        patch_instructions_path = join(
            self.channel_root, subdir, "patch_instructions.json"
        )
        _maybe_write(patch_instructions_path, new_patch, True)

    def _load_instructions(self, subdir):
        patch_instructions_path = join(
            self.channel_root, subdir, "patch_instructions.json"
        )
        if isfile(patch_instructions_path):
            log.debug("using patch instructions %s" % patch_instructions_path)
            with open(patch_instructions_path) as fh:
                instructions = json.load(fh)
                if instructions.get("patch_instructions_version", 0) > 1:
                    raise RuntimeError("Incompatible patch instructions version")
                return instructions
        return {}

    def _patch_repodata(self, subdir, repodata, patch_generator=None):
        if patch_generator and any(
            patch_generator.endswith(ext) for ext in CONDA_PACKAGE_EXTENSIONS
        ):
            instructions = self._load_patch_instructions_tarball(
                subdir, patch_generator
            )
        else:
            instructions = self._create_patch_instructions(
                subdir, repodata, patch_generator
            )
        if instructions:
            self._write_patch_instructions(subdir, instructions)
        else:
            instructions = self._load_instructions(subdir)
        if instructions.get("patch_instructions_version", 0) > 1:
            raise RuntimeError("Incompatible patch instructions version")

        return _apply_instructions(subdir, repodata, instructions), instructions
