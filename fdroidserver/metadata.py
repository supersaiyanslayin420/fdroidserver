# -*- coding: utf-8 -*-
#
# metadata.py - part of the FDroid server tools
# Copyright (C) 2013, Ciaran Gultnieks, ciaran@ciarang.com
# Copyright (C) 2013-2014 Daniel Martí <mvdan@mvdan.cc>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import os
import re
import sys
import glob
import cgi
import logging
import textwrap

import yaml
# use libyaml if it is available
try:
    from yaml import CLoader
    YamlLoader = CLoader
except ImportError:
    from yaml import Loader
    YamlLoader = Loader

# use the C implementation when available
import xml.etree.cElementTree as ElementTree

import common

srclibs = None


class MetaDataException(Exception):

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value

# To filter which ones should be written to the metadata files if
# present
app_fields = set([
    'Disabled',
    'AntiFeatures',
    'Provides',
    'Categories',
    'License',
    'Web Site',
    'Source Code',
    'Issue Tracker',
    'Changelog',
    'Donate',
    'FlattrID',
    'Bitcoin',
    'Litecoin',
    'Name',
    'Auto Name',
    'Summary',
    'Description',
    'Requires Root',
    'Repo Type',
    'Repo',
    'Binaries',
    'Maintainer Notes',
    'Archive Policy',
    'Auto Update Mode',
    'Update Check Mode',
    'Update Check Ignore',
    'Vercode Operation',
    'Update Check Name',
    'Update Check Data',
    'Current Version',
    'Current Version Code',
    'No Source Since',

    'comments',  # For formats that don't do inline comments
    'builds',    # For formats that do builds as a list
])


class App():

    def __init__(self):
        self.Disabled = None
        self.AntiFeatures = []
        self.Provides = None
        self.Categories = ['None']
        self.License = 'Unknown'
        self.WebSite = ''
        self.SourceCode = ''
        self.IssueTracker = ''
        self.Changelog = ''
        self.Donate = None
        self.FlattrID = None
        self.Bitcoin = None
        self.Litecoin = None
        self.Name = None
        self.AutoName = ''
        self.Summary = ''
        self.Description = []
        self.RequiresRoot = False
        self.RepoType = ''
        self.Repo = ''
        self.Binaries = None
        self.MaintainerNotes = []
        self.ArchivePolicy = None
        self.AutoUpdateMode = 'None'
        self.UpdateCheckMode = 'None'
        self.UpdateCheckIgnore = None
        self.VercodeOperation = None
        self.UpdateCheckName = None
        self.UpdateCheckData = None
        self.CurrentVersion = ''
        self.CurrentVersionCode = '0'
        self.NoSourceSince = ''

        self.id = None
        self.metadatapath = None
        self.builds = []
        self.comments = {}
        self.added = None
        self.lastupdated = None

    # Translates human-readable field names to attribute names, e.g.
    # 'Auto Name' to 'AutoName'
    @classmethod
    def field_to_attr(cls, f):
        return f.replace(' ', '')

    # Translates attribute names to human-readable field names, e.g.
    # 'AutoName' to 'Auto Name'
    @classmethod
    def attr_to_field(cls, k):
        if k in app_fields:
            return k
        f = re.sub(r'([a-z])([A-Z])', r'\1 \2', k)
        return f

    # Constructs an old-fashioned dict with the human-readable field
    # names. Should only be used for tests.
    def field_dict(self):
        d = {}
        for k, v in self.__dict__.iteritems():
            if k == 'builds':
                d['builds'] = []
                for build in v:
                    d['builds'].append(build.__dict__)
            else:
                k = App.attr_to_field(k)
                d[k] = v
        return d

    # Gets the value associated to a field name, e.g. 'Auto Name'
    def get_field(self, f):
        if f not in app_fields:
            raise MetaDataException('Unrecognised app field: ' + f)
        k = App.field_to_attr(f)
        return getattr(self, k)

    # Sets the value associated to a field name, e.g. 'Auto Name'
    def set_field(self, f, v):
        if f not in app_fields:
            raise MetaDataException('Unrecognised app field: ' + f)
        k = App.field_to_attr(f)
        self.__dict__[k] = v

    # Appends to the value associated to a field name, e.g. 'Auto Name'
    def append_field(self, f, v):
        if f not in app_fields:
            raise MetaDataException('Unrecognised app field: ' + f)
        k = App.field_to_attr(f)
        if k not in self.__dict__:
            self.__dict__[k] = [v]
        else:
            self.__dict__[k].append(v)

    # Like dict.update(), but using human-readable field names
    def update_fields(self, d):
        for f, v in d.iteritems():
            if f == 'builds':
                for b in v:
                    build = Build()
                    build.update_flags(b)
                    self.builds.append(build)
            else:
                self.set_field(f, v)


