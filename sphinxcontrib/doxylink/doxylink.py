# -*- coding: utf-8 -*-

from collections import namedtuple
import os
import xml.etree.ElementTree as ET
import urllib.parse

from docutils import nodes, utils
from sphinx.util.nodes import split_explicit_title
from sphinx.util.console import bold, standout

from ..doxylink import __version__
from .parsing import normalise, ParseException

Entry = namedtuple('Entry', ['kind', 'file'])


class FunctionList:
    """A FunctionList maps argument lists to specific entries"""
    def __init__(self):
        self.kind = 'function_list'
        self._arglist = {}  # type: Mapping[str, str]

    def __getitem__(self, arglist: str) -> Entry:
        # If the user has requested a specific function through specifying an arglist then get the right anchor
        if arglist:
            try:
                filename = self._arglist[arglist]
            except KeyError:
                # TODO Offer fuzzy suggestion
                raise LookupError('Argument list match not found')
        else:
            # Otherwise just return the first entry (if they don't care they get whatever comes first)
            filename = list(self._arglist.values())[0]

        return Entry(kind='function', file=filename)

    def add_overload(self, arglist: str, file: str) -> None:
        self._arglist[arglist] = file


class SymbolMap:
    """A SymbolMap maps symbols to Entries or FunctionLists"""
    def __init__(self, xml_doc: ET.ElementTree) -> None:
        self._mapping = parse_tag_file(xml_doc)

    def _get_symbol_matches(self, symbol):
        if self._mapping.get(symbol):
            return self._mapping[symbol]

        piecewise_list = find_url_piecewise(self._mapping, symbol)

        # If there is only one match, return it.
        if len(piecewise_list) == 1:
            return list(piecewise_list.values())[0]

        # If there is more than one item in piecewise_list then there is an ambiguity
        # Often this is due to the symbol matching the name of the constructor as well as the class name itself
        # We will prefer the class
        classes_list = {s: e for s, e in piecewise_list.items() if e.kind == 'class'}

        # If there is only one by here we return it.
        if len(classes_list) == 1:
            return list(classes_list.values())[0]

        # Now, to disambiguate between ``PolyVox::Array< 1, ElementType >::operator[]`` and ``PolyVox::Array::operator[]`` matching ``operator[]``,
        # we will ignore templated (as in C++ templates) tag names by removing names containing ``<``
        no_templates_list = {s: e for s, e in piecewise_list.items() if '<' not in s}

        if len(no_templates_list) == 1:
            return list(no_templates_list.values())[0]

        # If not found by now, return the shortest match, assuming that's the most specific
        if no_templates_list:
            # TODO return a warning here?
            shortest_match = min(no_templates_list.keys(), key=len)
            return no_templates_list[shortest_match]

        # TODO Offer fuzzy suggestion
        raise LookupError('Could not find a match')

    def __getitem__(self, item: str) -> Entry:
        try:
            symbol, normalised_arglist = normalise(item)
        except ParseException as error:
            raise LookupError(error)

        entry = self._get_symbol_matches(symbol)

        if isinstance(entry, FunctionList):
            entry = entry[normalised_arglist]

        return entry


def find_url(doc, symbol):
    """
    Return the URL for a given symbol.

    This is where the magic happens.
    This function could be a lot more clever. At present it required the passed symbol to be almost exactly the same as the entries in the Doxygen tag file.

    .. todo::

        Maybe print a list of all possible matches as a warning (but still only return the first)

    :Parameters:
        doc : xml.etree.ElementTree
            The XML DOM object
        symbol : string
            The symbol to lookup in the file. E.g. something like 'PolyVox::Array' or 'tidyUpMemory'

    :return: String representing the filename part of the URL
    """

    # First check for an exact match with a top-level object (namespaces, objects etc.)

    #env = inliner.document.settings.env

    matches = []
    for compound in doc.findall('.//compound'):
        if compound.find('name').text == symbol:
            matches += [{'file': compound.find('filename').text, 'kind': compound.get('kind')}]

    if len(matches) > 1:
        pass
        #env.warn(env.docname, 'There were multiple matches for `%s`: %s' % (symbol, matches))
    if len(matches) == 1:
        return matches[0]

    # Strip off first namespace bit of the compound name so that 'ArraySizes' can match 'PolyVox::ArraySizes'
    for compound in doc.findall('.//compound'):
        symbol_list = compound.find('name').text.split('::', 1)
        if len(symbol_list) == 2:
            reducedsymbol = symbol_list[1]
            if reducedsymbol == symbol:
                return {'file': compound.find('filename').text, 'kind': compound.get('kind')}

    # Now split the symbol by '::'. Find an exact match for the first part and then a member match for the second
    # So PolyVox::Array::operator[] becomes like {namespace: "PolyVox::Array", endsymbol: "operator[]"}
    symbol_list = symbol.rsplit('::', 1)
    if len(symbol_list) == 2:
        namespace = symbol_list[0]
        endsymbol = symbol_list[1]
        for compound in doc.findall('.//compound'):
            if compound.find('name').text == namespace:
                for member in compound.findall('member'):
                    #If this compound object contains the matching member then return it
                    if member.find('name').text == endsymbol:
                        return {'file': (member.findtext('anchorfile') or compound.findtext('filename')) + '#' + member.find('anchor').text, 'kind': member.get('kind')}

    # Then we'll look at unqualified members
    for member in doc.findall('.//member'):
        if member.find('name').text == symbol:
            return {'file': (member.findtext('anchorfile') or compound.findtext('filename')) + '#' + member.find('anchor').text, 'kind': member.get('kind')}

    return None


