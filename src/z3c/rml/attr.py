##############################################################################
#
# Copyright (c) 2007 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""RML Attribute Implementation

$Id: attr.py 121449 2011-04-19 16:00:38Z menesis $
"""
__docformat__ = "reStructuredText"
import cStringIO
import logging
import os
import re
import reportlab.graphics.widgets.markers
import reportlab.lib.colors
import reportlab.lib.pagesizes
import reportlab.lib.styles
import reportlab.lib.units
import reportlab.lib.utils
import urllib
import zope.interface
import zope.schema
from lxml import etree

from z3c.rml import interfaces

MISSING = object()
logger = logging.getLogger("z3c.rml")


def getFileInfo(attr):
    root = attr.context
    while root.parent:
        root = root.parent
    return '(file %s, line %i)' % (
        root.filename, attr.context.element.sourceline)


def getManager(context, interface=None):
    if interface is None:
        # Avoid circular imports
        from z3c.rml import interfaces
        interface = interfaces.IManager
    # Walk up the path until the manager is found
    while (not interface.providedBy(context) and context is not None):
        context = context.parent
    # If no manager was found, raise an error
    if context is None:
        raise ValueError('The manager could not be found.')
    return context


def deprecated(oldName, attr, reason):
    zope.interface.directlyProvides(attr, interfaces.IDeprecated)
    attr.deprecatedName = oldName
    attr.deprecatedReason = reason
    return attr


class RMLAttribute(zope.schema.Field):
    """An attribute of the RML directive."""

    missing_value = MISSING
    default = MISSING

    def fromUnicode(self, ustr):
        """See zope.schema.interfaces.IField"""
        if self.context is None:
            raise ValueError('Attribute not bound to a context.')
        return super(RMLAttribute, self).fromUnicode(unicode(ustr))

    def get(self):
        """See zope.schema.interfaces.IField"""
        # If the attribute has a deprecated partner and the deprecated name
        # has been specified, use it.
        if (interfaces.IDeprecated.providedBy(self) and
            self.deprecatedName in self.context.element.attrib):
            name = self.deprecatedName
            logger.warn(
                u'Deprecated attribute "%s": %s %s' % (
                name, self.deprecatedReason, getFileInfo(self)))
        else:
            name = self.__name__
        # Extract the value.
        value = self.context.element.get(name, self.missing_value)
        # Get the correct default value.
        if value is self.missing_value:
            if self.default is not None:
                return self.default
            return self.missing_value
        return self.fromUnicode(value)


class BaseChoice(RMLAttribute):
    choices = {}

    def fromUnicode(self, value):
        value = value.lower()
        if value in self.choices:
            return self.choices[value]
        raise ValueError(
            '%r not a valid value for attribute "%s". %s' % (
            value, self.__name__, getFileInfo(self)))


class Combination(RMLAttribute):
    """A combination of several other attribute types."""

    def __init__(self, value_types=(), *args, **kw):
        super(Combination, self).__init__(*args, **kw)
        self.value_types = value_types

    def fromUnicode(self, value):
        for value_type in self.value_types:
            bound = value_type.bind(self)
            try:
                return bound.fromUnicode(value)
            except ValueError:
                pass
        raise ValueError(
            '"%s" is not a valid value. %s' %(value, getFileInfo(self)))


class String(RMLAttribute, zope.schema.Bytes):
    """A simple Bytes string."""


class Text(RMLAttribute, zope.schema.Text):
    """A simple unicode string."""


class Integer(RMLAttribute, zope.schema.Int):
    """An integer. A minimum and maximum value can be specified."""
    # By making min and max simple attributes, we avoid some validation
    # problems.
    min = None
    max = None


class Float(RMLAttribute, zope.schema.Float):
    """An flaoting point. A minimum and maximum value can be specified."""
    # By making min and max simple attributes, we avoid some validation
    # problems.
    min = None
    max = None


class StringOrInt(RMLAttribute):
    """A (bytes) string or an integer."""

    def fromUnicode(self, value):
        try:
            return int(value)
        except ValueError:
            return str(value)


class Sequence(RMLAttribute, zope.schema._field.AbstractCollection):
    """A list of values of a specified type."""

    splitre = re.compile('[ \t\n,;]*')

    def __init__(self, splitre=None, *args, **kw):
        super(Sequence, self).__init__(*args, **kw)
        if splitre is not None:
            self.splitre = splitre

    def fromUnicode(self, ustr):
        if ustr.startswith('(') and ustr.endswith(')'):
            ustr = ustr[1:-1]
        ustr = ustr.strip()
        raw_values = self.splitre.split(ustr)
        result = [self.value_type.bind(self.context).fromUnicode(raw.strip())
                  for raw in raw_values]
        if ((self.min_length is not None and len(result) < self.min_length) and
            (self.max_length is not None and len(result) > self.max_length)):
            raise ValueError(
                'Length of sequence must be at least %s and at most %i. %s' % (
                self.min_length, self.max_length, getFileInfo(self)))
        return result


class Choice(BaseChoice):
    """A choice of several values. The values are always case-insensitive."""

    def __init__(self, choices=None, *args, **kw):
        super(Choice, self).__init__(*args, **kw)
        if isinstance(choices, (tuple, list)):
            choices = dict([(val.lower(), val) for val in choices])
        self.choices = choices


class Boolean(BaseChoice):
    '''A boolean value.

    For true the values "true", "yes", and "1" are allowed. For false, the
    values "false", "no", "1" are allowed.
    '''
    choices = {'true': True, 'false': False,
               'yes': True, 'no': False,
               '1': True, '0': False,
               }


class BooleanWithDefault(Boolean):
    '''This is a boolean field that can also receive the value "default".'''
    choices = Boolean.choices.copy()
    choices.update({'default': None})


class Measurement(RMLAttribute):
    '''This field represents a length value.

    The units "in" (inch), "cm", and "mm" are allowed. If no units are
    specified, the value is given in points/pixels.
    '''
    def __init__(self, allowPercentage=False, allowStar=False, *args, **kw):
        super(Measurement, self).__init__(*args, **kw)
        self.allowPercentage = allowPercentage
        self.allowStar = allowStar

    units = [
	(re.compile('^(-?[0-9\.]+)\s*in$'), reportlab.lib.units.inch),
	(re.compile('^(-?[0-9\.]+)\s*cm$'), reportlab.lib.units.cm),
	(re.compile('^(-?[0-9\.]+)\s*mm$'), reportlab.lib.units.mm),
	(re.compile('^(-?[0-9\.]+)\s*$'), 1)
        ]

    allowPercentage = False
    allowStar = False

    def fromUnicode(self, value):
        if value == 'None':
            return None
        if value == '*' and self.allowStar:
            return value
        if value.endswith('%') and self.allowPercentage:
            return value
	for unit in self.units:
            res = unit[0].search(value, 0)
            if res:
                return unit[1]*float(res.group(1))
        raise ValueError(
            'The value %r is not a valid measurement. %s' % (
            value, getFileInfo(self)))


class File(Text):
    """This field will return a file object.

    The value itself can eith be be a relative or absolute path. Additionally
    the following syntax is supported: [path.to.python.mpackage]/path/to/file
    """
    open = staticmethod(urllib.urlopen)
    packageExtract = re.compile('^\[([0-9A-z_.]*)\]/(.*)$')

    doNotOpen = False

    def __init__(self, doNotOpen=False, *args, **kw):
        super(File, self).__init__(*args, **kw)
        self.doNotOpen = doNotOpen

    def fromUnicode(self, value):
        # Check whether the value is of the form:
        #    [<module.path>]/rel/path/image.gif"
        if value.startswith('['):
            result = self.packageExtract.match(value)
            if result is None:
                raise ValueError(
                    'The package-path-pair you specified was incorrect. %s' %(
                    getFileInfo(self)))
            modulepath, path = result.groups()
            module = __import__(modulepath, {}, {}, (modulepath))
            value = os.path.join(os.path.dirname(module.__file__), path)
        # If there is a drive name in the path, then we want a local file to
        # be opened. This is only interesting for Windows of course.
        if os.path.splitdrive(value)[0]:
            value = 'file:///' + value
        # If the file is not to be opened, simply return the path.
        if self.doNotOpen:
            return value
        # Open/Download the file
        fileObj = self.open(value)
        sio = cStringIO.StringIO(fileObj.read())
        fileObj.close()
        sio.seek(0)
        return sio


class Image(File):
    """Similar to the file File attribute, except that an image is internally
    expected."""

    def __init__(self, onlyOpen=False, *args, **kw):
        super(Image, self).__init__(*args, **kw)
        self.onlyOpen = onlyOpen

    def fromUnicode(self, value):
        fileObj = super(Image, self).fromUnicode(value)
        if self.onlyOpen:
            return fileObj
        return reportlab.lib.utils.ImageReader(fileObj)


class Color(RMLAttribute):
    """Requires the input of a color. There are several supported formats.

    Three values in a row are interpreted as RGB value ranging from 0-255.
    A string is interpreted as a name to a pre-defined color.
    The 'CMYK()' wrapper around four values represents a CMYK color
    specification.
    """

    def __init__(self, acceptNone=False, *args, **kw):
        super(Color, self).__init__(*args, **kw)
        self.acceptNone = acceptNone

    def fromUnicode(self, value):
        if self.acceptNone and value == 'None':
            return None
        manager = getManager(self.context)
        if value in manager.colors:
            return manager.colors[value]
        try:
            return reportlab.lib.colors.toColor(value)
        # Bare except, since code raises string exception: Invalid color value
        except:
            raise ValueError(
                'The color specification "%s" is not valid. %s' % (
                value, getFileInfo(self)))

class Style(String):
    """Requires a valid style to be entered.

    Whether the style is a paragraph, table or box style is irrelevant, except
    that it has to fit the tag.
    """
    default = reportlab.lib.styles.getSampleStyleSheet().byName['Normal']

    def fromUnicode(self, value):
        manager = getManager(self.context)
        for styles in (manager.styles,
                       reportlab.lib.styles.getSampleStyleSheet().byName):
            if value in styles:
                return styles[value]
            elif 'style.' + value in styles:
                return styles['style.' + value]
            elif value.startswith('style.') and value[6:] in styles:
                return styles[value[6:]]
        raise ValueError('Style %r could not be found. %s' % (
            value, getFileInfo(self)))


class Symbol(Text):
    """This attribute should contain the text representation of a symbol to be
    used."""

    def fromUnicode(self, value):
        return reportlab.graphics.widgets.markers.makeMarker(value)


class PageSize(RMLAttribute):
    """A simple measurement pair that specifies the page size. Optionally you
    can also specify a the name of a page size, such as A4, letter, or legal.
    """

    sizePair = Sequence(value_type=Measurement())
    words = Sequence(value_type=String())

    def fromUnicode(self, value):
        # First try to get a pair
        try:
            return self.sizePair.bind(self.context).fromUnicode(value)
        except ValueError:
            pass
        # Now we try to lookup a name. The following type of combinations must
        # work: "Letter" "LETTER" "A4 landscape" "letter portrait"
        words = self.words.bind(self.context).fromUnicode(value)
        words = [word.lower() for word in words]
        # First look for the orientation
        orienter = None
        for orientation in ('landscape', 'portrait'):
            if orientation in words:
                orienter = getattr(reportlab.lib.pagesizes, orientation)
                words.remove(orientation)
        # We must have exactely one value left that matches a paper size
        pagesize = getattr(reportlab.lib.pagesizes, words[0].upper())
        # Now do the final touches
        if orienter:
            pagesize = orienter(pagesize)
        return pagesize


class TextNode(RMLAttribute):
    """Return the text content of an element."""

    def get(self):
        if self.context.element.text is None:
            return u''
        return unicode(self.context.element.text).strip()


class FirstLevelTextNode(TextNode):
    """Gets all the text content of an element without traversing into any
    child-elements."""

    def get(self):
        text = self.context.element.text or u''
        for child in self.context.element.getchildren():
            text += child.tail or u''
        return text.strip()


class TextNodeSequence(Sequence):
    """A sequence of values retrieved from the element's content."""

    def get(self):
        return self.fromUnicode(self.context.element.text)