def metafieldtype(name):
    if name in ['Description', 'Maintainer Notes']:
        return 'multiline'
    if name in ['Categories', 'AntiFeatures']:
        return 'list'
    if name == 'Build Version':
        return 'build'
    if name == 'Build':
        return 'buildv2'
    if name == 'Use Built':
        return 'obsolete'
    if name not in app_fields:
        return 'unknown'
    return 'string'


# In the order in which they are laid out on files
build_flags_order = [
    'disable',
    'commit',
    'subdir',
    'submodules',
    'init',
    'patch',
    'gradle',
    'maven',
    'kivy',
    'output',
    'srclibs',
    'oldsdkloc',
    'encoding',
    'forceversion',
    'forcevercode',
    'rm',
    'extlibs',
    'prebuild',
    'update',
    'target',
    'scanignore',
    'scandelete',
    'build',
    'buildjni',
    'ndk',
    'preassemble',
    'gradleprops',
    'antcommands',
    'novcheck',
]


build_flags = set(build_flags_order + ['version', 'vercode'])


class Build():

    def __init__(self):
        self.disable = False
        self.commit = None
        self.subdir = None
        self.submodules = False
        self.init = ''
        self.patch = []
        self.gradle = False
        self.maven = False
        self.kivy = False
        self.output = None
        self.srclibs = []
        self.oldsdkloc = False
        self.encoding = None
        self.forceversion = False
        self.forcevercode = False
        self.rm = []
        self.extlibs = []
        self.prebuild = ''
        self.update = None
        self.target = None
        self.scanignore = []
        self.scandelete = []
        self.build = ''
        self.buildjni = []
        self.ndk = None
        self.preassemble = []
        self.gradleprops = []
        self.antcommands = None
        self.novcheck = False

    def get_flag(self, f):
        if f not in build_flags:
            raise MetaDataException('Unrecognised build flag: ' + f)
        return getattr(self, f)

    def set_flag(self, f, v):
        if f == 'versionName':
            f = 'version'
        if f == 'versionCode':
            f = 'vercode'
        if f not in build_flags:
            raise MetaDataException('Unrecognised build flag: ' + f)
        setattr(self, f, v)

    def append_flag(self, f, v):
        if f not in build_flags:
            raise MetaDataException('Unrecognised build flag: ' + f)
        if f not in self.__dict__:
            self.__dict__[f] = [v]
        else:
            self.__dict__[f].append(v)

    def method(self):
        for f in ['maven', 'gradle', 'kivy']:
            if self.get_flag(f):
                return f
        if self.output:
            return 'raw'
        return 'ant'

    def ndk_path(self):
        version = self.ndk
        if not version:
            version = 'r10e'  # falls back to latest
        paths = common.config['ndk_paths']
        if version not in paths:
            return ''
        return paths[version]

    def update_flags(self, d):
        for f, v in d.iteritems():
            self.set_flag(f, v)


def flagtype(name):
    if name in ['extlibs', 'srclibs', 'patch', 'rm', 'buildjni', 'preassemble',
                'update', 'scanignore', 'scandelete', 'gradle', 'antcommands',
                'gradleprops']:
        return 'list'
    if name in ['init', 'prebuild', 'build']:
        return 'script'
    if name in ['submodules', 'oldsdkloc', 'forceversion', 'forcevercode',
                'novcheck']:
        return 'bool'
    return 'string'


# Designates a metadata field type and checks that it matches
#
# 'name'     - The long name of the field type
# 'matching' - List of possible values or regex expression
# 'sep'      - Separator to use if value may be a list
# 'fields'   - Metadata fields (Field:Value) of this type
# 'flags'    - Build flags (flag=value) of this type
#
class FieldValidator():

    def __init__(self, name, matching, sep, fields, flags):
        self.name = name
        self.matching = matching
        if type(matching) is str:
            self.compiled = re.compile(matching)
        self.sep = sep
        self.fields = fields
        self.flags = flags

    def _assert_regex(self, values, appid):
        for v in values:
            if not self.compiled.match(v):
                raise MetaDataException("'%s' is not a valid %s in %s. "
                                        % (v, self.name, appid) +
                                        "Regex pattern: %s" % (self.matching))

    def _assert_list(self, values, appid):
        for v in values:
            if v not in self.matching:
                raise MetaDataException("'%s' is not a valid %s in %s. "
                                        % (v, self.name, appid) +
                                        "Possible values: %s" % (", ".join(self.matching)))

    def check(self, v, appid):
        if type(v) is not str or not v:
            return
        if self.sep is not None:
            values = v.split(self.sep)
        else:
            values = [v]
        if type(self.matching) is list:
            self._assert_list(values, appid)
        else:
            self._assert_regex(values, appid)


