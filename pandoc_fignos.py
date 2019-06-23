#! /usr/bin/env python

"""pandoc-fignos: a pandoc filter that inserts figure nos. and refs."""


__version__ = '2.0.0b1'


# Copyright 2015-2019 Thomas J. Duck.
# All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


# OVERVIEW
#
# The basic idea is to scan the document twice in order to:
#
#   1. Insert text for the figure number in each figure caption.
#      For LaTeX, insert \label{...} instead.  The figure ids
#      and associated figure numbers are stored in the global
#      references tracker.
#
#   2. Replace each reference with a figure number.  For LaTeX,
#      replace with \ref{...} instead.
#
# This is followed by injecting header code as needed for certain output
# formats.


# pylint: disable=invalid-name

import sys
import re
import functools
import argparse
import json
import copy
import textwrap
import uuid

from pandocfilters import walk
from pandocfilters import Image, Math, Str, Space, Para, RawBlock, RawInline
from pandocfilters import Span

import pandocxnos
from pandocxnos import PandocAttributes
from pandocxnos import STRTYPES, STDIN, STDOUT, STDERR
from pandocxnos import elt, check_bool, get_meta, extract_attrs
from pandocxnos import repair_refs, process_refs_factory, replace_refs_factory
from pandocxnos import attach_attrs_factory, detach_attrs_factory
from pandocxnos import insert_secnos_factory, delete_secnos_factory

if sys.version_info > (3,):
    from urllib.request import unquote
else:
    from urllib import unquote  # pylint: disable=no-name-in-module


# Read the command-line arguments
parser = argparse.ArgumentParser(description='Pandoc figure numbers filter.')
parser.add_argument('--version', action='version',
                    version='%(prog)s {version}'.format(version=__version__))
parser.add_argument('fmt')
parser.add_argument('--pandocversion', help='The pandoc version.')
args = parser.parse_args()

# Pattern for matching labels
LABEL_PATTERN = re.compile(r'(fig:[\w/-]*)')

# Meta variables; may be reset elsewhere
captionname = 'Figure'  # The caption name
cleveref = False        # Flags that clever references should be used
capitalise = False      # Flags that plusname should be capitalised
plusname = ['fig.', 'figs.']      # Sets names for mid-sentence references
starname = ['Figure', 'Figures']  # Sets names for references at sentence start
numbersections = False  # Flags that figures should be numbered by section
warninglevel = 1        # 0 - no warnings; 1 - some warnings; 2 - all warnings

# Processing state variables
cursec = None    # Current section
Nreferences = 0  # Number of references in current section (or document)
references = {}  # Maps reference labels to [number/tag, figure secno]

# Processing flags
captionname_changed = False     # Flags the the caption name changed
plusname_changed = False        # Flags that the plus name changed
starname_changed = False        # Flags that the star name changed
has_unnumbered_figures = False  # Flags unnumbered figures were found
has_tagged_figures = False      # Flags a tagged figure was found

PANDOCVERSION = None


# Actions --------------------------------------------------------------------

def _extract_attrs(x, n):
    """Extracts attributes for an image.  n is the index where the
    attributes begin in the element list x.  Extracted elements are deleted
    from the list.
    """
    try:
        return extract_attrs(x, n)

    except (ValueError, IndexError):

        if PANDOCVERSION < '1.16':
            # Look for attributes attached to the image path, as occurs with
            # image references for pandoc < 1.16 (pandoc-fignos Issue #14).
            # See http://pandoc.org/MANUAL.html#images for the syntax.
            # Note: This code does not handle the "optional title" for
            # image references (search for link_attributes in pandoc's docs).
            assert x[n-1]['t'] == 'Image'
            image = x[n-1]
            s = image['c'][-1][0]
            if '%20%7B' in s:
                path = s[:s.index('%20%7B')]
                attrstr = unquote(s[s.index('%7B'):])
                image['c'][-1][0] = path  # Remove attr string from the path
                return PandocAttributes(attrstr.strip(), 'markdown')
        raise


