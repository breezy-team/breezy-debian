#!/usr/bin/python3
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Support for merging new upstream versions."""

import errno
import json
import logging
import os
import socket
import ssl
import tempfile
import traceback
from typing import Optional, Callable, Union

from breezy.bzr import LineEndingError
from breezy.git.remote import RemoteGitError
import breezy.plugins.launchpad  # noqa: F401
from breezy.workingtree import WorkingTree

from debian.changelog import Version, ChangelogParseError

from breezy import errors, urlutils

from breezy.branch import Branch
from breezy.controldir import ControlDir
from breezy.errors import (
    InvalidNormalization,
    NoCommits,
    InvalidHttpResponse,
    NoRoundtrippingSupport,
    UncommittedChanges,
)
from breezy.transport import (
    FileExists,
    NoSuchFile,
    UnsupportedProtocol,
)

from breezy.workingtree import PointlessMerge
from breezy.transport import (
    Transport,
    UnusableRedirect,
    get_transport,
)
from .config import UpstreamMetadataSyntaxError
from .info import versions_dict
from .util import (
    InconsistentSourceFormatError,
)
from .import_dsc import (
    UpstreamAlreadyImported,
    UpstreamBranchAlreadyMerged,
    CorruptUpstreamSourceFile,
)
from .changelog import debcommit

from breezy.transform import MalformedTransform

from .merge_upstream import (
    get_upstream_branch_location,
    do_import,
    do_merge,
    get_existing_imported_upstream_revids,
    get_tarballs,
)
from .repack_tarball import (
    UnsupportedRepackFormat,
)

from .upstream.pristinetar import (
    PristineTarError,
    get_pristine_tar_source,
)

from .util import (
    debuild_config,
    guess_build_type,
    get_files_excluded,
    tree_contains_upstream_source,
    BUILD_TYPE_MERGE,
    BUILD_TYPE_NATIVE,
    find_changelog,
    MissingChangelogError,
    control_files_in_root,
    full_branch_url,
)

from .upstream import (
    TarfileSource,
    MissingUpstreamTarball,
    PackageVersionNotPresent,
)
from .upstream.uscan import (
    UScanSource,
    UScanError,
    NoWatchFile,
    WatchLineWithoutMatches,
    WatchLineWithoutMatchingHrefs,
)
from .upstream.branch import (
    UpstreamBranchSource,
    DistCommandFailed,
    run_dist_command,
    PreviousVersionTagMissing,
)

from debmutate.changelog import ChangelogEditor, upstream_merge_changelog_line
from debmutate.reformatting import GeneratedFile
from debmutate.versions import (
    add_dfsg_suffix,
    strip_dfsg_suffix,
    matches_release,
    new_upstream_package_version,
    initial_debian_revision,
    debianize_upstream_version,
)

from debmutate.vcs import split_vcs_url
from debmutate.watch import WatchSyntaxError

from breezy.tree import Tree, MissingNestedTree


class BigVersionJump(Exception):
    """There was a big version jump."""

    def __init__(self, old_upstream_version, new_upstream_version):
        self.old_upstream_version = old_upstream_version
        self.new_upstream_version = new_upstream_version


class DistMissingNestedTree(Exception):
    """Dist failed to find a nested tree."""

    def __init__(self, path):
        self.path = path


class UpstreamMergeConflicted(Exception):
    """The upstream merge resulted in conflicts."""

    def __init__(self, upstream_version, conflicts):
        self.version = upstream_version
        self.conflicts = conflicts


class UpstreamAlreadyMerged(Exception):
    """Upstream release (or later version) has already been merged."""

    def __init__(self, upstream_version):
        self.version = upstream_version


class UpstreamBranchLocationInvalid(Exception):
    """Upstream branch location is invalid."""

    def __init__(self, url, extra):
        self.url = url
        self.extra = extra


class InvalidFormatUpstreamVersion(Exception):
    """Invalid format upstream version string."""

    def __init__(self, version, source):
        self.version = version
        self.source = source


class UpstreamBranchUnknown(Exception):
    """The location of the upstream branch is unknown."""


class NewUpstreamMissing(Exception):
    """Unable to find upstream version to merge."""


class UpstreamBranchUnavailable(Exception):
    """Snapshot merging was requested by upstream branch is unavailable."""

    def __init__(self, location, error):
        self.location = location
        self.error = error


class UpstreamVersionMissingInUpstreamBranch(Exception):
    """The upstream version is missing in the upstream branch."""

    def __init__(self, upstream_branch, upstream_version):
        self.branch = upstream_branch
        self.version = upstream_version


class PackageIsNative(Exception):
    """Unable to merge upstream version."""

    def __init__(self, package, version):
        self.package = package
        self.version = version


class UpstreamNotBundled(Exception):
    """Packaging branch does not carry upstream sources."""

    def __init__(self, package):
        self.package = package


class NewUpstreamTarballMissing(Exception):
    def __init__(self, package, version, upstream):
        self.package = package
        self.version = version
        self.upstream = upstream


class NoUpstreamLocationsKnown(Exception):
    """No upstream locations (uscan/repository) for the package are known."""

    def __init__(self, package):
        self.package = package


class NewerUpstreamAlreadyImported(Exception):
    """A newer upstream version has already been imported."""

    def __init__(self, old_upstream_version, new_upstream_version):
        self.old_upstream_version = old_upstream_version
        self.new_upstream_version = new_upstream_version


class ChangelogGeneratedFile(Exception):
    """The changelog file is generated."""

    def __init__(self, path, template_path, template_type):
        self.path = path
        self.template_path = template_path
        self.template_type = template_type


class ReleaseWithoutChanges(Exception):
    """There was a new release, but it didn't change anything."""

    def __init__(self, new_upstream_version):
        self.new_upstream_version = new_upstream_version


class MergingUpstreamSubpathUnsupported(Exception):
    """Merging an upstream branch with subpath is not yet supported."""


RELEASE_BRANCH_NAME = "new-upstream-release"
SNAPSHOT_BRANCH_NAME = "new-upstream-snapshot"
DEFAULT_DISTRIBUTION = "unstable"
VALUE_IMPORT = {"release": 20, "snapshot": 10}
VALUE_MERGE = {
    "release": 40,
    "snapshot": 30,
}