# Generic value types
valuetypes = {
    FieldValidator("Integer",
                   r'^[1-9][0-9]*$', None,
                   [],
                   ['vercode']),

    FieldValidator("Hexadecimal",
                   r'^[0-9a-f]+$', None,
                   ['FlattrID'],
                   []),

    FieldValidator("HTTP link",
                   r'^http[s]?://', None,
                   ["Web Site", "Source Code", "Issue Tracker", "Changelog", "Donate"], []),

    FieldValidator("Bitcoin address",
                   r'^[a-zA-Z0-9]{27,34}$', None,
                   ["Bitcoin"],
                   []),

    FieldValidator("Litecoin address",
                   r'^L[a-zA-Z0-9]{33}$', None,
                   ["Litecoin"],
                   []),

    FieldValidator("bool",
                   r'([Yy]es|[Nn]o|[Tt]rue|[Ff]alse)', None,
                   ["Requires Root"],
                   ['submodules', 'oldsdkloc', 'forceversion', 'forcevercode',
                    'novcheck']),

    FieldValidator("Repo Type",
                   ['git', 'git-svn', 'svn', 'hg', 'bzr', 'srclib'], None,
                   ["Repo Type"],
                   []),

    FieldValidator("Binaries",
                   r'^http[s]?://', None,
                   ["Binaries"],
                   []),

    FieldValidator("Archive Policy",
                   r'^[0-9]+ versions$', None,
                   ["Archive Policy"],
                   []),

    FieldValidator("Anti-Feature",
                   ["Ads", "Tracking", "NonFreeNet", "NonFreeDep", "NonFreeAdd", "UpstreamNonFree"], ',',
                   ["AntiFeatures"],
                   []),

    FieldValidator("Auto Update Mode",
                   r"^(Version .+|None)$", None,
                   ["Auto Update Mode"],
                   []),

    FieldValidator("Update Check Mode",
                   r"^(Tags|Tags .+|RepoManifest|RepoManifest/.+|RepoTrunk|HTTP|Static|None)$", None,
                   ["Update Check Mode"],
                   [])
}


# Check an app's metadata information for integrity errors
def check_metadata(app):
    for v in valuetypes:
        for f in v.fields:
            v.check(app.get_field(f), app.id)
        for build in app.builds:
            for f in v.flags:
                v.check(build.get_flag(f), app.id)


# Formatter for descriptions. Create an instance, and call parseline() with
# each line of the description source from the metadata. At the end, call
# end() and then text_wiki and text_html will contain the result.
class DescriptionFormatter:
    stNONE = 0
    stPARA = 1
    stUL = 2
    stOL = 3
    bold = False
    ital = False
    state = stNONE
    text_wiki = ''
    text_html = ''
    text_txt = ''
    para_lines = []
    linkResolver = None

    def __init__(self, linkres):
        self.linkResolver = linkres

    def endcur(self, notstates=None):
        if notstates and self.state in notstates:
            return
        if self.state == self.stPARA:
            self.endpara()
        elif self.state == self.stUL:
            self.endul()
        elif self.state == self.stOL:
            self.endol()

    def endpara(self):
        self.state = self.stNONE
        whole_para = ' '.join(self.para_lines)
        self.addtext(whole_para)
        self.text_txt += textwrap.fill(whole_para, 80,
                                       break_long_words=False,
                                       break_on_hyphens=False) + '\n\n'
        self.text_html += '</p>'
        del self.para_lines[:]

    def endul(self):
        self.text_html += '</ul>'
        self.text_txt += '\n'
        self.state = self.stNONE

    def endol(self):
        self.text_html += '</ol>'
        self.text_txt += '\n'
        self.state = self.stNONE

    def formatted(self, txt, html):
        formatted = ''
        if html:
            txt = cgi.escape(txt)
        while True:
            index = txt.find("''")
            if index == -1:
                return formatted + txt
            formatted += txt[:index]
            txt = txt[index:]
            if txt.startswith("'''"):
                if html:
                    if self.bold:
                        formatted += '</b>'
                    else:
                        formatted += '<b>'
                self.bold = not self.bold
                txt = txt[3:]
            else:
                if html:
                    if self.ital:
                        formatted += '</i>'
                    else:
                        formatted += '<i>'
                self.ital = not self.ital
                txt = txt[2:]

    def linkify(self, txt):
        linkified_plain = ''
        linkified_html = ''
        while True:
            index = txt.find("[")
            if index == -1:
                return (linkified_plain + self.formatted(txt, False), linkified_html + self.formatted(txt, True))
            linkified_plain += self.formatted(txt[:index], False)
            linkified_html += self.formatted(txt[:index], True)
            txt = txt[index:]
            if txt.startswith("[["):
                index = txt.find("]]")
                if index == -1:
                    raise MetaDataException("Unterminated ]]")
                url = txt[2:index]
                if self.linkResolver:
                    url, urltext = self.linkResolver(url)
                else:
                    urltext = url
                linkified_html += '<a href="' + url + '">' + cgi.escape(urltext) + '</a>'
                linkified_plain += urltext
                txt = txt[index + 2:]
            else:
                index = txt.find("]")
                if index == -1:
                    raise MetaDataException("Unterminated ]")
                url = txt[1:index]
                index2 = url.find(' ')
                if index2 == -1:
                    urltxt = url
                else:
                    urltxt = url[index2 + 1:]
                    url = url[:index2]
                    if url == urltxt:
                        raise MetaDataException("Url title is just the URL - use [url]")
                linkified_html += '<a href="' + url + '">' + cgi.escape(urltxt) + '</a>'
                linkified_plain += urltxt
                if urltxt != url:
                    linkified_plain += ' (' + url + ')'
                txt = txt[index + 1:]

    def addtext(self, txt):
        p, h = self.linkify(txt)
        self.text_html += h

    def parseline(self, line):
        self.text_wiki += "%s\n" % line
        if not line:
            self.endcur()
        elif line.startswith('* '):
            self.endcur([self.stUL])
            self.text_txt += "%s\n" % line
            if self.state != self.stUL:
                self.text_html += '<ul>'
                self.state = self.stUL
            self.text_html += '<li>'
            self.addtext(line[1:])
            self.text_html += '</li>'
        elif line.startswith('# '):
            self.endcur([self.stOL])
            self.text_txt += "%s\n" % line
            if self.state != self.stOL:
                self.text_html += '<ol>'
                self.state = self.stOL
            self.text_html += '<li>'
            self.addtext(line[1:])
            self.text_html += '</li>'
        else:
            self.para_lines.append(line)
            self.endcur([self.stPARA])
            if self.state == self.stNONE:
                self.text_html += '<p>'
                self.state = self.stPARA

    def end(self):
        self.endcur()
        self.text_txt = self.text_txt.strip()


