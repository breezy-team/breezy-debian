#    test_new_upstream.py -- Tests for merging new upstream versions
#    Copyright (C) 2026 Zainab Mehmut
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import tarfile

from ....tests import TestCaseWithTransport
from .. import new_upstream


class MergeUpstreamSubpathTests(TestCaseWithTransport):
    """merge_upstream with an upstream branch location that has a subpath."""

    def setUp(self):
        super().setUp()
        self.upstream = self.make_branch_and_tree("upstream")
        self.build_tree_contents(
            [
                ("upstream/sub/",),
                ("upstream/sub/hello", "1.0\n"),
                ("upstream/other/",),
                ("upstream/other/file", "x\n"),
            ]
        )
        self.upstream.add(["sub", "sub/hello", "other", "other/file"])
        revid1 = self.upstream.commit("one")
        self.upstream.branch.tags.set_tag("1.0", revid1)
        self.build_tree_contents([("upstream/sub/hello", "2.0\n")])
        self.upstream.branch.tags.set_tag("2.0", self.upstream.commit("two"))
        self.overrideAttr(
            new_upstream,
            "get_upstream_branch_location",
            lambda tree, subpath, config, trust_package=False: (
                self.upstream.branch.base + " [sub]",
                None,
            ),
        )
        # The packaging branch has the upstream 1.0 history, so upstream
        # history would normally be included.
        self.tree = self.upstream.controldir.sprout(
            "package", revision_id=revid1
        ).open_workingtree()
        self.tree.remove(["sub", "other"], keep_files=False)
        self.build_tree_contents([("package/hello", "1.0\n")])
        self.tree.add(["hello"])
        self.tree.branch.tags.set_tag(
            "upstream-1.0", self.tree.commit("Import upstream 1.0")
        )
        self.build_tree_contents(
            [
                ("package/debian/",),
                ("package/debian/source/",),
                ("package/debian/source/format", "3.0 (quilt)\n"),
                (
                    "package/debian/changelog",
                    (
                        "foo (1.0-1) unstable; urgency=medium\n\n"
                        "  * Initial release.\n\n"
                        " -- J. Maintainer <maint@example.com>  "
                        "Wed, 21 Oct 2026 12:00:00 +0000\n"
                    ),
                ),
                (
                    "package/debian/control",
                    (
                        "Source: foo\nMaintainer: J. Maintainer <maint@example.com>\n\n"
                        "Package: foo\nArchitecture: all\n"
                    ),
                ),
            ]
        )
        self.tree.add(
            [
                "debian",
                "debian/source",
                "debian/source/format",
                "debian/changelog",
                "debian/control",
            ]
        )
        self.tree.commit("packaging")

    def assertUpstreamImport(self, result, contents):
        [(_, _, revid, _, _)] = result.imported_revisions
        with self.tree.branch.lock_read():
            rev_tree = self.tree.branch.repository.revision_tree(revid)
            self.assertEqual(["hello"], [p for p, _, _, _ in rev_tree.list_files()])
            self.assertEqual(contents, rev_tree.get_file_text("hello"))
            parent_ids = rev_tree.get_parent_ids()
        self.assertFalse(result.include_upstream_history)
        self.assertFalse(
            set(self.upstream.branch.repository.all_revision_ids()) & set(parent_ids)
        )

    def merge_release(self, **kwargs):
        self.build_tree_contents([("foo-2.0/",), ("foo-2.0/hello", "2.0\n")])
        with tarfile.open("foo-2.0.tar.gz", "w:gz") as tf:
            tf.add("foo-2.0")
        with self.tree.lock_write():
            result = new_upstream.merge_upstream(
                self.tree, location=self.test_dir + "/foo-2.0.tar.gz", **kwargs
            )
        self.assertEqual("2.0", str(result.new_upstream_version))
        self.assertUpstreamImport(result, b"2.0\n")

    def test_release(self):
        self.merge_release()

    def test_release_include_upstream_history(self):
        self.merge_release(include_upstream_history=True)

    def test_snapshot(self):
        self.build_tree_contents([("upstream/sub/hello", "2.1\n")])
        self.upstream.commit("three")
        with self.tree.lock_write():
            result = new_upstream.merge_upstream(self.tree, version_kind="snapshot")
        self.assertEqual("2.0+bzr3", str(result.new_upstream_version))
        self.assertUpstreamImport(result, b"2.1\n")