def is_big_version_jump(old_upstream_version, new_upstream_version):
    try:
        old_major_version = int(str(old_upstream_version).split(".")[0])
    except ValueError:
        return False
    try:
        new_major_version = int(str(new_upstream_version).split(".")[0])
    except ValueError:
        return False
    if old_major_version > 0 and new_major_version > 5 * old_major_version:
        return True
    return False


class ImportUpstreamResult:
    """Object representing the result of an import_upstream operation."""

    __slots__ = [
        "old_upstream_version",
        "new_upstream_version",
        "upstream_branch",
        "upstream_branch_browse",
        "upstream_revisions",
        "imported_revisions",
        "include_upstream_history",
    ]

    def __init__(
        self,
        include_upstream_history,
        old_upstream_version,
        new_upstream_version,
        upstream_branch,
        upstream_branch_browse,
        upstream_revisions,
        imported_revisions,
    ):
        self.old_upstream_version = old_upstream_version
        self.new_upstream_version = new_upstream_version
        self.upstream_branch = upstream_branch
        self.upstream_branch_browse = upstream_branch_browse
        self.upstream_revisions = upstream_revisions
        self.imported_revisions = imported_revisions
        self.include_upstream_history = include_upstream_history


def detect_include_upstream_history(
    packaging_branch, upstream_branch_source, package, old_upstream_version
):
    # Simple heuristic: Find the old upstream version and see if it's present
    # in the history of the packaging branch
    try:
        (revision, _subpath) = upstream_branch_source.version_as_revision(
            package, old_upstream_version
        )
    except PackageVersionNotPresent:
        logging.warning(
            "Old upstream version %r is not present in upstream "
            "branch %r. Unable to determine whether upstream history "
            "is normally included. Assuming no.",
            old_upstream_version,
            upstream_branch_source,
        )
        return False

    graph = packaging_branch.repository.get_graph()
    ret = graph.is_ancestor(revision, packaging_branch.last_revision())
    if ret:
        logging.info(
            "Including upstream history, since previous upstream version "
            "(%s) is present in packaging branch history.",
            old_upstream_version,
        )
    else:
        logging.info(
            "Not including upstream history, since previous upstream version "
            "(%s) is not present in packaging branch history.",
            old_upstream_version,
        )

    return ret


class BranchOpenError(Exception):
    """Generic exception for branch open failures."""


def _convert_exception(url: str, e: Exception) -> Optional[BranchOpenError]:
    if isinstance(e, socket.error):
        return BranchOpenError(url, "Socket error: %s" % e)
    if isinstance(e, errors.NotBranchError):
        return BranchOpenError(url, "Branch does not exist: %s" % e)
    if isinstance(e, UnsupportedProtocol):
        return BranchOpenError(url, str(e))
    if isinstance(e, errors.ConnectionError):
        return BranchOpenError(url, str(e))
    if isinstance(e, errors.PermissionDenied):
        return BranchOpenError(url, str(e))
    if isinstance(e, errors.InvalidHttpResponse):
        if "Unexpected HTTP status 429" in str(e):
            raise BranchOpenError(url, str(e))
        return BranchOpenError(url, str(e))
    if isinstance(e, errors.TransportError):
        return BranchOpenError(url, str(e))
    if isinstance(e, UnusableRedirect):
        return BranchOpenError(url, str(e))
    if isinstance(e, errors.UnsupportedFormatError):
        return BranchOpenError(url, str(e))
    if isinstance(e, errors.UnknownFormatError):
        return BranchOpenError(url, str(e))
    if isinstance(e, RemoteGitError):
        return BranchOpenError(url, str(e))
    if isinstance(e, LineEndingError):
        return BranchOpenError(url, str(e))
    return None


def open_branch(
    url: str,
    possible_transports: Optional[list[Transport]] = None,
    name: str = None,
) -> Branch:
    """Open a branch by URL."""
    from breezy.directory_service import directories

    url = directories.dereference(url, purpose="read")

    url, params = urlutils.split_segment_parameters(url)
    if name is None:
        try:
            name = urlutils.unquote(params["branch"])
        except KeyError:
            name = None
    try:
        transport = get_transport(url, possible_transports=possible_transports)
        dir = ControlDir.open_from_transport(transport)
        return dir.open_branch(name=name)
    except Exception as e:
        converted = _convert_exception(url, e)
        if converted is not None:
            raise converted
        raise e