# Parse multiple lines of description as written in a metadata file, returning
# a single string in text format and wrapped to 80 columns.
def description_txt(lines):
    ps = DescriptionFormatter(None)
    for line in lines:
        ps.parseline(line)
    ps.end()
    return ps.text_txt


# Parse multiple lines of description as written in a metadata file, returning
# a single string in wiki format. Used for the Maintainer Notes field as well,
# because it's the same format.
def description_wiki(lines):
    ps = DescriptionFormatter(None)
    for line in lines:
        ps.parseline(line)
    ps.end()
    return ps.text_wiki


# Parse multiple lines of description as written in a metadata file, returning
# a single string in HTML format.
def description_html(lines, linkres):
    ps = DescriptionFormatter(linkres)
    for line in lines:
        ps.parseline(line)
    ps.end()
    return ps.text_html


def parse_srclib(metadatapath):

    thisinfo = {}

    # Defaults for fields that come from metadata
    thisinfo['Repo Type'] = ''
    thisinfo['Repo'] = ''
    thisinfo['Subdir'] = None
    thisinfo['Prepare'] = None

    if not os.path.exists(metadatapath):
        return thisinfo

    metafile = open(metadatapath, "r")

    n = 0
    for line in metafile:
        n += 1
        line = line.rstrip('\r\n')
        if not line or line.startswith("#"):
            continue

        try:
            f, v = line.split(':', 1)
        except ValueError:
            raise MetaDataException("Invalid metadata in %s:%d" % (line, n))

        if f == "Subdir":
            thisinfo[f] = v.split(',')
        else:
            thisinfo[f] = v

    return thisinfo


def read_srclibs():
    """Read all srclib metadata.

    The information read will be accessible as metadata.srclibs, which is a
    dictionary, keyed on srclib name, with the values each being a dictionary
    in the same format as that returned by the parse_srclib function.

    A MetaDataException is raised if there are any problems with the srclib
    metadata.
    """
    global srclibs

    # They were already loaded
    if srclibs is not None:
        return

    srclibs = {}

    srcdir = 'srclibs'
    if not os.path.exists(srcdir):
        os.makedirs(srcdir)

    for metadatapath in sorted(glob.glob(os.path.join(srcdir, '*.txt'))):
        srclibname = os.path.basename(metadatapath[:-4])
        srclibs[srclibname] = parse_srclib(metadatapath)


