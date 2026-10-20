#    test_vcs_up_to_date.py -- Testsuite for vcs_up_to_date
#
#    This file is part of breezy-debian.
#
#    breezy-debian is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    breezy-debian is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with breezy-debian; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#

from ..vcs_up_to_date import check_up_to_date
from . import TestCaseWithTransport
from .test_upstream import MockApt


class CheckUpToDateTests(TestCaseWithTransport):
    def make_changelog_tree(self, versions):
        tree = self.make_branch_and_tree(".")
        changelog = "".join(
            "robotfindskitten (%s) unstable; urgency=medium\n\n"
            "  * Upload.\n\n"
            " -- A Maintainer <maint@example.com>  Mon, 01 Jan 2024 00:00:00 +0000\n\n"
            % version
            for version in versions
        )
        self.build_tree_contents([("debian/",), ("debian/changelog", changelog)])
        tree.add(["debian", "debian/changelog"])
        return tree

    def test_old_entry_with_invalid_version(self):
        tree = self.make_changelog_tree(["2.8284271.702-2", "v1600005_344b-1"])
        apt = MockApt([{"Package": "robotfindskitten", "Version": "2.8284271.702-2"}])
        self.assertIsNone(check_up_to_date(tree, "", apt))

    def test_newest_entry_with_invalid_version(self):
        tree = self.make_changelog_tree(["v1600005_344b-1", "2.8284271.702-2"])
        apt = MockApt([{"Package": "robotfindskitten", "Version": "2.8284271.702-2"}])
        self.assertRaises(ValueError, check_up_to_date, tree, "", apt)