def parse_tag_file(doc: ET.ElementTree) -> dict:
    """
    Takes in an XML tree from a Doxygen tag file and returns a dictionary that looks something like:

    .. code-block:: python

        {'PolyVox': Entry(...),
         'PolyVox::Array': Entry(...),
         'PolyVox::Array1DDouble': Entry(...),
         'PolyVox::Array1DFloat': Entry(...),
         'PolyVox::Array1DInt16': Entry(...),
         'QScriptContext::throwError': FunctionList(...),
         'QScriptContext::toString': FunctionList(...)
         }

    Note the different form for functions. This is required to allow for 'overloading by argument type'.

    :Parameters:
        doc : xml.etree.ElementTree
            The XML DOM object

    :return: a dictionary mapping fully qualified symbols to files
    """

    mapping = {}  # type: Mapping[str, Union[Entry, FunctionList]]
    function_list = []  # This is a list of function to be parsed and inserted into mapping at the end of the function.
    for compound in doc.findall('./compound'):
        compound_kind = compound.get('kind')
        if compound_kind not in {'namespace', 'class', 'struct', 'file', 'define', 'group'}:
            continue  # Skip everything that isn't a namespace, class, struct or file

        compound_name = compound.findtext('name')
        compound_filename = compound.findtext('filename')

        # TODO The following is a hack bug fix I think
        # Doxygen doesn't seem to include the file extension to <compound kind="file"><filename> entries
        # If it's a 'file' type, check if it _does_ have an extension, if not append '.html'
        if compound_kind == 'file' and not os.path.splitext(compound_filename)[1]:
            compound_filename = compound_filename + '.html'

        # If it's a compound we can simply add it
        mapping[compound_name] = Entry(kind=compound_kind, file=compound_filename)

        for member in compound.findall('member'):

            # If the member doesn't have an <anchorfile> element, use the parent compounds <filename> instead
            # This is the way it is in the qt.tag and is perhaps an artefact of old Doxygen
            anchorfile = member.findtext('anchorfile') or compound_filename
            member_symbol = compound_name + '::' + member.findtext('name')
            member_kind = member.get('kind')
            arglist_text = member.findtext('./arglist')  # If it has an <arglist> then we assume it's a function. Empty <arglist> returns '', not None. Things like typedefs and enums can have empty arglists

            if arglist_text and member_kind not in {'variable', 'typedef', 'enumeration'}:
                function_list.append((member_symbol, arglist_text, member_kind, join(anchorfile, '#', member.findtext('anchor'))))
            else:
                mapping[member_symbol] = Entry(kind=member.get('kind'), file=join(anchorfile, '#', member.findtext('anchor')))

    for member_symbol, arglist, kind, anchor_link in function_list:
        try:
            normalised_arglist = normalise(member_symbol + arglist)[1]
        except ParseException as e:
            print('Skipping %s %s%s. Error reported from parser was: %s' % (kind, member_symbol, arglist, e))
        else:
            if mapping.get(member_symbol) and isinstance(mapping[member_symbol], FunctionList):
                mapping[member_symbol].add_overload(normalised_arglist, anchor_link)
            else:
                mapping[member_symbol] = FunctionList()
                mapping[member_symbol].add_overload(normalised_arglist, anchor_link)

    return mapping


def find_url_piecewise(mapping: dict, symbol: str) -> dict:
    """
    Match the requested symbol reverse piecewise (split on ``::``) against the tag names to ensure they match exactly (modulo ambiguity)
    So, if in the mapping there is ``PolyVox::Volume::FloatVolume`` and ``PolyVox::Volume`` they would be split into:

    .. code-block:: python

        ['PolyVox', 'Volume', 'FloatVolume'] and ['PolyVox', 'Volume']

    and reversed:

    .. code-block:: python

        ['FloatVolume', 'Volume', 'PolyVox'] and ['Volume', 'PolyVox']

    and truncated to the shorter of the two:

    .. code-block:: python

        ['FloatVolume', 'Volume'] and ['Volume', 'PolyVox']

    If we're searching for the ``PolyVox::Volume`` symbol we would compare:

    .. code-block:: python

        ['Volume', 'PolyVox'] to ['FloatVolume', 'Volume', 'PolyVox'].

    That doesn't match so we look at the next in the mapping:

    .. code-block:: python

        ['Volume', 'PolyVox'] to ['Volume', 'PolyVox'].

    Good, so we add it to the list

    """
    piecewise_list = {}
    for item, data in mapping.items():
        split_symbol = symbol.split('::')
        split_item = item.split('::')

        split_symbol.reverse()
        split_item.reverse()

        min_length = min(len(split_symbol), len(split_item))

        split_symbol = split_symbol[:min_length]
        split_item = split_item[:min_length]

        #print split_symbol, split_item

        if split_symbol == split_item:
            #print symbol + ' : ' + item
            piecewise_list[item] = data

    return piecewise_list