def _process_figure(value, fmt):
    """Processes a figure.  Returns a dict containing figure properties."""

    # pylint: disable=global-statement
    global cursec        # Current section being processed
    global Nreferences   # Number of refs in current section (or document)
    global has_unnumbered_figures  # Flags that unnumbered figures were found

    # Initialize the return value
    fig = {'is_unnumbered': False,
           'is_unreferenceable': False,
           'is_tagged': False}

    # Bail out if there are no attributes
    if len(value[0]['c']) == 2:
        has_unnumbered_figures = True
        fig.update({'is_unnumbered': True, 'is_unreferenceable': True})
        return fig

    # Parse the figure
    attrs = fig['attrs'] = PandocAttributes(value[0]['c'][0], 'pandoc')
    fig['caption'] = value[0]['c'][1]

    # Bail out if the label does not conform to expectations
    if not LABEL_PATTERN.match(attrs.id):
        has_unnumbered_figures = True
        fig.update({'is_unnumbered': True, 'is_unreferenceable': True})
        return fig

    # Identify unreferenceable figures
    if attrs.id == 'fig:':
        attrs.id += str(uuid.uuid4())
        fig['is_unreferenceable'] = True

    # Update the current section number
    if attrs['secno'] != cursec:  # The section number changed
        cursec = attrs['secno']   # Update the global section tracker
        Nreferences = 1           # Resets the global reference counter

    # Pandoc's --number-sections supports section numbering latex/pdf, html,
    # epub, and docx
    if numbersections:
        # Latex/pdf supports equation numbers by section natively.  For the
        # other formats we must hard-code in figure numbers by section as
        # tags.
        if fmt in ['html', 'html5', 'epub', 'epub2', 'epub3', 'docx'] and \
          'tag' not in attrs:
            attrs['tag'] = str(cursec) + '.' + str(Nreferences)
            Nreferences += 1

    # Save reference information
    fig['is_tagged'] = 'tag' in attrs
    if fig['is_tagged']:  # ... then save the tag
        # Remove any surrounding quotes
        if attrs['tag'][0] == '"' and attrs['tag'][-1] == '"':
            attrs['tag'] = attrs['tag'].strip('"')
        elif attrs['tag'][0] == "'" and attrs['tag'][-1] == "'":
            attrs['tag'] = attrs['tag'].strip("'")
        references[attrs.id] = [attrs['tag'], cursec]
    else:  # ... then save the figure number
        Nreferences += 1  # Increment the global reference counter
        references[attrs.id] = [Nreferences, cursec]

    return fig


def _adjust_caption(fmt, fig, value):
    """Adjusts the caption."""
    attrs, caption = fig['attrs'], fig['caption']
    if fmt in ['latex', 'beamer']:  # Append a \label if this is referenceable
        if PANDOCVERSION < '1.17' and not fig['is_unreferenceable']:
            # pandoc >= 1.17 installs \label for us
            value[0]['c'][1] += \
              [RawInline('tex', r'\protect\label{%s}'%attrs.id)]
    else:  # Hard-code in the caption name and number/tag
        if fig['is_unnumbered']:
            return
        if isinstance(references[attrs.id][0], int):  # Numbered reference
            if fmt in ['html', 'html5', 'epub', 'epub2', 'epub3']:
                value[0]['c'][1] = [RawInline('html', r'<span>'),
                                    Str(captionname), Space(),
                                    Str('%d:'%references[attrs.id][0]),
                                    RawInline('html', r'</span>')]
            else:
                value[0]['c'][1] = [Str(captionname),
                                    Space(),
                                    Str('%d:'%references[attrs.id][0])]
            value[0]['c'][1] += [Space()] + list(caption)
        else:  # Tagged reference
            assert isinstance(references[attrs.id][0], STRTYPES)
            text = references[attrs.id][0]
            if text.startswith('$') and text.endswith('$'):  # Math
                math = text.replace(' ', r'\ ')[1:-1]
                els = [Math({"t":"InlineMath", "c":[]}, math), Str(':')]
            else:  # Text
                els = [Str(text+':')]
            if fmt in ['html', 'html5', 'epub', 'epub2', 'epub3']:
                value[0]['c'][1] = \
                  [RawInline('html', r'<span>'),
                   Str(captionname),
                   Space()] + els + [RawInline('html', r'</span>')]
            else:
                value[0]['c'][1] = [Str(captionname), Space()] + els
            value[0]['c'][1] += [Space()] + list(caption)