def find_new_upstream(  # noqa: C901
    tree,
    subpath: str,
    config,
    package,
    location: Optional[str] = None,
    old_upstream_version=None,
    new_upstream_version=None,
    trust_package: bool = False,
    version_kind: str = "release",
    allow_ignore_upstream_branch: bool = False,
    top_level: bool = False,
    create_dist=None,
    include_upstream_history: Optional[bool] = None,
    force_big_version_jump: bool = False,
    require_uscan: bool = False,
    skip_signatures: bool = False,
):
    # TODO(jelmer): Find the lastest upstream present in the upstream branch
    # rather than what's in the changelog.

    upstream_branch_location, upstream_branch_browse = get_upstream_branch_location(
        tree, subpath, config, trust_package=trust_package
    )

    if upstream_branch_location:
        (upstream_branch_url, upstream_branch_name, upstream_subpath) = split_vcs_url(
            upstream_branch_location
        )
        if upstream_branch_name:
            upstream_branch_url = urlutils.join_segment_parameters(
                upstream_branch_url, {"branch": upstream_branch_name}
            )
        try:
            upstream_branch = open_branch(upstream_branch_url)
        except BranchOpenError as e:
            if version_kind != "snapshot" and allow_ignore_upstream_branch:
                logging.warning(
                    "Upstream branch %s inaccessible; ignoring. %s",
                    upstream_branch_location,
                    e,
                )
            else:
                raise UpstreamBranchUnavailable(upstream_branch_location, e)
            upstream_branch = None
            upstream_branch_browse = None
        except urlutils.InvalidURL as e:
            if version_kind != "snapshot" and allow_ignore_upstream_branch:
                logging.warning(
                    "Upstream branch location %s invalid; ignoring. %s", e.path, e
                )
            else:
                raise UpstreamBranchLocationInvalid(e.path, e.extra)
            upstream_branch = None
            upstream_branch_browse = None
    else:
        upstream_branch = None
        upstream_subpath = None  # noqa: F841

    if upstream_branch is not None:
        try:
            upstream_branch_source = UpstreamBranchSource.from_branch(
                upstream_branch,
                config=config,
                local_dir=tree.controldir,
                create_dist=create_dist,
                version_kind=version_kind,
                subpath=upstream_subpath,
            )
        except InvalidHttpResponse as e:
            raise UpstreamBranchUnavailable(upstream_branch_location, str(e))
        except ssl.SSLError as e:
            raise UpstreamBranchUnavailable(upstream_branch_location, str(e))
    else:
        upstream_branch_source = None

    if location is not None:
        try:
            branch = open_branch(location)
        except BranchOpenError:
            primary_upstream_source = TarfileSource(location, new_upstream_version)
        else:
            primary_upstream_source = UpstreamBranchSource.from_branch(
                branch,
                config=config,
                local_dir=tree.controldir,
                create_dist=create_dist,
                version_kind=version_kind,
            )
    else:
        if version_kind == "snapshot":
            if upstream_branch_source is None:
                raise UpstreamBranchUnknown()
            primary_upstream_source = upstream_branch_source
        else:
            try:
                primary_upstream_source = UScanSource.from_tree(
                    tree,
                    subpath,
                    top_level,
                    auto_fix=True,
                    skip_signatures=skip_signatures,
                )
            except NoWatchFile:
                # TODO(jelmer): Call out to lintian_brush.watch to generate a
                # watch file.
                if upstream_branch_source is None:
                    raise NoUpstreamLocationsKnown(package)
                if require_uscan:
                    raise
                primary_upstream_source = upstream_branch_source

    if new_upstream_version is None and primary_upstream_source is not None:
        unmangled_new_upstream_version, new_upstream_version = (
            primary_upstream_source.get_latest_version(package, old_upstream_version)
        )
    else:
        new_upstream_version = debianize_upstream_version(new_upstream_version)

    if new_upstream_version is None:
        raise NewUpstreamMissing()

    if not new_upstream_version[0].isdigit():
        # dpkg forbids this, let's just refuse it early
        raise InvalidFormatUpstreamVersion(
            new_upstream_version, primary_upstream_source
        )

    try:
        new_upstream_version = Version(new_upstream_version)
    except ValueError:
        raise InvalidFormatUpstreamVersion(
            new_upstream_version, primary_upstream_source
        )

    if old_upstream_version:
        if strip_dfsg_suffix(str(old_upstream_version)) == strip_dfsg_suffix(
            str(new_upstream_version)
        ):
            raise UpstreamAlreadyImported(new_upstream_version)
        if old_upstream_version > new_upstream_version:
            if version_kind == "release" and matches_release(
                str(old_upstream_version), str(new_upstream_version)
            ):
                raise UpstreamAlreadyImported(new_upstream_version)
            raise NewerUpstreamAlreadyImported(
                old_upstream_version, new_upstream_version
            )
        if (
            is_big_version_jump(old_upstream_version, new_upstream_version)
            and not force_big_version_jump
        ):
            raise BigVersionJump(old_upstream_version, new_upstream_version)

    # TODO(jelmer): Check if new_upstream_version is already imported

    logging.info("Using version string %s.", new_upstream_version)

    if include_upstream_history is None and upstream_branch_source is not None:
        include_upstream_history = detect_include_upstream_history(
            tree.branch, upstream_branch_source, package, old_upstream_version
        )

    if include_upstream_history is False:
        upstream_branch_source = None

    # Look up the revision id from the version string
    if upstream_branch_source is not None:
        try:
            upstream_revisions = upstream_branch_source.version_as_revisions(
                package, str(new_upstream_version)
            )
        except PackageVersionNotPresent:
            if upstream_branch_source is primary_upstream_source:
                # The branch is our primary upstream source, so if it can't
                # find the version then there's nothing we can do.
                raise UpstreamVersionMissingInUpstreamBranch(
                    upstream_branch_source.upstream_branch, str(new_upstream_version)
                )
            elif not allow_ignore_upstream_branch:
                raise UpstreamVersionMissingInUpstreamBranch(
                    upstream_branch_source.upstream_branch, str(new_upstream_version)
                )
            else:
                logging.warning(
                    "Upstream version %s is not in upstream branch %s. "
                    "Not merging from upstream branch. ",
                    new_upstream_version,
                    upstream_branch_source.upstream_branch,
                )
                upstream_revisions = None
                upstream_branch_source = None
    else:
        upstream_revisions = None

    try:
        files_excluded = get_files_excluded(tree, subpath, top_level)
    except NoSuchFile:
        files_excluded = None
    else:
        if files_excluded:
            new_upstream_version = Version(
                add_dfsg_suffix(str(new_upstream_version), old_upstream_version)
            )
            logging.info(
                "Adding DFSG suffix since upstream files are excluded: %s",
                new_upstream_version,
            )

    return (
        primary_upstream_source,
        new_upstream_version,
        upstream_revisions,
        upstream_branch_source,
        upstream_branch,
        upstream_branch_browse,
        files_excluded,
        include_upstream_history,
    )


