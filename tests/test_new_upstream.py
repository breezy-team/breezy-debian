#    test_new_upstream.py -- Tests for importing new upstream versions.
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

from debian.changelog import Version

from ....tests import (
    TestCaseWithTransport,
)
from .. import new_upstream
from ..import_dsc import UpstreamAlreadyImported


class FindNewUpstreamTests(TestCaseWithTransport):
    def test_upstream_already_imported(self):
        self.overrideAttr(
            new_upstream,
            "get_upstream_branch_location",
            lambda *args, **kwargs: (None, None),
        )
        tree = self.make_branch_and_tree(".")
        e = self.assertRaises(
            UpstreamAlreadyImported,
            new_upstream.find_new_upstream,
            tree,
            "",
            None,
            "foo",
            location="foo-1.0.tar.gz",
            old_upstream_version=Version("1.0"),
            new_upstream_version="1.0",
        )
        self.assertEqual("1.0", e.version)
        self.assertEqual('Upstream version "1.0" has already been imported.', str(e))

    def test_upstream_already_imported_with_tag(self):
        e = UpstreamAlreadyImported("1.0", "upstream/1.0")
        self.assertEqual(
            'Upstream version "1.0" has already been imported (tag: upstream/1.0).',
            str(e),
        )
