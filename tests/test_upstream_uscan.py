#    test_upstream_uscan.py -- Test getting the upstream source from uscan
#    Copyright (C) 2012 Jelmer Vernooij
#
#    This file is part of brz-debian
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

"""Tests for the upstream uscan module."""

import os
import tempfile

from ....transport import NoSuchFile
from ....tests import (
    TestCase,
    TestCaseWithTransport,
)
from ..upstream.uscan import (
    UScanSource,
    UScanError,
    _xml_report_extract_target_paths,
    _xml_report_extract_upstream_version,
    _xml_report_extract_warnings,
)

# Stand-in for uscan: the component is only found without a version option
FAKE_USCAN = """#!/bin/sh
dest=
version=
for arg in "$@"; do
    case $arg in
        --destdir=*) dest=${arg#--destdir=};;
        --download-debversion=*|--download-version=*) version=1;;
    esac
done
echo "$*" >> "%(log)s"
echo "<dehs>"
echo main > "$dest/foo_1.0.orig.tar.gz"
echo "<target-path>$dest/foo_1.0.orig.tar.gz</target-path>"
if [ -z "$version" ] && [ -z "%(never)s" ]; then
    echo bar > "$dest/foo_1.0.orig-bar.tar.gz"
    echo "<component id=\"bar\">"
    echo "<component-target-path>$dest/foo_1.0.orig-bar.tar.gz</component-target-path>"
    echo "</component>"
else
    echo "<warnings>no matching files for version 1.0 in watch line</warnings>"
fi
echo "</dehs>"
"""

WATCH = """version=4
https://example.com/foo-(.*).tar.gz
"""

WATCH_COMPONENT = (
    WATCH
    + """opts="component=bar" https://example.com/bar-(.*).tar.gz ignore
"""
)


class UScanSourceTests(TestCaseWithTransport):
    def setUp(self):
        super().setUp()
        self.tree = self.make_branch_and_tree(".")

    def test_export_watchfile_none(self):
        src = UScanSource(self.tree, "", False)
        self.assertRaises(NoSuchFile, src._export_file, "watch", self.test_dir)

    def test_export_watchfile_top_level(self):
        src = UScanSource(self.tree, "", True)
        self.build_tree(["watch"])
        self.tree.add(["watch"])
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNot(src._export_file("watch", tmpdir), None)

    def test_export_watchfile(self):
        src = UScanSource(self.tree, "", False)
        self.build_tree(["debian/", "debian/watch"])
        self.tree.smart_add(["debian/watch"])
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNot(src._export_file("watch", tmpdir), None)


class UScanFetchTarballsTests(TestCaseWithTransport):
    def setUp(self):
        super().setUp()
        self.tree = self.make_branch_and_tree("tree")
        self.build_tree(["tree/debian/", "bin/", "target/"])
        self.log = os.path.join(self.test_dir, "uscan.log")

    def install_uscan(self, never=""):
        path = os.path.join(self.test_dir, "bin", "uscan")
        with open(path, "w") as f:
            f.write(FAKE_USCAN % {"log": self.log, "never": never})
        os.chmod(path, 0o755)
        self.overrideEnv(
            "PATH", os.path.join(self.test_dir, "bin") + ":" + os.environ["PATH"]
        )

    def make_source(self, watch):
        self.build_tree_contents([("tree/debian/watch", watch)])
        self.tree.smart_add(["tree/debian/watch"])
        return UScanSource(self.tree, "", False)

    def version_args(self):
        with open(self.log) as f:
            return [
                [arg for arg in line.split() if arg.startswith("--download-")]
                for line in f
            ]

    def test_no_component(self):
        self.install_uscan()
        src = self.make_source(WATCH)
        self.assertEqual(
            ["target/foo_1.0.orig.tar.gz"], src.fetch_tarballs("foo", "1.0", "target")
        )
        self.assertEqual([["--download-debversion=1.0"]], self.version_args())

    def test_watch_without_version(self):
        self.install_uscan()
        src = self.make_source("https://example.com/foo-(.*).tar.gz\n")
        self.assertEqual(
            ["target/foo_1.0.orig.tar.gz"], src.fetch_tarballs("foo", "1.0", "target")
        )
        self.assertEqual([["--download-debversion=1.0"]], self.version_args())

    def test_component(self):
        self.install_uscan()
        src = self.make_source(WATCH_COMPONENT)
        self.assertEqual(
            ["target/foo_1.0.orig.tar.gz", "target/foo_1.0.orig-bar.tar.gz"],
            src.fetch_tarballs("foo", "1.0", "target"),
        )
        self.assertEqual([[]], self.version_args())
        self.assertPathExists("target/foo_1.0.orig-bar.tar.gz")

    def test_component_not_found(self):
        self.install_uscan(never="1")
        src = self.make_source(WATCH_COMPONENT)
        self.assertRaises(UScanError, src.fetch_tarballs, "foo", "1.0", "target")
        self.assertEqual(
            [[], ["--download-debversion=1.0"], ["--download-version=1.0"]],
            self.version_args(),
        )

    def test_component_other_version(self):
        self.install_uscan()
        src = self.make_source(WATCH_COMPONENT)
        self.assertRaises(UScanError, src.fetch_tarballs, "foo", "0.9", "target")
        self.assertEqual(3, len(self.version_args()))
        self.assertEqual([], os.listdir("target"))