def import_upstream(
    tree: Tree,
    version_kind: str = "release",
    location: Optional[str] = None,
    new_upstream_version: Optional[Union[Version, str]] = None,
    force: bool = False,
    distribution_name: str = DEFAULT_DISTRIBUTION,
    allow_ignore_upstream_branch: bool = True,
    trust_package: bool = False,
    committer: Optional[str] = None,
    subpath: str = "",
    include_upstream_history: Optional[bool] = None,
    create_dist: Optional[Callable[[Tree, str, Version, str], Optional[str]]] = None,
    force_big_version_jump: bool = False,
    skip_signatures: bool = False,
) -> ImportUpstreamResult:
    """Import a new upstream version into a tree.

    Raises:
      InvalidFormatUpstreamVersion
      PreviousVersionTagMissing
      UnsupportedRepackFormat
      DistCommandFailed
      MissingChangelogError
      MissingUpstreamTarball
      NewUpstreamMissing
      UpstreamBranchUnavailable
      UpstreamAlreadyImported
      QuiltError
      UpstreamVersionMissingInUpstreamBranch
      UpstreamBranchUnknown
      PackageIsNative
      InconsistentSourceFormatError
      ChangelogParseError
      UScanError
      UpstreamMetadataSyntaxError
      UpstreamNotBundled
      NewUpstreamTarballMissing
      NoUpstreamLocationsKnown
      NewerUpstreamAlreadyImported
    Returns:
      ImportUpstreamResult object
    """
    # TODO(jelmer): support more components
    components = [None]
    config = debuild_config(tree, subpath)
    (changelog, top_level) = find_changelog(tree, subpath, merge=False, max_blocks=2)
    old_upstream_version = changelog.version.upstream_version
    package = changelog.package
    contains_upstream_source = tree_contains_upstream_source(tree, subpath)
    build_type = config.build_type
    if build_type is None:
        build_type = guess_build_type(
            tree,
            changelog.version,
            subpath,
            contains_upstream_source=contains_upstream_source,
        )
    if build_type == BUILD_TYPE_MERGE:
        raise UpstreamNotBundled(changelog.package)
    if build_type == BUILD_TYPE_NATIVE:
        raise PackageIsNative(changelog.package, changelog.version)

    (
        primary_upstream_source,
        new_upstream_version,
        upstream_revisions,
        upstream_branch_source,
        upstream_branch,
        upstream_branch_browse,
        files_excluded,
        include_upstream_history,
    ) = find_new_upstream(
        tree,
        subpath,
        config,
        package,
        location=location,
        old_upstream_version=old_upstream_version,
        new_upstream_version=new_upstream_version,
        trust_package=trust_package,
        version_kind=version_kind,
        allow_ignore_upstream_branch=allow_ignore_upstream_branch,
        top_level=top_level,
        include_upstream_history=include_upstream_history,
        create_dist=create_dist,
        force_big_version_jump=force_big_version_jump,
        skip_signatures=skip_signatures,
    )

    with tempfile.TemporaryDirectory() as target_dir:
        initial_path = os.path.join(target_dir, "initial")
        os.mkdir(initial_path)
        try:
            locations = primary_upstream_source.fetch_tarballs(
                package, str(new_upstream_version), target_dir, components=components
            )
        except (PackageVersionNotPresent, WatchLineWithoutMatchingHrefs):
            if upstream_revisions is not None:
                locations = upstream_branch_source.fetch_tarballs(
                    package,
                    str(new_upstream_version),
                    initial_path,
                    components=components,
                    revisions=upstream_revisions,
                )
            else:
                raise
        orig_path = os.path.join(target_dir, "orig")
        try:
            tarball_filenames = get_tarballs(
                orig_path, tree, package, new_upstream_version, locations
            )
        except FileExists as e:
            raise AssertionError(
                "The target file %s already exists, and is either "
                "different to the new upstream tarball, or they "
                "are of different formats. Either delete the target "
                "file, or use it as the argument to import." % e.path
            )
        imported_revisions = do_import(
            tree,
            subpath,
            tarball_filenames,
            package,
            str(new_upstream_version),
            str(old_upstream_version),
            upstream_branch_source.upstream_branch if upstream_branch_source else None,
            upstream_revisions,
            merge_type=None,
            force=force,
            committer=committer,
            files_excluded=files_excluded,
        )

    return ImportUpstreamResult(
        include_upstream_history=include_upstream_history,
        old_upstream_version=old_upstream_version,
        new_upstream_version=new_upstream_version,
        upstream_branch=upstream_branch,
        upstream_branch_browse=upstream_branch_browse,
        upstream_revisions=upstream_revisions,
        imported_revisions=imported_revisions,
    )


class MergeUpstreamResult:
    """Object representing the result of a merge_upstream operation."""

    __slots__ = [
        "old_upstream_version",
        "new_upstream_version",
        "upstream_branch",
        "upstream_branch_browse",
        "upstream_revisions",
        "old_revision",
        "new_revision",
        "imported_revisions",
        "include_upstream_history",
    ]

    def __init__(
        self,
        include_upstream_history,
        old_upstream_version,
        new_upstream_version,
        upstream_branch,
        upstream_branch_browse,
        upstream_revisions,
        old_revision,
        new_revision,
        imported_revisions,
    ):
        self.include_upstream_history = include_upstream_history
        self.old_upstream_version = old_upstream_version
        self.new_upstream_version = new_upstream_version
        self.upstream_branch = upstream_branch
        self.upstream_branch_browse = upstream_branch_browse
        self.upstream_revisions = upstream_revisions
        self.old_revision = old_revision
        self.new_revision = new_revision
        self.imported_revisions = imported_revisions

    def __tuple__(self):
        # Backwards compatibility
        return (self.old_upstream_version, self.new_upstream_version)


