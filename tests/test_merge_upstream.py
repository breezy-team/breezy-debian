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

import sys
from unittest import mock

from debian.changelog import Changelog, Version

from ....tests import (
    TestCaseWithTransport,
)

from ..merge_upstream import (
    changelog_add_new_version,
    get_upstream_branch_location,
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


class _Config:
    def __init__(self, upstream_branch=None):
        self.upstream_branch = upstream_branch


class GetUpstreamBranchLocationTests(TestCaseWithTransport):
    def setUp(self):
        super().setUp()
        # A module that is None in sys.modules cannot be imported, as if
        # it was not installed.
        patcher = mock.patch.dict(
            sys.modules,
            {
                "lintian_brush": None,
                "lintian_brush.vcs": None,
                "upstream_ontologist": None,
                "upstream_ontologist.guess": None,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_configured(self):
        tree = self.make_branch_and_tree(".")
        self.assertEqual(
            ("https://example.com/foo.git", None),
            get_upstream_branch_location(
                tree, "", _Config("https://example.com/foo.git")
            ),
        )

    def test_configured_is_sanitized(self):
        tree = self.make_branch_and_tree(".")
        self.assertEqual(
            ("https://salsa.debian.org/jelmer/dulwich", None),
            get_upstream_branch_location(
                tree, "", _Config("git://salsa.debian.org/jelmer/dulwich")
            ),
        )

    def test_configured_keeps_credentials(self):
        tree = self.make_branch_and_tree(".")
        self.assertEqual(
            ("https://user:secret@github.com/jelmer/example", None),
            get_upstream_branch_location(
                tree, "", _Config("https://user:secret@github.com/jelmer/example")
            ),
        )

    def test_guessed_without_upstream_ontologist(self):
        tree = self.make_branch_and_tree(".")
        self.assertEqual(
            (None, None), get_upstream_branch_location(tree, "", _Config())
        )