# Read all metadata. Returns a list of 'app' objects (which are dictionaries as
# returned by the parse_txt_metadata function.
def read_metadata(xref=True):

    # Always read the srclibs before the apps, since they can use a srlib as
    # their source repository.
    read_srclibs()

    apps = {}

    for basedir in ('metadata', 'tmp'):
        if not os.path.exists(basedir):
            os.makedirs(basedir)

    # If there are multiple metadata files for a single appid, then the first
    # file that is parsed wins over all the others, and the rest throw an
    # exception. So the original .txt format is parsed first, at least until
    # newer formats stabilize.

    for metadatapath in sorted(glob.glob(os.path.join('metadata', '*.txt'))
                               + glob.glob(os.path.join('metadata', '*.json'))
                               + glob.glob(os.path.join('metadata', '*.xml'))
                               + glob.glob(os.path.join('metadata', '*.yaml'))):
        app = parse_metadata(metadatapath)
        if app.id in apps:
            raise MetaDataException("Found multiple metadata files for " + app.id)
        check_metadata(app)
        apps[app.id] = app

    if xref:
        # Parse all descriptions at load time, just to ensure cross-referencing
        # errors are caught early rather than when they hit the build server.
        def linkres(appid):
            if appid in apps:
                return ("fdroid.app:" + appid, "Dummy name - don't know yet")
            raise MetaDataException("Cannot resolve app id " + appid)

        for appid, app in apps.iteritems():
            try:
                description_html(app.Description, linkres)
            except MetaDataException, e:
                raise MetaDataException("Problem with description of " + appid +
                                        " - " + str(e))

    return apps


def split_list_values(s):
    # Port legacy ';' separators
    l = [v.strip() for v in s.replace(';', ',').split(',')]
    return [v for v in l if v]


def get_default_app_info(metadatapath=None):
    if metadatapath is None:
        appid = None
    else:
        appid, _ = common.get_extension(os.path.basename(metadatapath))

    app = App()
    app.metadatapath = metadatapath
    if appid is not None:
        app.id = appid

    return app


def sorted_builds(builds):
    return sorted(builds, key=lambda build: int(build.vercode))


def post_metadata_parse(app):

    for f in app_fields:
        v = app.get_field(f)
        if type(v) in (float, int):
            app.set_field(f, str(v))

    # convert to the odd internal format
    for f in ('Description', 'Maintainer Notes'):
        v = app.get_field(f)
        if isinstance(v, basestring):
            text = v.rstrip().lstrip()
            app.set_field(f, text.split('\n'))

    esc_newlines = re.compile('\\\\( |\\n)')

    for build in app.builds:
        for k in build_flags:
            v = build.get_flag(k)

            if type(v) in (float, int):
                build.set_flag(k, v)
            else:
                keyflagtype = flagtype(k)

                if keyflagtype == 'script':
                    build.set_flag(k, re.sub(esc_newlines, '', v).lstrip().rstrip())
                elif keyflagtype == 'bool':
                    # TODO handle this using <xsd:element type="xsd:boolean> in a schema
                    if isinstance(v, basestring) and v == 'true':
                        build.set_flag(k, 'true')
                elif keyflagtype == 'string':
                    if isinstance(v, bool) and v:
                        build.set_flag(k, 'yes')

    if not app.Description:
        app.Description = ['No description available']

    app.builds = sorted_builds(app.builds)


# Parse metadata for a single application.
#
#  'metadatapath' - the filename to read. The package id for the application comes
#               from this filename. Pass None to get a blank entry.
#
# Returns a dictionary containing all the details of the application. There are
# two major kinds of information in the dictionary. Keys beginning with capital
# letters correspond directory to identically named keys in the metadata file.
# Keys beginning with lower case letters are generated in one way or another,
# and are not found verbatim in the metadata.
#
# Known keys not originating from the metadata are:
#
#  'builds'           - a list of dictionaries containing build information
#                       for each defined build
#  'comments'         - a list of comments from the metadata file. Each is
#                       a list of the form [field, comment] where field is
#                       the name of the field it preceded in the metadata
#                       file. Where field is None, the comment goes at the
#                       end of the file. Alternatively, 'build:version' is
#                       for a comment before a particular build version.
#  'descriptionlines' - original lines of description as formatted in the
#                       metadata file.
#


def _decode_list(data):
    '''convert items in a list from unicode to basestring'''
    rv = []
    for item in data:
        if isinstance(item, unicode):
            item = item.encode('utf-8')
        elif isinstance(item, list):
            item = _decode_list(item)
        elif isinstance(item, dict):
            item = _decode_dict(item)
        rv.append(item)
    return rv


def _decode_dict(data):
    '''convert items in a dict from unicode to basestring'''
    rv = {}
    for k, v in data.iteritems():
        if isinstance(k, unicode):
            k = k.encode('utf-8')
        if isinstance(v, unicode):
            v = v.encode('utf-8')
        elif isinstance(v, list):
            v = _decode_list(v)
        elif isinstance(v, dict):
            v = _decode_dict(v)
        rv[k] = v
    return rv