def _add_markup(fmt, fig, value):
    """Adds markup to the output."""

    # pylint: disable=global-statement
    global has_tagged_figures  # Flags a tagged figure was found

    if fig['is_unnumbered']:
        if fmt in ['latex', 'beamer']:
            # Use the no-prefix-figure-caption environment
            return [RawBlock('tex', r'\begin{fignos:no-prefix-figure-caption}'),
                    Para(value),
                    RawBlock('tex', r'\end{fignos:no-prefix-figure-caption}')]
        return None  # Nothing to do

    attrs = fig['attrs']
    ret = None

    if fmt in ['latex', 'beamer']:
        if fig['is_tagged']:  # A figure cannot be tagged if it is unnumbered
            # Use the tagged-figure environment
            has_tagged_figures = True
            ret = [RawBlock('tex', r'\begin{fignos:tagged-figure}[%s]' % \
                            references[attrs.id][0]),
                   Para(value),
                   RawBlock('tex', r'\end{fignos:tagged-figure}')]
    elif fmt in ('html', 'html5', 'epub', 'epub2', 'epub3'):
        if PANDOCVERSION < '1.16' and LABEL_PATTERN.match(attrs.id):
            # Insert anchor for PANDOCVERSION < 1.16; for later versions
            # the label is installed as an <img> id by pandoc.
            anchor = RawBlock('html', '<a name="%s"></a>'%attrs.id)
            ret = [anchor, Para(value)]
    elif fmt == 'docx':
        # As per http://officeopenxml.com/WPhyperlink.php
        bookmarkstart = \
          RawBlock('openxml',
                   '<w:bookmarkStart w:id="0" w:name="%s"/>'
                   %attrs.id)
        bookmarkend = \
          RawBlock('openxml', '<w:bookmarkEnd w:id="0"/>')
        ret = [bookmarkstart, Para(value), bookmarkend]
    return ret


def process_figures(key, value, fmt, meta):  # pylint: disable=unused-argument
    """Processes the figures."""

    # Process figures wrapped in Para elements
    if key == 'Para' and len(value) == 1 and \
      value[0]['t'] == 'Image' and value[0]['c'][-1][1].startswith('fig:'):

        # Process the figure and add markup
        fig = _process_figure(value, fmt)
        if 'attrs' in fig:
            _adjust_caption(fmt, fig, value)
        return _add_markup(fmt, fig, value)

    return None


# TeX blocks -----------------------------------------------------------------

# Define an environment that disables figure caption prefixes.  Counters
# must be saved and later restored.  The \thefigure and \theHfigure counter
# must be set to something unique so that duplicate internal names are avoided
# (see Sect. 3.2 of
# http://ctan.mirror.rafal.ca/macros/latex/contrib/hyperref/doc/manual.html).
NO_PREFIX_CAPTION_ENV_TEX = r"""
%% pandoc-fignos: environment to disable figure caption prefixes
\makeatletter
\newcounter{figno}
\newenvironment{fignos:no-prefix-figure-caption}{
  \caption@ifcompatibility{}{
    \let\oldthefigure\thefigure
    \let\oldtheHfigure\theHfigure
    \renewcommand{\thefigure}{figno:\thefigno}
    \renewcommand{\theHfigure}{figno:\thefigno}
    \stepcounter{figno}
    \captionsetup{labelformat=empty}
  }
}{
  \caption@ifcompatibility{}{
    \captionsetup{labelformat=default}
    \let\thefigure\oldthefigure
    \let\theHfigure\oldtheHfigure
    \addtocounter{figure}{-1}
  }
}
\makeatother
"""

# Define an environment for tagged figures
TAGGED_FIGURE_ENV_TEX = r"""
%% pandoc-fignos: environment for tagged figures
\newenvironment{fignos:tagged-figure}[1][]{
  \let\oldthefigure\thefigure
  \let\oldtheHfigure\theHfigure
  \renewcommand{\thefigure}{#1}
  \renewcommand{\theHfigure}{#1}
}{
  \let\thefigure\oldthefigure
  \let\theHfigure\oldtheHfigure
  \addtocounter{figure}{-1}
}
"""

# Reset the caption name; i.e. change "Figure" at the beginning of a caption
# to something else.
CAPTION_NAME_TEX = r"""
%% pandoc-fignos: change the caption name
\renewcommand{\figurename}{%s}
"""

# Define some tex to number figures by section
NUMBER_BY_SECTION_TEX = r"""
%% pandoc-fignos: number figures by section
\numberwithin{figure}{section}
"""


