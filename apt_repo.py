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

import subprocess

from debian.deb822 import Dsc

from breezy.trace import note


class NoAptSources(Exception):
    """No apt sources were configured."""


class AptSourceError(Exception):
    """An error occured while running 'apt source'."""

    def __init__(self, reason):
        self.reason = reason


class Apt:

    def iter_source_by_name(self, source_name):
        for source in self.iter_sources():
            if source['Package'] == source_name:
                yield source

    def iter_sources(self):
        raise NotImplementedError(self.iter_sources)

    def iter_binaries(self, binary_name):
        raise NotImplementedError(self.iter_binaries)

    def retrieve_source(self, source_name, target_directory, source_version=None):
        raise NotImplementedError(self.retrieve_source)


class LocalApt(Apt):

    def __init__(self):
        self._apt_pkg = None

    @property
    def apt_pkg(self):
        if self._apt_pkg is not None:
            return self._apt_pkg
        try:
            import apt_pkg
        except ImportError as e:
            raise DependencyNotPresent('apt_pkg', e)
        else:
            apt_pkg.init()
        self._apt_pkg = apt_pkg
        return self._apt_pkg

    def iter_sources(self):
        try:
            sources = self.apt_pkg.SourceRecords()
        except SystemError:
            raise NoAptSources()

        sources.restart()
        while sources.step():
            yield Dsc(sources.record)

    def iter_source_by_name(self, source_name):
        try:
            sources = self.apt_pkg.SourceRecords()
        except SystemError:
            raise NoAptSources()

        sources.restart()
        while sources.lookup(source_name):
            yield Dsc(sources.record)

    def iter_binaries(self, binary_name):
        try:
            import apt
        except ImportError as e:
            raise DependencyNotPresent('apt', e)

        cache = apt.Cache()
        for pkg in cache:
            for version in pkg.versions:
                yield version._records

    def retrieve_source(self, package_name, target, source_version=None):
        self._run_apt_source(package, sources.version, target_dir)

    def _get_command(self, package, version_str=None):
        return ['apt', 'source', '-y', '--only-source', '--tar-only',
                ('%s=%s' % (package, version_str))
                if version_str is not None else package]

    def _run_apt_source(self, package, version_str, target_dir):
        command = self._get_command(package, version_str)
        try:
            subprocess.run(
                command, cwd=target_dir, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.splitlines()
            if stderr[-1] == (
                b"E: You must put some 'source' URIs in your sources.list"
            ):
                raise NoAptSources()
            CS = b"\x1b[1;31mE: \x1b[0m"
            CE = b"\x1b[0m"
            if stderr[-1] == (
                CS + b"You must put some 'deb-src' URIs in your sources.list" +
                CE
            ):
                raise NoAptSources()
            if stderr[-1].startswith(b"E: "):
                raise AptSourceError(stderr[-1][3:].decode())
            if stderr[-1].startswith(CS):
                raise AptSourceError(stderr[-1][len(CS): -len(CE)].decode())
            raise AptSourceError(
                [line.decode("utf-8", "surrogateescape") for line in stderr]
            )


class RemoteApt(Apt):

    def __init__(self, mirror_uri, distribution=None, components=None):
        self.mirror_uri = mirror_uri
        self.distribution = distribution
        self.components = components

    @classmethod
    def from_string(cls, text):
        (mirror_uri, distribution, rest) = text.split(' ', 3)
        return cls(mirror_uri, distribution, rest.split())