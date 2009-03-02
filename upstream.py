#    upstream.py -- Providers of upstream source
#    Copyright (C) 2009 Canonical Ltd.
#
#    This file is part of bzr-builddeb.
#
#    bzr-builddeb is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    bzr-builddeb is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with bzr-builddeb; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import os
import shutil
import subprocess
import tarfile
import tempfile

from debian_bundle.changelog import Version

from bzrlib.export import export
from bzrlib.trace import info

from bzrlib.plugins.builddeb.errors import MissingUpstreamTarball
from bzrlib.plugins.builddeb.import_dsc import DistributionBranch
from bzrlib.plugins.builddeb.repack_tarball import repack_tarball

class UpstreamProvider(object):
    """An upstream provider can provide the upstream source for a package.

    Different implementations can provide it in different ways, for
    instance using pristine-tar, or using apt.
    """

    def __init__(self, tree, branch, package, version, store_dir,
            larstiq=False, upstream_branch=None, upstream_revision=None,
            allow_split=False):
        """Create an UpstreamProvider.

        :param tree: The tree that is being built from.
        :param branch: The branch that is being built from.
        :param package: the name of the source package that is being built.
        :param version: the Version of the package that is being built.
        :param store_dir: A directory to cache the tarballs.
        :param larstiq: Whether the tree versions the root of ./debian.
        :param upstream_branch: An upstream branch that can be exported
            if needed.
        :param upstream_revision: The revision to use of the upstream branch
            if it is used.
        :param allows_split: Whether the provider can provide the tarball
            by exporting the branch and removing the "debian" dir.
        """
        self.tree = tree
        self.branch = branch
        self.package = package
        self.version = version
        self.store_dir = store_dir
        self.larstiq = larstiq
        self.upstream_branch = upstream_branch
        self.upstream_revision = upstream_revision
        self.allows_split = allows_split

    def provide(self, target_dir):
        """Provide the upstream tarball any way possible.

        Call this to place the correctly named tarball in to target_dir,
        through means possible.

        If the tarball is already there then do nothing.
        If it is in self.store_dir then copy it over.
        Else retrive it and cache it in self.store_dir, then copy it over:
           - If pristine-tar metadata is available then that will be used.
           - Else if apt knows about a source of that version that will be
             retrieved.
           - Else if uscan knows about that version it will be downloaded
             and repacked as needed.
           - Else a call will be made to get-orig-source to try and retrieve
             the tarball.

        If the tarball can't be found at all then MissingUpstreamTarball
        will be raised.

        :param target_dir: The directory to place the tarball in.
        :return: The path to the tarball.
        """
        if self.already_exists_in_target(target_dir):
            return os.path.join(target_dir, self._tarball_name())
        if not self.already_exists_in_store():
            if not os.path.exists(self.store_dir):
                os.makedirs(self.store_dir)
            if self.provide_with_pristine_tar(self.store_dir):
                pass
            elif self.provide_with_apt(self.store_dir):
                pass
            elif self.provide_with_uscan(self.store_dir):
                pass
            elif self.provide_with_get_orig_source(self.store_dir):
                pass
            elif self.provide_from_upstream_branch(self.store_dir):
                pass
            elif self.provide_from_self_by_split(self.store_dir):
                pass
        if self.provide_from_store_dir(target_dir):
            return os.path.join(target_dir, self._tarball_name())
        raise MissingUpstreamTarball(self._tarball_name())

    def already_exists_in_target(self, target_dir):
        return os.path.exists(os.path.join(target_dir, self._tarball_name()))

    def already_exists_in_store(self):
        return os.path.exists(os.path.join(self.store_dir,
                    self._tarball_name()))

    def provide_from_store_dir(self, target_dir):
        if self.already_exists_in_store():
            repack_tarball(os.path.join(self.store_dir, self._tarball_name()),
                    self._tarball_name(), target_dir=target_dir)
            return True
        return False

    def provide_with_apt(self, target_dir):
        import apt_pkg
        apt_pkg.init()
        sources = apt_pkg.GetPkgSrcRecords()
        sources.Restart()
        found = False
        info("Using apt to look for the upstream tarball.")
        while sources.Lookup(self.package):
            if self.version == Version(sources.Version).upstream_version:
                command = 'apt-get source -y --only-source --tar-only %s=%s' % \
                    (self.package, sources.Version)
                proc = subprocess.Popen(command, shell=True, cwd=target_dir)
                proc.wait()
                if proc.returncode != 0:
                    break
                return True
        info("apt could not find the needed tarball.")
        return False

    def provide_with_uscan(self, target_dir):
        if self.larstiq:
            watchfile = 'watch'
        else:
            watchfile = 'debian/watch'
        watch_id = self.tree.path2id(watchfile)
        if watch_id is None:
            info("No watch file to use to retrieve upstream tarball.")
            return False
        (tmp, tempfilename) = tempfile.mkstemp()
        try:
            tmp = os.fdopen(tmp, 'wb')
            watch = self.tree.get_file_text(watch_id)
            tmp.write(watch)
        finally:
            tmp.close()
        info("Using uscan to look for the upstream tarball.")
        try:
            r = os.system("uscan --upstream-version %s --force-download --rename "
                          "--package %s --watchfile %s --check-dirname-level 0 " 
                          "--download --repack --destdir %s" %
                          (self.version, self.package, tempfilename,
                           target_dir))
            if r != 0:
                info("uscan could not find the needed tarball.")
                return False
            return True
        finally:
          os.unlink(tempfilename)

    def provide_with_pristine_tar(self, target_dir):
        db = DistributionBranch(self.branch, None, tree=self.tree)
        if not db._has_upstream_version_in_packaging_branch(self.version):
            return False
        revid = db._revid_of_upstream_version_from_branch(self.version)
        if not db.has_pristine_tar_delta(revid):
            return False
        info("Using pristine-tar to reconstruct the needed tarball.")
        dest_filename = os.path.join(target_dir,
                self._tarball_name())
        db.reconstruct_pristine_tar(revid, self.package, self.version,
                dest_filename)
        return True

    def provide_with_get_orig_source(self, target_dir):
        if self.larstiq:
            rules_name = 'rules'
        else:
            rules_name = 'debian/rules'
        rules_id = self.tree.path2id(rules_name)
        if rules_id is not None:
            info("Trying to use get-orig-source to retrieve needed tarball.")
            desired_tarball_name = self._tarball_name()
            tmpdir = tempfile.mkdtemp(prefix="builddeb-get-orig-source-")
            try:
                base_export_dir = os.path.join(tmpdir, "export")
                export_dir = base_export_dir
                if self.larstiq:
                    export_dir = os.path.join(export_dir, "debian")
                export(self.tree, export_dir, format="dir")
                command = ["/usr/bin/make", "-f", "debian/rules",
                        "get-orig-source"]
                proc = subprocess.Popen(command, cwd=base_export_dir)
                ret = proc.wait()
                if ret != 0:
                    info("Trying to run get-orig-source rule failed")
                    return False
                fetched_tarball = os.path.join(tmpdir, desired_tarball_name)
                if not os.path.exists(fetched_tarball):
                    info("get-orig-source did not create %s"
                            % desired_tarball_name)
                    return False
                repack_tarball(fetched_tarball, desired_tarball_name,
                               target_dir=target_dir)
                return True
            finally:
                shutil.rmtree(tmpdir)
        info("No debian/rules file to try and use for a get-orig-source "
             "rule")
        return False

    def _tarball_name(self):
        return "%s_%s.orig.tar.gz" % (self.package, self.version)

    def provide_from_upstream_branch(self, target_dir):
        if self.upstream_branch is None:
            return False
        assert self.upstream_revision is not None
        self.upstream_branch.lock_read()
        try:
            rev_tree = self.upstream_branch.repository.revision_tree(
                    self.upstream_revision)
            dest = os.path.join(target_dir, self._tarball_name())
            tarball_base = "%s-%s" % (self.package, self.version)
            export(rev_tree, dest, 'tgz', tarball_base)
            return True
        finally:
            self.upstream_branch.unlock()

    def provide_from_self_by_split(self, target_dir):
        if not self.allow_split:
            return False
        tmpdir = tempfile.mkdtemp(prefix="builddeb-get-orig-source-")
        try:
            export_dir = os.path.join(tmpdir,
                    "%s-%s" % (self.package, self.version))
            export(self.tree, export_dir, format="dir")
            shutil.rmtree(os.path.join(export_dir, "debian"))
            target_file = os.path.join(target_dir, self._tarball_name())
            tar = tarfile.open(target_file, "w:gz")
            try:
                tar.add(export_dir, "%s-%s" % (self.package, self.version))
            finally:
                tar.close()
        finally:
            shutil.rmtree(tmpdir)


class _MissingUpstreamProvider(UpstreamProvider):
    """For tests"""

    def __init__(self):
        pass

    def provide(self, target_dir):
        raise MissingUpstreamTarball("test_tarball")


class _TouchUpstreamProvider(UpstreamProvider):
    """For tests"""

    def __init__(self, desired_tarball_name):
        self.desired_tarball_name = desired_tarball_name

    def provide(self, target_dir):
        f = open(os.path.join(target_dir, self.desired_tarball_name), "wb")
        f.write("I am a tarball, honest\n")
        f.close()

class _SimpleUpstreamProvider(UpstreamProvider):
    """For tests"""

    def __init__(self, package, version, store_dir):
        self.package = package
        self.version = version
        self.store_dir = store_dir

    def provide(self, target_dir):
        if self.already_exists_in_target(target_dir) \
            or self.provide_from_store_dir(target_dir):
            return os.path.join(target_dir, self._tarball_name())
        raise MissingUpstreamTarball(self._tarball_name())