# Main program ---------------------------------------------------------------

# pylint: disable=too-many-branches,too-many-statements
def process(meta):
    """Saves metadata fields in global variables and returns a few
    computed fields."""

    # pylint: disable=global-statement
    global captionname     # The caption name
    global cleveref        # Flags that clever references should be used
    global capitalise      # Flags that plusname should be capitalised
    global plusname        # Sets names for mid-sentence references
    global starname        # Sets names for references at sentence start
    global numbersections  # Flags that sections should be numbered by section
    global warninglevel    # 0 - no warnings; 1 - some; 2 - all
    global captionname_changed  # Flags the the caption name changed
    global plusname_changed     # Flags that the plus name changed
    global starname_changed     # Flags that the star name changed

    # Read in the metadata fields and do some checking

    for name in ['fignos-warning-level', 'xnos-warning-level']:
        if name in meta:
            warninglevel = int(get_meta(meta, name))
            break

    metanames = ['fignos-warning-level', 'xnos-warning-level',
                 'fignos-caption-name',
                 'fignos-cleveref', 'xnos-cleveref',
                 'xnos-capitalize', 'xnos-capitalise',
                 'fignos-plus-name', 'fignos-star-name',
                 'fignos-number-sections', 'xnos-number-sections']

    if warninglevel:
        for name in meta:
            if (name.startswith('fignos') or name.startswith('xnos')) and \
              name not in metanames:
                msg = textwrap.dedent("""
                          pandoc-fignos: unknown meta variable "%s"
                      """ % name)
                STDERR.write(msg)

    if 'fignos-caption-name' in meta:
        old_captionname = captionname
        captionname = get_meta(meta, 'fignos-caption-name')
        captionname_changed = captionname != old_captionname
        assert isinstance(captionname, STRTYPES)

    for name in ['fignos-cleveref', 'xnos-cleveref']:
        # 'xnos-cleveref' enables cleveref in all 3 of fignos/eqnos/tablenos
        if name in meta:
            cleveref = check_bool(get_meta(meta, name))
            break

    for name in ['xnos-capitalise', 'xnos-capitalize']:
        # 'xnos-capitalise' enables capitalise in all 3 of
        # fignos/eqnos/tablenos.  Since this uses an option in the caption
        # package, it is not possible to select between the three (use
        # 'fignos-plus-name' instead.  'xnos-capitalize' is an alternative
        # spelling
        if name in meta:
            capitalise = check_bool(get_meta(meta, name))
            break

    if 'fignos-plus-name' in meta:
        tmp = get_meta(meta, 'fignos-plus-name')
        old_plusname = copy.deepcopy(plusname)
        if isinstance(tmp, list):  # The singular and plural forms were given
            plusname = tmp
        else:  # Only the singular form was given
            plusname[0] = tmp
        plusname_changed = plusname != old_plusname
        assert len(plusname) == 2
        for name in plusname:
            assert isinstance(name, STRTYPES)
        if plusname_changed:
            starname = [name.title() for name in plusname]

    if 'fignos-star-name' in meta:
        tmp = get_meta(meta, 'fignos-star-name')
        old_starname = copy.deepcopy(starname)
        if isinstance(tmp, list):
            starname = tmp
        else:
            starname[0] = tmp
        starname_changed = starname != old_starname
        assert len(starname) == 2
        for name in starname:
            assert isinstance(name, STRTYPES)

    for name in ['fignos-number-sections', 'xnos-number-sections']:
        if name in meta:
            numbersections = check_bool(get_meta(meta, name))
            break