def join(*args):
    return ''.join(args)


def create_role(app, tag_filename, rootdir):
    # Tidy up the root directory path
    if not rootdir.endswith(('/', '\\')):
        rootdir = join(rootdir, os.sep)

    try:
        tag_file = ET.parse(tag_filename)

        cache_name = os.path.basename(tag_filename)

        app.info(bold('Checking tag file cache for %s: ' % cache_name), nonl=True)
        if not hasattr(app.env, 'doxylink_cache'):
            # no cache present at all, initialise it
            app.info('No cache at all, rebuilding...')
            mapping = SymbolMap(tag_file)
            app.env.doxylink_cache = {cache_name: {'mapping': mapping, 'mtime': os.path.getmtime(tag_filename)}}
        elif not app.env.doxylink_cache.get(cache_name):
            # Main cache is there but the specific sub-cache for this tag file is not
            app.info('Sub cache is missing, rebuilding...')
            mapping = SymbolMap(tag_file)
            app.env.doxylink_cache[cache_name] = {'mapping': mapping, 'mtime': os.path.getmtime(tag_filename)}
        elif app.env.doxylink_cache[cache_name]['mtime'] < os.path.getmtime(tag_filename):
            # tag file has been modified since sub-cache creation
            app.info('Sub-cache is out of date, rebuilding...')
            mapping = SymbolMap(tag_file)
            app.env.doxylink_cache[cache_name] = {'mapping': mapping, 'mtime': os.path.getmtime(tag_filename)}
        elif not app.env.doxylink_cache[cache_name].get('version') or app.env.doxylink_cache[cache_name].get('version') != __version__:
            # sub-cache doesn't have a version or the version doesn't match
            app.info('Sub-cache schema version doesn\'t match, rebuilding...')
            mapping = SymbolMap(tag_file)
            app.env.doxylink_cache[cache_name] = {'mapping': mapping, 'mtime': os.path.getmtime(tag_filename)}
        else:
            # The cache is up to date
            app.info('Sub-cache is up-to-date')
    except FileNotFoundError:
        tag_file = None
        app.warn(standout('Could not find tag file %s. Make sure your `doxylink` config variable is set correctly.' % tag_filename))

    def find_doxygen_link(name, rawtext, text, lineno, inliner, options={}, content=[]):
        # from :name:`title <part>`
        has_explicit_title, title, part = split_explicit_title(text)
        part = utils.unescape(part)
        warning_messages = []
        if tag_file:
            url = find_url(tag_file, part)
            try:
                url = app.env.doxylink_cache[cache_name]['mapping'][part]
            except LookupError as error:
                warning_messages.append('Error while parsing `%s`. Is not a well-formed C++ function call or symbol. If this is not the case, it is a doxylink bug so please report it. Error reported was: %s' % (part, error))
            if url:

                # If it's an absolute path then the link will work regardless of the document directory
                # Also check if it is a URL (i.e. it has a 'scheme' like 'http' or 'file')
                if os.path.isabs(rootdir) or urllib.parse.urlparse(rootdir).scheme:
                    full_url = join(rootdir, url.file)
                # But otherwise we need to add the relative path of the current document to the root source directory to the link
                else:
                    relative_path_to_docsrc = os.path.relpath(app.env.srcdir, os.path.dirname(inliner.document.current_source))
                    full_url = join(relative_path_to_docsrc, '/', rootdir, url.file)  # We always use the '/' here rather than os.sep since this is a web link avoids problems like documentation/.\../library/doc/ (mixed slashes)

                if url.kind == 'function' and app.config.add_function_parentheses and not normalise(title)[1]:
                    title = join(title, '()')

                pnode = nodes.reference(title, title, internal=False, refuri=full_url)
                return [pnode], []
            # By here, no match was found
            warning_messages.append('Could not find match for `%s` in `%s` tag file' % (part, tag_filename))
        else:
            warning_messages.append('Could not find match for `%s` because tag file not found' % (part))

        pnode = nodes.inline(rawsource=title, text=title)
        return [pnode], [inliner.reporter.warning(message, line=lineno) for message in warning_messages]

    return find_doxygen_link


def setup_doxylink_roles(app):
    for name, (tag_filename, rootdir) in app.config.doxylink.items():
        app.add_role(name, create_role(app, tag_filename, rootdir))