def parse_metadata(metadatapath):
    _, ext = common.get_extension(metadatapath)
    accepted = common.config['accepted_formats']
    if ext not in accepted:
        logging.critical('"' + metadatapath
                         + '" is not in an accepted format, '
                         + 'convert to: ' + ', '.join(accepted))
        sys.exit(1)

    if ext == 'txt':
        return parse_txt_metadata(metadatapath)
    if ext == 'json':
        return parse_json_metadata(metadatapath)
    if ext == 'xml':
        return parse_xml_metadata(metadatapath)
    if ext == 'yaml':
        return parse_yaml_metadata(metadatapath)

    logging.critical('Unknown metadata format: ' + metadatapath)
    sys.exit(1)


def parse_json_metadata(metadatapath):

    app = get_default_app_info(metadatapath)

    # fdroid metadata is only strings and booleans, no floats or ints. And
    # json returns unicode, and fdroidserver still uses plain python strings
    # TODO create schema using https://pypi.python.org/pypi/jsonschema
    jsoninfo = json.load(open(metadatapath, 'r'),
                         object_hook=_decode_dict,
                         parse_int=lambda s: s,
                         parse_float=lambda s: s)
    app.update_fields(jsoninfo)
    post_metadata_parse(app)

    return app


def parse_xml_metadata(metadatapath):

    app = get_default_app_info(metadatapath)

    tree = ElementTree.ElementTree(file=metadatapath)
    root = tree.getroot()

    if root.tag != 'resources':
        logging.critical(metadatapath + ' does not have root as <resources></resources>!')
        sys.exit(1)

    for child in root:
        if child.tag != 'builds':
            # builds does not have name="" attrib
            name = child.attrib['name']

        if child.tag == 'string':
            app.set_field(name, child.text)
        elif child.tag == 'string-array':
            for item in child:
                app.append_field(name, item.text)
        elif child.tag == 'builds':
            for b in child:
                build = Build()
                for key in b:
                    build.set_flag(key.tag, key.text)
                app.builds.append(build)

    # TODO handle this using <xsd:element type="xsd:boolean> in a schema
    if not isinstance(app.RequiresRoot, bool):
        if app.RequiresRoot == 'true':
            app.RequiresRoot = True
        else:
            app.RequiresRoot = False

    post_metadata_parse(app)

    return app


def parse_yaml_metadata(metadatapath):

    app = get_default_app_info(metadatapath)

    yamlinfo = yaml.load(open(metadatapath, 'r'), Loader=YamlLoader)
    app.update_fields(yamlinfo)
    post_metadata_parse(app)

    return app