class TextNodeGrid(TextNodeSequence):
    """A grid/matrix of values retrieved from the element's content.

    The number of columns is specified for every case, but the number of rows
    is dynamic.
    """

    def __init__(self, columns=None, *args, **kw):
        super(TextNodeGrid, self).__init__(*args, **kw)
        self.columns = columns

    def fromUnicode(self, ustr):
        result = super(TextNodeGrid, self).fromUnicode(ustr)
        if len(result) % self.columns != 0:
            raise ValueError(
                'Number of elements must be divisible by %i. %s' %(
                self.columns, getFileInfo(self)))
        return [result[i*self.columns:(i+1)*self.columns]
                for i in range(len(result)/self.columns)]


class RawXMLContent(RMLAttribute):
    """Retrieve the raw content of an element.

    Only some special element substitution will be made.
    """

    def __init__(self, *args, **kw):
        super(RawXMLContent, self).__init__(*args, **kw)
        # Do it in here, since we have a recursive problem otherwise
        from z3c.rml import special
        self.handleElements = {'getName': special.GetName}

    def get(self):
        # Replace what we can replace
        for subElement in self.context.element.iterdescendants():
            if subElement.tag in self.handleElements:
                substitute = self.handleElements[subElement.tag](
                    subElement, self.context)
                substitute.process()
        # Now create the text
        # ReportLab's paragraph parser does not like attributes from other
        # namespaces; sigh. So we have to improvize.
        text = etree.tounicode(self.context.element)
        text = text[text.find('>')+1:text.rfind('<')]
        return text


class XMLContent(RawXMLContent):
    """Same as 'RawXMLContent', except that the whitespace is normalized."""

    def get(self):
        result = super(XMLContent, self).get()
        return result.strip().replace('\t', ' ')
