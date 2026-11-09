#    test_merge_upstream.py -- Testsuite for builddeb's upstream merging.
#    Copyright (C) 2007 James Westby <jw+debian@jameswestby.net>
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
#

import os
import pathlib
import tarfile

from debian.changelog import Changelog, Version

from ....tests import (
    TestCaseWithTransport,
)

from ..merge_upstream import (
    changelog_add_new_version,
    get_tarballs,
)


class ChangelogAddNewVersionTests(TestCaseWithTransport):
    def test_add_new(self):
        tree = self.make_branch_and_tree(".")
        tree.lock_write()
        self.addCleanup(tree.unlock)
        tree.mkdir("debian")
        changelog_add_new_version(tree, "", "1.0", "sid", None, "somepkg")
        # changelog_add_new_version will version the changelog if it was
        # created
        with open("debian/changelog", "rb") as f:
            cl = Changelog(f)
        self.assertEqual(cl._blocks[0].package, "somepkg")
        self.assertEqual(cl._blocks[0].distributions, "UNRELEASED")
        self.assertEqual(cl._blocks[0].version, Version("1.0-1"))
        self.assertEqual([], list(tree.filter_unversioned_files(["debian/changelog"])))


def make_orig_tarball(path):
    os.mkdir("foo-1.0")
    with open("foo-1.0/hello.c", "w") as f:
        f.write("int main() {}\n")
    with tarfile.open(path, "w:gz") as tf:
        tf.add("foo-1.0")
    return os.path.abspath(path)


class GetTarballsTests(TestCaseWithTransport):
    def get_tarballs(self, source_format=None):
        tree = self.make_branch_and_tree("tree")
        if source_format is not None:
            self.build_tree_contents(
                [
                    ("tree/debian/",),
                    ("tree/debian/source/",),
                    ("tree/debian/source/format", source_format),
                ]
            )
        tarball = make_orig_tarball("foo-1.0.tar.gz")
        return get_tarballs("orig", tree, "foo", "1.0", [pathlib.Path(tarball)])

    def test_path_location(self):
        self.assertEqual(
            [os.path.abspath("tree/orig/foo_1.0.orig.tar.gz")], self.get_tarballs()
        )

    def test_path_location_v3(self):
        self.assertEqual(
            [os.path.abspath("tree/orig/foo_1.0.orig.tar.gz")],
            self.get_tarballs("3.0 (quilt)\n"),
        )