def parse_txt_metadata(metadatapath):

    linedesc = None

    def add_buildflag(p, build):
        if not p.strip():
            raise MetaDataException("Empty build flag at {1}"
                                    .format(buildlines[0], linedesc))
        bv = p.split('=', 1)
        if len(bv) != 2:
            raise MetaDataException("Invalid build flag at {0} in {1}"
                                    .format(buildlines[0], linedesc))

        pk, pv = bv
        pk = pk.lstrip()
        if pk not in build_flags:
            raise MetaDataException("Unrecognised build flag at {0} in {1}"
                                    .format(p, linedesc))
        t = flagtype(pk)
        if t == 'list':
            pv = split_list_values(pv)
            if pk == 'gradle':
                if len(pv) == 1 and pv[0] in ['main', 'yes']:
                    pv = ['yes']
            build.set_flag(pk, pv)
        elif t == 'string' or t == 'script':
            build.set_flag(pk, pv)
        elif t == 'bool':
            v = pv == 'yes'
            if v:
                build.set_flag(pk, True)

        else:
            raise MetaDataException("Unrecognised build flag type '%s' at %s in %s"
                                    % (t, p, linedesc))

    def parse_buildline(lines):
        v = "".join(lines)
        parts = [p.replace("\\,", ",")
                 for p in re.split(r"(?<!\\),", v)]
        if len(parts) < 3:
            raise MetaDataException("Invalid build format: " + v + " in " + metafile.name)
        build = Build()
        build.origlines = lines
        build.version = parts[0]
        build.vercode = parts[1]
        if parts[2].startswith('!'):
            # For backwards compatibility, handle old-style disabling,
            # including attempting to extract the commit from the message
            build.disable = parts[2][1:]
            commit = 'unknown - see disabled'
            index = parts[2].rfind('at ')
            if index != -1:
                commit = parts[2][index + 3:]
                if commit.endswith(')'):
                    commit = commit[:-1]
            build.commit = commit
        else:
            build.commit = parts[2]
        for p in parts[3:]:
            add_buildflag(p, build)

        return build

    def add_comments(key):
        if not curcomments:
            return
        app.comments[key] = list(curcomments)
        del curcomments[:]

    app = get_default_app_info(metadatapath)
    metafile = open(metadatapath, "r")

    mode = 0
    buildlines = []
    curcomments = []
    build = None
    vc_seen = {}

    c = 0
    for line in metafile:
        c += 1
        linedesc = "%s:%d" % (metafile.name, c)
        line = line.rstrip('\r\n')
        if mode == 3:
            if not any(line.startswith(s) for s in (' ', '\t')):
                if not build.commit and not build.disable:
                    raise MetaDataException("No commit specified for {0} in {1}"
                                            .format(build.version, linedesc))

                app.builds.append(build)
                add_comments('build:' + build.vercode)
                mode = 0
            else:
                if line.endswith('\\'):
                    buildlines.append(line[:-1].lstrip())
                else:
                    buildlines.append(line.lstrip())
                    bl = ''.join(buildlines)
                    add_buildflag(bl, build)
                    buildlines = []

        if mode == 0:
            if not line:
                continue
            if line.startswith("#"):
                curcomments.append(line[1:].strip())
                continue
            try:
                f, v = line.split(':', 1)
            except ValueError:
                raise MetaDataException("Invalid metadata in " + linedesc)
            if f != f.strip() or v != v.strip():
                raise MetaDataException("Extra spacing found in " + linedesc)

            # Translate obsolete fields...
            if f == 'Market Version':
                f = 'Current Version'
            if f == 'Market Version Code':
                f = 'Current Version Code'

            fieldtype = metafieldtype(f)
            if fieldtype not in ['build', 'buildv2']:
                add_comments(f)
            if fieldtype == 'multiline':
                mode = 1
                if v:
                    raise MetaDataException("Unexpected text on same line as " + f + " in " + linedesc)
            elif fieldtype == 'string':
                app.set_field(f, v)
            elif fieldtype == 'list':
                app.set_field(f, split_list_values(v))
            elif fieldtype == 'build':
                if v.endswith("\\"):
                    mode = 2
                    buildlines = [v[:-1]]
                else:
                    build = parse_buildline([v])
                    app.builds.append(build)
                    add_comments('build:' + app.builds[-1].vercode)
            elif fieldtype == 'buildv2':
                build = Build()
                vv = v.split(',')
                if len(vv) != 2:
                    raise MetaDataException('Build should have comma-separated version and vercode, not "{0}", in {1}'
                                            .format(v, linedesc))
                build.version = vv[0]
                build.vercode = vv[1]
                if build.vercode in vc_seen:
                    raise MetaDataException('Duplicate build recipe found for vercode %s in %s' % (
                                            build.vercode, linedesc))
                vc_seen[build.vercode] = True
                buildlines = []
                mode = 3
            elif fieldtype == 'obsolete':
                pass        # Just throw it away!
            else:
                raise MetaDataException("Unrecognised field type for " + f + " in " + linedesc)
        elif mode == 1:     # Multiline field
            if line == '.':
                mode = 0
            else:
                app.append_field(f, line)
        elif mode == 2:     # Line continuation mode in Build Version
            if line.endswith("\\"):
                buildlines.append(line[:-1])
            else:
                buildlines.append(line)
                build = parse_buildline(buildlines)
                app.builds.append(build)
                add_comments('build:' + app.builds[-1].vercode)
                mode = 0
    add_comments(None)

    # Mode at end of file should always be 0...
    if mode == 1:
        raise MetaDataException(f + " not terminated in " + metafile.name)
    elif mode == 2:
        raise MetaDataException("Unterminated continuation in " + metafile.name)
    elif mode == 3:
        raise MetaDataException("Unterminated build in " + metafile.name)

    post_metadata_parse(app)

    return app