class UScanOutputParsingTests(TestCase):
    def test__xml_report_extract_target_paths(self):
        text = b"""
<dehs>
<package>foo</package>
<upstream-version>1.0</upstream-version>
<target>foo_1.0.orig.tar.gz</target>
<target-path>/tmp/foo_1.0.orig.tar.gz</target-path>
<component id="bar">
  <component-upstream-version>0.2</component-upstream-version>
  <component-target>foo_1.0.orig-bar.tar.gz</component-target>
  <component-target-path>/tmp/foo_1.0.orig-bar.tar.gz</component-target-path>
</component>
</dehs>"""
        self.assertEqual(
            ["/tmp/foo_1.0.orig.tar.gz", "/tmp/foo_1.0.orig-bar.tar.gz"],
            _xml_report_extract_target_paths(text),
        )

    def test__xml_report_extract_upstream_version(self):
        text = b"""
<dehs>
<package>tdb</package>
<debian-uversion>1.2.8</debian-uversion>
<debian-mangled-uversion>1.2.8</debian-mangled-uversion>
<upstream-version>1.2.9</upstream-version>
<upstream-url>ftp://ftp.samba.org/pub/tdb/tdb-1.2.9.tar.gz</upstream-url>
<status>Newer version available</status>
</dehs>"""
        self.assertEqual("1.2.9", _xml_report_extract_upstream_version(text))

    def test__xml_report_extract_warnings(self):
        text = b"""
<dehs>
<package>tdb</package>
<debian-uversion>1.2.8</debian-uversion>
<debian-mangled-uversion>1.2.8</debian-mangled-uversion>
<upstream-version>1.2.9</upstream-version>
<upstream-url>ftp://ftp.samba.org/pub/tdb/tdb-1.2.9.tar.gz</upstream-url>
<status>Newer version available</status>
<warnings>this is a warning
with a newline</warnings>
</dehs>"""
        self.assertEqual(
            ["this is a warning\nwith a newline"],
            list(_xml_report_extract_warnings(text)),
        )

    def test__xml_report_extract_upstream_version_warnings(self):
        text = b"""
<dehs>
<package>tdb</package>
<warnings>uscan warning: Unable to determine current version
in debian/watch, skipping:
ftp://ftp.samba.org/pub/tdb/tdb-(.+).tar.gz</warnings>
</dehs>
"""
        self.assertIs(None, _xml_report_extract_upstream_version(text))

    def test__xml_report_extract_upstream_version_noise(self):
        text = b"""
<dehs>
blahf =>
<package>tdb</package>
<debian-uversion>1.2.8</debian-uversion>
<upstream-version>1.2.9</upstream-version>
<warnings>uscan warning: Unable to determine current version
in debian/watch, skipping:
ftp://ftp.samba.org/pub/tdb/tdb-(.+).tar.gz</warnings>
</dehs>
"""
        self.assertEqual("1.2.9", _xml_report_extract_upstream_version(text))

    def test__xml_report_extract_upstream_version_errors(self):
        text = b"""
<dehs>
blahf =>
<package>tdb</package>
<debian-uversion>1.2.8</debian-uversion>
<errors>something something signature</errors>
</dehs>
"""
        self.assertRaises(UScanError, _xml_report_extract_upstream_version, text)
