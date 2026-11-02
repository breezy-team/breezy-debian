# Copyright (C) 2010 Canonical Limited
# vim: ts=4 sts=4 sw=4
#
# This file is part of bzr-builddeb.
#
# bzr-builddeb is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# bzr-builddeb is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with bzr-builddeb; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""Tests for builddeb.tagging."""

import pathlib

from .. import tagging, tree_debian_tag_name
from ..release import release
from ....tests import (
    TestCase,
    TestCaseWithTransport,
)


class TestDebVersionSort(TestCase):
    def test_sort(self):
        tags = [("1.0", "revid"), ("1.0.1", "revid"), ("1.0~1", "revid")]
        tagging.sort_debversion(None, tags)
        self.assertEqual(
            tags, [("1.0~1", "revid"), ("1.0", "revid"), ("1.0.1", "revid")]
        )


CHANGELOG = """\
foo (1.0-1) %s; urgency=medium

  * Initial release.

 -- Joe Tester <joe@example.com>  Mon, 01 Jan 2024 00:00:00 +0000
"""


class TestPathSubpath(TestCaseWithTransport):
    def make_package(self, distribution, changelog="debian/changelog", extra=()):
        tree = self.make_branch_and_tree(".")
        contents = [("debian/",), (changelog, CHANGELOG % distribution)]
        contents.extend(extra)
        self.build_tree_contents(contents)
        tree.add([c[0].rstrip("/") for c in contents])
        tree.commit("Initial commit")
        return tree

    def test_tree_debian_tag_name(self):
        tree = self.make_package("unstable")
        self.assertEqual(
            "debian/1.0-1",
            tree_debian_tag_name(tree, tree.branch, subpath=pathlib.Path("")),
        )

    def test_tree_debian_tag_name_merge_mode(self):
        tree = self.make_package(
            "unstable",
            changelog="changelog",
            extra=[
                (".bzr-builddeb/",),
                (".bzr-builddeb/default.conf", "[BUILDDEB]\nmerge = True\n"),
            ],
        )
        self.assertEqual(
            "debian/1.0-1",
            tree_debian_tag_name(tree, tree.branch, subpath=pathlib.Path("")),
        )

    def test_release(self):
        tree = self.make_package("UNRELEASED")
        self.assertEqual("debian/1.0-1", release(tree, pathlib.Path("")))
        self.assertIn("debian/1.0-1", tree.branch.tags.get_tag_dict())