def add_tex(meta):
    """Adds text to the meta data."""

    # pylint: disable=too-many-boolean-expressions
    warnings = warninglevel == 2 and \
      (pandocxnos.cleveref_required() or has_unnumbered_figures or
       plusname_changed or starname_changed or has_tagged_figures or
       captionname != 'Figure' or numbersections)
    if warnings:
        msg = textwrap.dedent("""\
                  pandoc-fignos: Wrote the following blocks to
                  header-includes.  If you use pandoc's
                  --include-in-header option then you will need to
                  manually include these yourself.
              """)
        STDERR.write('\n')
        STDERR.write(textwrap.fill(msg))
        STDERR.write('\n')

    # Update the header-includes metadata.  Pandoc's
    # --include-in-header option will override anything we do here.  This
    # is a known issue and is owing to a design decision in pandoc.
    # See https://github.com/jgm/pandoc/issues/3139.

    if pandocxnos.cleveref_required():
        tex = """
            %%%% pandoc-fignos: required package
            \\usepackage%s{cleveref}
        """ % ('[capitalise]' if capitalise else '')
        pandocxnos.add_tex_to_header_includes(
            meta, tex, warninglevel, r'\\usepackage(\[[\w\s,]*\])?\{cleveref\}')

    if has_unnumbered_figures:
        tex = """
            %%%% pandoc-fignos: required package
            \\usepackage{caption}
        """
        pandocxnos.add_tex_to_header_includes(
            meta, tex, warninglevel, r'\\usepackage(\[[\w\s,]*\])?\{caption\}')

    if plusname_changed:
        tex = """
            %%%% pandoc-fignos: change cref names
            \\crefname{figure}{%s}{%s}
        """ % (plusname[0], plusname[1])
        pandocxnos.add_tex_to_header_includes(meta, tex, warninglevel)

    if starname_changed:
        tex = """\
            %%%% pandoc-fignos: change Cref names
            \\Crefname{figure}{%s}{%s}
        """ % (starname[0], starname[1])
        pandocxnos.add_tex_to_header_includes(meta, tex, warninglevel)

    if has_unnumbered_figures:
        pandocxnos.add_tex_to_header_includes(
            meta, NO_PREFIX_CAPTION_ENV_TEX, warninglevel)

    if has_tagged_figures:
        pandocxnos.add_tex_to_header_includes(
            meta, TAGGED_FIGURE_ENV_TEX, warninglevel)

    if captionname != 'Figure':
        pandocxnos.add_tex_to_header_includes(
            meta, CAPTION_NAME_TEX % captionname, warninglevel)

    if numbersections:
        pandocxnos.add_tex_to_header_includes(
            meta, NUMBER_BY_SECTION_TEX, warninglevel)

    if warnings:
        STDERR.write('\n')


def main():
    """Filters the document AST."""

    # pylint: disable=global-statement
    global PANDOCVERSION
    global Image

    # Get the output format and document
    fmt = args.fmt
    doc = json.loads(STDIN.read())

    # Initialize pandocxnos
    PANDOCVERSION = pandocxnos.init(args.pandocversion, doc)

    # Element primitives
    if PANDOCVERSION < '1.16':
        Image = elt('Image', 2)

    # Chop up the doc
    meta = doc['meta'] if PANDOCVERSION >= '1.18' else doc[0]['unMeta']
    blocks = doc['blocks'] if PANDOCVERSION >= '1.18' else doc[1:]

    # Process the metadata variables
    process(meta)

    # First pass
    replace = PANDOCVERSION >= '1.16'
    attach_attrs_image = attach_attrs_factory('pandoc-fignos', Image,
                                              warninglevel,
                                              extract_attrs=_extract_attrs,
                                              replace=replace)
    detach_attrs_image = detach_attrs_factory(Image)
    insert_secnos = insert_secnos_factory(Image)
    delete_secnos = delete_secnos_factory(Image)
    altered = functools.reduce(lambda x, action: walk(x, action, fmt, meta),
                               [attach_attrs_image, insert_secnos,
                                process_figures, delete_secnos,
                                detach_attrs_image], blocks)

    # Second pass
    process_refs = process_refs_factory('pandoc-fignos', references.keys(),
                                        warninglevel)
    replace_refs = replace_refs_factory(references,
                                        cleveref, False,
                                        plusname if not capitalise \
                                        or plusname_changed else
                                        [name.title() for name in plusname],
                                        starname)
    attach_attrs_span = attach_attrs_factory('pandoc-fignos', Span,
                                             warninglevel, replace=True)
    altered = functools.reduce(lambda x, action: walk(x, action, fmt, meta),
                               [repair_refs, process_refs, replace_refs,
                                attach_attrs_span],
                               altered)

    if fmt in ['latex', 'beamer']:
        add_tex(meta)

    # Update the doc
    if PANDOCVERSION >= '1.18':
        doc['blocks'] = altered
    else:
        doc = doc[:1] + altered

    # Dump the results
    json.dump(doc, STDOUT)

    # Flush stdout
    STDOUT.flush()

if __name__ == '__main__':
    main()