def merge_upstream(  # noqa: C901
    tree: Tree,
    version_kind: str = "release",
    location: Optional[str] = None,
    new_upstream_version: Optional[str] = None,
    *,
    force: bool = False,
    distribution_name: str = DEFAULT_DISTRIBUTION,
    allow_ignore_upstream_branch: bool = True,
    trust_package: bool = False,
    committer: Optional[str] = None,
    update_changelog: bool = True,
    subpath: str = "",
    include_upstream_history: Optional[bool] = None,
    create_dist: Optional[Callable[[Tree, str, Version, str], Optional[str]]] = None,
    force_big_version_jump: bool = False,
    debian_revision: Optional[str] = None,
    require_uscan: bool = False,
    skip_signatures: bool = False,
    skip_empty: bool = False,
) -> MergeUpstreamResult:
    """Merge a new upstream version into a tree.

    Raises:
      InvalidFormatUpstreamVersion
      PreviousVersionTagMissing
      UnsupportedRepackFormat
      DistCommandFailed
      MissingChangelogError
      MissingUpstreamTarball
      NewUpstreamMissing
      UpstreamBranchUnavailable
      UpstreamAlreadyMerged
      UpstreamAlreadyImported
      UpstreamMergeConflicted
      QuiltError
      UpstreamVersionMissingInUpstreamBranch
      UpstreamBranchUnknown
      PackageIsNative
      InconsistentSourceFormatError
      ChangelogParseError
      UScanError
      NoUpstreamLocationsKnown
      UpstreamMetadataSyntaxError
      NewerUpstreamAlreadyImported
      WatchSyntaxError
      ReleaseWithoutChanges
    Returns:
      MergeUpstreamResult object
    """
    components = [None]
    config = debuild_config(tree, subpath)
    (changelog, top_level) = find_changelog(tree, subpath, merge=False, max_blocks=2)
    old_upstream_version = changelog.version.upstream_version
    old_revision = tree.last_revision()
    package = changelog.package
    contains_upstream_source = tree_contains_upstream_source(tree, subpath)
    build_type = config.build_type
    if build_type is None:
        build_type = guess_build_type(
            tree,
            changelog.version,
            subpath,
            contains_upstream_source=contains_upstream_source,
        )
    need_upstream_tarball = build_type != BUILD_TYPE_MERGE
    if build_type == BUILD_TYPE_NATIVE:
        raise PackageIsNative(changelog.package, changelog.version)

    (
        primary_upstream_source,
        new_upstream_version,
        upstream_revisions,
        upstream_branch_source,
        upstream_branch,
        upstream_branch_browse,
        files_excluded,
        include_upstream_history,
    ) = find_new_upstream(
        tree,
        subpath,
        config,
        package,
        location=location,
        old_upstream_version=old_upstream_version,
        new_upstream_version=new_upstream_version,
        trust_package=trust_package,
        version_kind=version_kind,
        allow_ignore_upstream_branch=allow_ignore_upstream_branch,
        top_level=top_level,
        include_upstream_history=include_upstream_history,
        create_dist=create_dist,
        force_big_version_jump=force_big_version_jump,
        require_uscan=require_uscan,
        skip_signatures=skip_signatures,
    )

    if need_upstream_tarball:
        with tempfile.TemporaryDirectory() as target_dir:
            initial_path = os.path.join(target_dir, "initial")
            os.mkdir(initial_path)

            try:
                locations = primary_upstream_source.fetch_tarballs(
                    package,
                    str(new_upstream_version),
                    initial_path,
                    components=components,
                )
            except PackageVersionNotPresent as e:
                if upstream_revisions is not None:
                    locations = upstream_branch_source.fetch_tarballs(
                        package,
                        str(new_upstream_version),
                        initial_path,
                        components=components,
                        revisions=upstream_revisions,
                    )
                else:
                    raise NewUpstreamTarballMissing(e.package, e.version, e.upstream)

            orig_path = os.path.join(target_dir, "orig")
            os.mkdir(orig_path)
            try:
                tarball_filenames = get_tarballs(
                    orig_path, tree, package, new_upstream_version, locations
                )
            except FileExists as e:
                raise AssertionError(
                    "The target file %s already exists, and is either "
                    "different to the new upstream tarball, or they "
                    "are of different formats. Either delete the target "
                    "file, or use it as the argument to import." % e.path
                )
            try:
                conflicts, imported_revids = do_merge(
                    tree,
                    tarball_filenames,
                    package,
                    str(new_upstream_version),
                    str(old_upstream_version),
                    upstream_branch_source.upstream_branch
                    if upstream_branch_source
                    else None,
                    upstream_revisions,
                    merge_type=None,
                    force=force,
                    committer=committer,
                    files_excluded=files_excluded,
                )
            except UpstreamBranchAlreadyMerged:
                # TODO(jelmer): Perhaps reconcile these two exceptions?
                raise UpstreamAlreadyMerged(new_upstream_version)
            except UpstreamAlreadyImported:
                pristine_tar_source = get_pristine_tar_source(tree, tree.branch)
                imported_revids = get_existing_imported_upstream_revids(
                    pristine_tar_source, package, new_upstream_version
                )
                for entry in imported_revids:
                    if entry[0] is None:
                        upstream_revid = entry[2]
                        break
                else:
                    upstream_revid = None
                try:
                    try:
                        conflicts = tree.merge_from_branch(
                            pristine_tar_source.branch, to_revision=upstream_revid
                        )
                    except NoCommits:
                        # Maybe it's somewhere in our history, but
                        # there isn't a viable upstream branch.
                        conflicts = tree.merge_from_branch(
                            tree.branch, to_revision=upstream_revid
                        )
                except PointlessMerge:
                    raise UpstreamAlreadyMerged(new_upstream_version)
    else:
        conflicts = 0
        imported_revids = []

    if skip_empty:
        changes = tree.iter_changes(tree.basis_tree())
        try:
            next(changes)
        except StopIteration:
            tree.set_pending_merges([])
            raise ReleaseWithoutChanges(new_upstream_version)

    # Re-read changelog, since it may have been changed by the merge
    # from upstream.
    try:
        (changelog, top_level) = find_changelog(tree, subpath, False, max_blocks=2)
    except (ChangelogParseError, MissingChangelogError):
        # If there was a conflict that affected debian/changelog, then that
        # might be to blame.
        if conflicts:
            raise UpstreamMergeConflicted(old_upstream_version, conflicts)
        raise
    if top_level:
        debian_path = subpath
    else:
        debian_path = os.path.join(subpath, "debian")

    if Version(old_upstream_version) >= Version(new_upstream_version):
        if conflicts:
            raise UpstreamMergeConflicted(old_upstream_version, conflicts)
        raise UpstreamAlreadyMerged(new_upstream_version)

    try:
        with ChangelogEditor(
            tree.abspath(os.path.join(debian_path, "changelog"))
        ) as cl:
            if debian_revision is None:
                debian_revision = initial_debian_revision(distribution_name)
            new_version = str(
                new_upstream_package_version(
                    new_upstream_version, debian_revision, cl[0].version.epoch
                )
            )
            if not update_changelog:
                # We need to run "gbp dch" here, since the next "gbp dch" runs
                # won't pick up the pending changes, as we're about to change
                # debian/changelog.
                from debmutate.changelog import gbp_dch

                gbp_dch(tree.basedir)
            cl.auto_version(new_version)

            cl.add_entry([upstream_merge_changelog_line(new_upstream_version)])
    except GeneratedFile as e:
        raise ChangelogGeneratedFile(e.path, e.template_path, e.template_type)

    if not need_upstream_tarball:
        logging.info("The changelog has been updated for the new version.")
    else:
        if conflicts:
            raise UpstreamMergeConflicted(new_upstream_version, conflicts)

    debcommit(tree, subpath=subpath, committer=committer)

    return MergeUpstreamResult(
        include_upstream_history=include_upstream_history,
        old_upstream_version=old_upstream_version,
        new_upstream_version=new_upstream_version,
        old_revision=old_revision,
        new_revision=tree.last_revision(),
        upstream_branch=upstream_branch,
        upstream_branch_browse=upstream_branch_browse,
        upstream_revisions=upstream_revisions,
        imported_revisions=imported_revids,
    )