def write_plaintext_metadata(mf, app, w_comment, w_field, w_build):

    def w_comments(key):
        if key not in app.comments:
            return
        for line in app.comments[key]:
            w_comment(line)

    def w_field_always(f, v=None):
        if v is None:
            v = app.get_field(f)
        w_comments(f)
        w_field(f, v)

    def w_field_nonempty(f, v=None):
        if v is None:
            v = app.get_field(f)
        w_comments(f)
        if v:
            w_field(f, v)

    w_field_nonempty('Disabled')
    if app.AntiFeatures:
        w_field_always('AntiFeatures')
    w_field_nonempty('Provides')
    w_field_always('Categories')
    w_field_always('License')
    w_field_always('Web Site')
    w_field_always('Source Code')
    w_field_always('Issue Tracker')
    w_field_nonempty('Changelog')
    w_field_nonempty('Donate')
    w_field_nonempty('FlattrID')
    w_field_nonempty('Bitcoin')
    w_field_nonempty('Litecoin')
    mf.write('\n')
    w_field_nonempty('Name')
    w_field_nonempty('Auto Name')
    w_field_always('Summary')
    w_field_always('Description', description_txt(app.Description))
    mf.write('\n')
    if app.RequiresRoot:
        w_field_always('Requires Root', 'yes')
        mf.write('\n')
    if app.RepoType:
        w_field_always('Repo Type')
        w_field_always('Repo')
        if app.Binaries:
            w_field_always('Binaries')
        mf.write('\n')

    for build in sorted_builds(app.builds):

        if build.version == "Ignore":
            continue

        w_comments('build:' + build.vercode)
        w_build(build)
        mf.write('\n')

    if app.MaintainerNotes:
        w_field_always('Maintainer Notes', app.MaintainerNotes)
        mf.write('\n')

    w_field_nonempty('Archive Policy')
    w_field_always('Auto Update Mode')
    w_field_always('Update Check Mode')
    w_field_nonempty('Update Check Ignore')
    w_field_nonempty('Vercode Operation')
    w_field_nonempty('Update Check Name')
    w_field_nonempty('Update Check Data')
    if app.CurrentVersion:
        w_field_always('Current Version')
        w_field_always('Current Version Code')
    if app.NoSourceSince:
        mf.write('\n')
        w_field_always('No Source Since')
    w_comments(None)


# Write a metadata file in txt format.
#
# 'mf'      - Writer interface (file, StringIO, ...)
# 'app'     - The app data
def write_txt_metadata(mf, app):

    def w_comment(line):
        mf.write("# %s\n" % line)

    def w_field(f, v):
        t = metafieldtype(f)
        if t == 'list':
            v = ','.join(v)
        elif t == 'multiline':
            if type(v) == list:
                v = '\n' + '\n'.join(v) + '\n.'
            else:
                v = '\n' + v + '\n.'
        mf.write("%s:%s\n" % (f, v))

    def w_build(build):
        mf.write("Build:%s,%s\n" % (build.version, build.vercode))

        for f in build_flags_order:
            v = build.get_flag(f)
            if not v:
                continue

            t = flagtype(f)
            out = '    %s=' % f
            if t == 'string':
                out += v
            elif t == 'bool':
                out += 'yes'
            elif t == 'script':
                out += '&& \\\n        '.join([s.lstrip() for s in v.split('&& ')])
            elif t == 'list':
                out += ','.join(v) if type(v) == list else v

            mf.write(out)
            mf.write('\n')

    write_plaintext_metadata(mf, app, w_comment, w_field, w_build)


def write_yaml_metadata(mf, app):

    def w_comment(line):
        mf.write("# %s\n" % line)

    def escape(v):
        if not v:
            return ''
        if any(c in v for c in [': ', '%', '@', '*']):
            return "'" + v.replace("'", "''") + "'"
        return v

    def w_field(f, v, prefix='', t=None):
        if t is None:
            t = metafieldtype(f)
        v = ''
        if t == 'list':
            v = '\n'
            for e in v:
                v += prefix + ' - ' + escape(e) + '\n'
        elif t == 'multiline':
            v = ' |\n'
            lines = v
            if type(v) == str:
                lines = v.splitlines()
            for l in lines:
                if l:
                    v += prefix + '  ' + l + '\n'
                else:
                    v += '\n'
        elif t == 'bool':
            v = ' yes\n'
        elif t == 'script':
            cmds = [s + '&& \\' for s in v.split('&& ')]
            if len(cmds) > 0:
                cmds[-1] = cmds[-1][:-len('&& \\')]
            w_field(f, cmds, prefix, 'multiline')
            return
        else:
            v = ' ' + escape(v) + '\n'

        mf.write(prefix)
        mf.write(f)
        mf.write(":")
        mf.write(v)

    global first_build
    first_build = True

    def w_build(build):
        global first_build
        if first_build:
            mf.write("builds:\n")
            first_build = False

        w_field('versionName', build.version, '  - ', 'string')
        w_field('versionCode', build.vercode, '    ', 'strsng')
        for f in build_flags_order:
            v = build.get_flag(f)
            if not v:
                continue

            w_field(f, v, '    ', flagtype(f))

    write_plaintext_metadata(mf, app, w_comment, w_field, w_build)


def write_metadata(fmt, mf, app):
    if fmt == 'txt':
        return write_txt_metadata(mf, app)
    if fmt == 'yaml':
        return write_yaml_metadata(mf, app)
    raise MetaDataException("Unknown metadata format given")