def report_fatal(
    code: str,
    description: str,
    *,
    upstream_version: Optional[str] = None,
    details=None,
    hint: Optional[str] = None,
    transient: Optional[bool] = None,
    stage=None,
):
    if os.environ.get("SVP_API") == "1":
        context = {}
        if upstream_version is not None:
            context["upstream_version"] = str(upstream_version)
        with open(os.environ["SVP_RESULT"], "w") as f:
            json.dump(
                {
                    "result_code": code,
                    "hint": hint,
                    "transient": transient,
                    "stage": stage,
                    "description": description,
                    "versions": versions_dict(),
                    "details": details,
                    "context": context,
                },
                f,
            )
    logging.fatal("%s", description)
    if hint:
        logging.info("%s", hint)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trust-package",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="Only import a new version, do not merge.",
    )
    parser.add_argument(
        "--update-packaging",
        action="store_true",
        default=False,
        help="Attempt to update packaging to upstream changes.",
    )
    parser.add_argument(
        "--snapshot",
        help="Merge a new upstream snapshot rather than a release",
        action="store_true",
    )
    parser.add_argument(
        "--refresh-patches",
        action="store_true",
        help="Refresh quilt patches after upstream merge.",
    )
    parser.add_argument(
        "--dist-command",
        type=str,
        help="Command to run to create tarball from source tree.",
        default=os.environ.get("DIST"),
    )
    parser.add_argument(
        "--no-include-upstream-history",
        action="store_false",
        default=None,
        dest="include_upstream_history",
        help="do not include upstream branch history",
    )
    parser.add_argument(
        "--include-upstream-history",
        action="store_true",
        dest="include_upstream_history",
        help="force inclusion of upstream history",
        default=None,
    )
    parser.add_argument(
        "--force-big-version-jump",
        action="store_true",
        help="force through big version jumps",
    )
    parser.add_argument(
        "--debian-revision",
        type=str,
        help="Debian revision to use (e.g. '1' or '0ubuntu1')",
        default=None,
    )
    parser.add_argument(
        "--require-uscan",
        action="store_true",
        help=("Require that uscan provides a tarball(if --snapshot is not specified)"),
    )
    parser.add_argument(
        "--upstream-location",
        type=str,
        default=None,
        help="Location of the upstream location "
        "(defaults to reading debian/upstream/metadata or guessing)",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--skip-signatures", action="store_true", help="Skip signature validation"
    )
    parser.add_argument(
        "--skip-empty", action="store_true", help="Skip releases without changes"
    )
    # TODO(jelmer): Support "auto"
    parser.add_argument(
        "--version-kind",
        choices=["snapshot", "release"],
        default="release",
        help="Version kind to merge.",
    )
    args = parser.parse_args(argv)

    if args.verbose:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(level=loglevel, format="%(message)s")

    from breezy.plugin import load_plugins

    load_plugins()

    local_tree, subpath = WorkingTree.open_containing(".")

    with local_tree.lock_write():
        committer = os.environ.get("COMMITTER")

        if control_files_in_root(local_tree, subpath):
            report_fatal(
                "control-files-in-root",
                "control files live in root rather than debian/ (LarstIQ mode)",
                transient=False,
            )
            return 1

        if args.dist_command:

            def create_dist(tree, package, version, target_dir, subpath=""):
                try:
                    return run_dist_command(
                        tree,
                        package,
                        version,
                        target_dir,
                        args.dist_command,
                        subpath=subpath,
                    )
                except MissingNestedTree as e:
                    raise DistMissingNestedTree(e.path) from e
        else:
            create_dist = None

        update_changelog = True
        if os.environ.get("DEB_UPDATE_CHANGELOG") == "leave":
            update_changelog = False
        elif os.environ.get("DEB_UPDATE_CHANGELOG") == "update":
            update_changelog = True

        if args.snapshot:
            version_kind = "snapshot"
        else:
            version_kind = args.version_kind

        try:
            if not args.import_only:
                try:
                    result = merge_upstream(
                        tree=local_tree,
                        version_kind=version_kind,
                        trust_package=args.trust_package,
                        update_changelog=update_changelog,
                        subpath=subpath,
                        committer=committer,
                        include_upstream_history=args.include_upstream_history,
                        create_dist=create_dist,
                        force_big_version_jump=args.force_big_version_jump,
                        debian_revision=args.debian_revision,
                        require_uscan=args.require_uscan,
                        location=args.upstream_location,
                        skip_signatures=args.skip_signatures,
                        skip_empty=args.skip_empty,
                    )
                except MalformedTransform:
                    traceback.print_exc()
                    error_description = (
                        "Malformed tree transform during new upstream merge"
                    )
                    error_code = "malformed-transform"
                    report_fatal(error_code, error_description, transient=False)
                    return 1
                except CorruptUpstreamSourceFile as e:
                    report_fatal("corrupt-upstream-file", str(e), transient=False)
                except UncommittedChanges as e:
                    report_fatal("uncommitted-changes", str(e), transient=False)
                    return 1
            else:
                result = import_upstream(
                    tree=local_tree,
                    version_kind=version_kind,
                    trust_package=args.trust_package,
                    subpath=subpath,
                    committer=committer,
                    include_upstream_history=args.include_upstream_history,
                    create_dist=create_dist,
                    force_big_version_jump=args.force_big_version_jump,
                    skip_signatures=args.skip_signatures,
                )
        except MemoryError as e:
            report_fatal("memory-error", str(e))
            return 1
        except UpstreamAlreadyImported as e:
            if version_kind == "snapshot":
                report_fatal(
                    "nothing-to-do",
                    "Last upstream version %s already imported." % e.version,
                    upstream_version=e.version,
                    transient=False,
                )
            else:
                report_fatal(
                    "nothing-to-do",
                    "Last upstream version %s already imported. " % e.version,
                    upstream_version=e.version,
                    hint="Import a snapshot by specifying --snapshot.",
                    transient=False,
                )
            return 1
        except ReleaseWithoutChanges as e:
            report_fatal(
                "nothing-to-do",
                "New release %s is available, but does not contain changes."
                % e.new_upstream_version,
                upstream_version=e.new_upstream_version,
                transient=False,
            )
            return 1
        except UpstreamBranchLocationInvalid as e:
            report_fatal(
                "upstream-branch-invalid",
                "The upstream branch location ({}) is invalid: {}".format(
                    e.url, e.extra
                ),
                transient=False,
            )
            return 1
        except ChangelogGeneratedFile as e:
            report_fatal(
                "changelog-generated-file",
                "changelog file can't be updated because it is generated. "
                "(template type: %s, path: %s)" % (e.template_type, e.template_path),
            )
            return 1
        except UnsupportedRepackFormat as e:
            error_description = (
                "Unable to repack file %s to supported tarball format."
                % (os.path.basename(e.location))
            )
            report_fatal(
                "unsupported-repack-format", error_description, transient=False
            )
            return 1
        except NewUpstreamMissing:
            report_fatal("new-upstream-missing", "Unable to find new upstream source.")
            return 1
        except UpstreamAlreadyMerged as e:
            report_fatal(
                "nothing-to-do",
                "Last upstream version %s already merged." % e.version,
                upstream_version=e.version,
                transient=False,
            )
            return 1
        except NoWatchFile:
            report_fatal(
                "no-watch-file",
                "No watch file is present, but --require-uscan was specified",
                transient=False,
            )
            return 1
        except PreviousVersionTagMissing as e:
            report_fatal(
                "previous-upstream-missing",
                "Previous upstream version %s missing (tag: %s)."
                % (e.version, e.tag_name),
                transient=False,
            )
            return 1
        except InvalidFormatUpstreamVersion as e:
            report_fatal(
                "invalid-upstream-version-format",
                "{!r} reported invalid format version string {}.".format(
                    e.source, e.version
                ),
                transient=False,
            )
            return 1
        except PristineTarError as e:
            report_fatal("pristine-tar-error", "Pristine tar error: %s" % e)
            return 1
        except UpstreamBranchUnavailable as e:
            error_description = "The upstream branch at {} was unavailable: {}".format(
                e.location, e.error
            )
            transient = None
            error_code = "upstream-branch-unavailable"
            if "Fossil branches are not yet supported" in str(e.error):
                error_code = "upstream-unsupported-vcs-fossil"
                transient = False
            if "Mercurial branches are not yet supported." in str(e.error):
                error_code = "upstream-unsupported-vcs-hg"
                transient = False
            if "Subversion branches are not yet supported." in str(e.error):
                error_code = "upstream-unsupported-vcs-svn"
                transient = False
            if "Darcs branches are not yet supported" in str(e.error):
                error_code = "upstream-unsupported-vcs-darcs"
                transient = False
            if "Unsupported protocol for url" in str(e.error):
                transient = False
                if "svn://" in str(e.error):
                    error_code = "upstream-unsupported-vcs-svn"
                elif "cvs+pserver://" in str(e.error):
                    error_code = "upstream-unsupported-vcs-cvs"
                else:
                    error_code = "upstream-unsupported-vcs"
            report_fatal(error_code, error_description, transient=transient)
            return 1
        except UpstreamBranchUnknown:
            report_fatal(
                "upstream-branch-unknown",
                "Upstream branch location unknown.",
                hint="Set 'Repository' field in debian/upstream/metadata?",
                transient=False,
            )
            return 1
        except UpstreamMergeConflicted as e:
            if isinstance(e.conflicts, int):
                conflicts = e.conflicts
            else:
                conflicts = [[c.path, c.typestring] for c in e.conflicts]
            report_fatal(
                "upstream-merged-conflicts",
                "Merging upstream version %s resulted in conflicts." % e.version,
                upstream_version=e.version,
                details={"conflicts": conflicts},
                transient=False,
            )
            return 1
        except PackageIsNative as e:
            report_fatal(
                "native-package",
                "Package {} is native; unable to merge new upstream.".format(e.package),
                transient=False,
            )
            return 1
        except ChangelogParseError as e:
            error_description = str(e)
            error_code = "unparseable-changelog"
            report_fatal(error_code, error_description, transient=False)
            return 1
        except UpstreamVersionMissingInUpstreamBranch as e:
            error_description = (
                "Upstream version {} not in upstream branch {!r}".format(
                    e.version, e.branch
                )
            )
            error_code = "upstream-version-missing-in-upstream-branch"
            report_fatal(error_code, error_description, transient=False)
            return 1
        except InconsistentSourceFormatError as e:
            report_fatal(
                "inconsistent-source-format",
                "Inconsistencies in type of package: %s" % e,
                transient=False,
            )
            return 1
        except WatchLineWithoutMatches as e:
            report_fatal(
                "uscan-watch-line-without-matches",
                "UScan did not find matches for line: %s" % e.line.strip(),
                transient=False,
            )
            return 1
        except NoRoundtrippingSupport:
            error_description = (
                "Unable to import upstream repository into packaging repository."
            )
            error_code = "roundtripping-error"
            report_fatal(error_code, error_description, transient=False)
            return 1
        except UScanError as e:
            error_description = str(e)
            if e.errors == "OpenPGP signature did not verify.":
                error_code = "upstream-pgp-signature-verification-failed"
            else:
                error_code = "uscan-error"
            report_fatal(error_code, error_description)
            return 1
        except UpstreamMetadataSyntaxError as e:
            report_fatal(
                "upstream-metadata-syntax-error",
                "Unable to parse {}: {}".format(e.path, e.error),
                transient=False,
            )
            return 1
        except InvalidNormalization as e:
            error_description = str(e)
            error_code = "invalid-path-normalization"
            report_fatal(error_code, error_description, transient=False)
            return 1
        except MissingChangelogError as e:
            report_fatal(
                "missing-changelog", "Missing changelog %s" % e, transient=False
            )
            return 1
        except DistCommandFailed as e:
            report_fatal(
                e.kind or "dist-command-failed",
                e.error,
                transient=False,
                stage=("dist",),
            )
            return 1
        except MissingUpstreamTarball as e:
            report_fatal("missing-upstream-tarball", "Missing upstream tarball: %s" % e)
            return 1
        except NewUpstreamTarballMissing as e:
            report_fatal(
                "new-upstream-tarball-missing",
                "New upstream version (%s/%s) found, but was missing "
                "when retrieved as tarball from %r."
                % (e.package, e.version, e.upstream),
                upstream_version=e.version,
            )
            return 1
        except NoUpstreamLocationsKnown:
            report_fatal(
                "no-upstream-locations-known",
                "No debian/watch file or Repository in "
                "debian/upstream/metadata to retrieve new upstream version "
                "from.",
            )
            return 1
        except NewerUpstreamAlreadyImported as e:
            report_fatal(
                "newer-upstream-version-already-imported",
                "A newer upstream release (%s) has already been imported. "
                "Found: %s" % (e.old_upstream_version, e.new_upstream_version),
                upstream_version=e.new_upstream_version,
                transient=False,
            )
            return 1
        except MergingUpstreamSubpathUnsupported as e:
            report_fatal(
                "unsupported-merging-upstream-with-subpath", str(e), transient=False
            )
        except WatchSyntaxError as e:
            report_fatal("watch-syntax-error", str(e), transient=False)
            return 1
        except BigVersionJump as e:
            report_fatal(
                "big-version-jump",
                "There was a big jump in upstream versions: {} ⇒ {}".format(
                    e.old_upstream_version, e.new_upstream_version
                ),
                upstream_version=e.new_upstream_version,
                transient=False,
            )
            return 1
        except DistMissingNestedTree as e:
            report_fatal(
                "requires-nested-tree-support",
                "Unable to find nested tree at %s" % e.path,
                transient=False,
                stage=("dist",),
            )
            return 1
        except OSError as e:
            if e.errno == errno.ENOSPC:
                report_fatal("no-space-on-device", str(e))
                return 1
            raise

        svp_context = {
            "old_upstream_version": str(result.old_upstream_version)
            if result.old_upstream_version
            else None,
            "upstream_version": str(result.new_upstream_version),
        }

        if result.upstream_branch:
            svp_context["upstream_branch_url"] = full_branch_url(result.upstream_branch)
            svp_context["upstream_branch_browse"] = result.upstream_branch_browse

        svp_context["include_upstream_history"] = result.include_upstream_history

        if args.import_only:
            logging.info(
                "Imported new upstream version %s (previous: %s)",
                result.new_upstream_version,
                result.old_upstream_version,
            )

            if os.environ.get("SVP_API") == "1":
                with open(os.environ["SVP_RESULT"], "w") as f:
                    json.dump(
                        {
                            "value": VALUE_IMPORT[version_kind],
                            "description": "Imported new upstream version %s"
                            % (result.new_upstream_version),
                            "context": svp_context,
                            "versions": versions_dict(),
                        },
                        f,
                    )
            return 0
        else:
            logging.info(
                "Merged new upstream version %s (previous: %s)",
                result.new_upstream_version,
                result.old_upstream_version,
            )

            if args.update_packaging:
                from .update_packaging import (
                    update_packaging,
                )

                old_tree = local_tree.branch.repository.revision_tree(
                    result.old_revision
                )
                notes = update_packaging(local_tree, old_tree, committer=committer)
                svp_context["notes"] = notes
                for n in notes:
                    logging.info("%s", n)

            patch_series_path = os.path.join(subpath, "debian/patches/series")
            if args.refresh_patches and local_tree.has_filename(patch_series_path):
                from .quilt_refresh import (
                    QuiltError,
                    QuiltPatchPushFailure,
                    QuiltPatchDoesNotApply,
                    refresh_quilt_patches,
                )

                logging.info("Refresh quilt patches.")
                try:
                    refresh_quilt_patches(
                        local_tree,
                        committer=committer,
                        subpath=subpath,
                    )
                except QuiltPatchDoesNotApply as e:
                    error_description = (
                        "Quilt patch %s no longer applies" % e.patch_name
                    )
                    error_code = "quilt-patch-out-of-date"
                    report_fatal(error_code, error_description)
                    return 1
                except QuiltPatchPushFailure as e:
                    error_description = (
                        "An error occurred refreshing quilt patch %s" % (e.patch_name,)
                    )
                    error_code = "quilt-refresh-error"
                    report_fatal(error_code, error_description)
                    return 1
                except QuiltError as e:
                    error_description = (
                        "An error (%d) occurred refreshing quilt patches: "
                        "%s%s" % (e.retcode, e.stderr, e.extra)
                    )
                    error_code = "quilt-refresh-error"
                    report_fatal(error_code, error_description, transient=False)
                    return 1
                except QuiltPatchPushFailure as e:
                    error_description = (
                        "An error occurred refreshing quilt patch %s: %s"
                        % (e.patch_name, e.actual_error.extra)
                    )
                    error_code = "quilt-refresh-error"
                    report_fatal(error_code, error_description, transient=False)
                    return 1

            logging.info("Merge new upstream version %s", result.new_upstream_version)
            proposed_commit_message = (
                "Merge new upstream release %s" % result.new_upstream_version
            )
            if os.environ.get("SVP_API") == "1":
                with open(os.environ["SVP_RESULT"], "w") as f:
                    json.dump(
                        {
                            "value": VALUE_MERGE[version_kind],
                            "description": "Merged new upstream version %s"
                            % (result.new_upstream_version),
                            "commit-message": proposed_commit_message,
                            "context": svp_context,
                            "versions": versions_dict(),
                        },
                        f,
                    )


if __name__ == "__main__":
    import sys

    from breezy.commands import exception_to_return_code

    sys.exit(exception_to_return_code(main(sys.argv[1:])))
