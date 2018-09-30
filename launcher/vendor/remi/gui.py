"""
   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

import os
import sys
import logging
import functools
import threading
import collections
import inspect

from .server import runtimeInstances

log = logging.getLogger('remi.gui')

pyLessThan3 = sys.version_info < (3,)


def to_pix(x):
    return str(x) + 'px'


def from_pix(x):
    v = 0
    try:
        v = int(float(x.replace('px', '')))
    except ValueError:
        log.error('error parsing px', exc_info=True)
    return v


def jsonize(d):
    return ';'.join(map(lambda k, v: k + ':' + v + '', d.keys(), d.values()))


class EventSource(object):
    def __init__(self, *args, **kwargs):
        self.setup_event_methods()
    
    def setup_event_methods(self):
        for (method_name, method) in inspect.getmembers(self, predicate=inspect.ismethod):
            _event_info = None
            if hasattr(method, "_event_info"):
                _event_info = method._event_info
            
            if hasattr(method, '__is_event'):
                e = ClassEventConnector(self, method_name, method)
                setattr(self, method_name, e)

            if _event_info:
                getattr(self, method_name)._event_info = _event_info


class ClassEventConnector(object):
    """ This class allows to manage the events. Decorating a method with *decorate_event* decorator
        The method gets the __is_event flag. At runtime, the methods that has this flag gets replaced
        by a ClassEventConnector. This class overloads the __call__ method, where the event method is called,
        and after that the listener method is called too.
    """
    def __init__(self, event_source_instance, event_name, event_method_bound):
        self.event_source_instance = event_source_instance
        self.event_name = event_name
        self.event_method_bound = event_method_bound
        self.callback = None
        self.userdata = None
        
    def connect(self, callback, *userdata):
        """ The callback and userdata gets stored, and if there is some javascript to add
            the js code is appended as attribute for the event source
        """
        if hasattr(self.event_method_bound, '_js_code'):
            self.event_source_instance.attributes[self.event_name] = self.event_method_bound._js_code%{
                'emitter_identifier':self.event_source_instance.identifier, 'event_name':self.event_name}
        self.callback = callback
        self.userdata = userdata

    def __call__(self, *args, **kwargs):
        #here the event method gets called
        callback_params =  self.event_method_bound(*args, **kwargs)
        if not self.callback:
            return callback_params
        if not callback_params:
            callback_params = self.userdata
        else:
            callback_params = callback_params + self.userdata
        #here the listener gets called, passing as parameters the return values of the event method
        # plus the userdata parameters
        return self.callback(self.event_source_instance, *callback_params)


def decorate_event(method):
    """ setup a method as an event """
    setattr(method, "__is_event", True )
    return method


def decorate_event_js(js_code):
    """setup a method as an event, adding also javascript code to generate

    Args:
        js_code (str): javascript code to generate the event client-side.
            js_code is added to the widget html as 
            widget.attributes['onclick'] = js_code%{'emitter_identifier':widget.identifier, 'event_name':'onclick'}
    """
    def add_annotation(method):
        setattr(method, "__is_event", True )
        setattr(method, "_js_code", js_code )
        return method
    return add_annotation


def decorate_set_on_listener(prototype):
    """ Private decorator for use in the editor.
        Allows the Editor to create listener methods.

        Args:
            params (str): The list of parameters for the listener 
                method (es. "(self, new_value)")
    """
    # noinspection PyDictCreation,PyProtectedMember
    def add_annotation(method):
        method._event_info = {}
        method._event_info['name'] = method.__name__
        method._event_info['prototype'] = prototype
        return method

    return add_annotation


def decorate_constructor_parameter_types(type_list):
    """ Private decorator for use in the editor. 
        Allows Editor to instanciate widgets.

        Args:
            params (str): The list of types for the widget 
                constructor method (i.e. "(int, int, str)")
    """
    def add_annotation(method):
        method._constructor_types = type_list
        return method

    return add_annotation


def decorate_explicit_alias_for_listener_registration(method):
    method.__doc__ = """ Registers the listener
                         For backward compatibility
                         Suggested new dialect event.connect(callback, *userdata)
                     """
    return method


class _EventDictionary(dict, EventSource):
    """This dictionary allows to be notified if its content is changed.
    """

    def __init__(self, *args, **kwargs):
        self.__version__ = 0
        self.__lastversion__ = 0
        super(_EventDictionary, self).__init__(*args, **kwargs)
        EventSource.__init__(self, *args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            if self[key] == value:
                return
        ret = super(_EventDictionary, self).__setitem__(key, value)
        self.onchange()
        return ret

    def __delitem__(self, key):
        if key not in self:
            return
        ret = super(_EventDictionary, self).__delitem__(key)
        self.onchange()
        return ret

    def pop(self, key, d=None):
        if key not in self:
            return
        ret = super(_EventDictionary, self).pop(key, d)
        self.onchange()
        return ret

    def clear(self):
        ret = super(_EventDictionary, self).clear()
        self.onchange()
        return ret

    def update(self, d):
        ret = super(_EventDictionary, self).update(d)
        self.onchange()
        return ret

    def ischanged(self):
        return self.__version__ != self.__lastversion__

    def align_version(self):
        self.__lastversion__ = self.__version__

    @decorate_event
    def onchange(self):
        """Called on content change.
        """
        self.__version__ += 1
        return ()


class Tag(object):
    """
    Tag is the base class of the framework. It represents an element that can be added to the GUI,
    but it is not necessarily graphically representable.
    You can use this class for sending javascript code to the clients.
    """
    def __init__(self, attributes = {}, _type = '', _class = None,  **kwargs):
        """
        Args:
            attributes (dict): The attributes to be applied. 
           _type (str): HTML element type or ''
           _class (str): CSS class or '' (defaults to Class.__name__)
           id (str): the unique identifier for the class instance, usefull for public API definition.
        """
        self._parent = None

        self.kwargs = kwargs

        self._render_children_list = []

        self.children = _EventDictionary()
        self.attributes = _EventDictionary()  # properties as class id style
        self.style = _EventDictionary()  # used by Widget, but instantiated here to make gui_updater simpler

        self.ignore_update = False
        self.children.onchange.connect(self._need_update)
        self.attributes.onchange.connect(self._need_update)
        self.style.onchange.connect(self._need_update)

        self.type = _type
        self.attributes['id'] = str(id(self))

        #attribute['id'] can be overwritten to get a static Tag identifier
        self.attributes.update(attributes)

        # the runtime instances are processed every time a requests arrives, searching for the called method
        # if a class instance is not present in the runtimeInstances, it will
        # we not callable
        runtimeInstances[self.identifier] = self

        self._classes = []
        self.add_class(self.__class__.__name__ if _class == None else _class)

        #this variable will contain the repr of this tag, in order to avoid unuseful operations
        self._backup_repr = ''

    @property
    def identifier(self):
        return self.attributes['id']

    def set_identifier(self, new_identifier):
        """Allows to set a unique id for the Tag.

        Args:
            new_identifier (str): a unique id for the tag
        """
        self.attributes['id'] = new_identifier
        runtimeInstances[new_identifier] = self

    def repr(self, changed_widgets={}):
        """It is used to automatically represent the object to HTML format
        packs all the attributes, children and so on.

        Args:
            changed_widgets (dict): A dictionary containing a collection of tags that have to be updated.
                The tag that have to be updated is the key, and the value is its textual repr.
        """
        local_changed_widgets = {}
        innerHTML = ''
        for k in self._render_children_list:
            s = self.children[k]
            if isinstance(s, Tag):
                innerHTML = innerHTML + s.repr(local_changed_widgets)
            elif isinstance(s, type('')):
                innerHTML = innerHTML + s
            elif isinstance(s, type(u'')):
                innerHTML = innerHTML + s.encode('utf-8')
            else:
                innerHTML = innerHTML + repr(s)

        if self._ischanged() or ( len(local_changed_widgets) > 0 ):
            self._backup_repr = ''.join(('<', self.type, ' ', self._repr_attributes, '>', 
                                        innerHTML, '</', self.type, '>'))
            #faster but unsupported before python3.6
            #self._backup_repr = f'<{self.type} {self._repr_attributes}>{innerHTML}</{self.type}>'
        if self._ischanged():
            # if self changed, no matter about the children because will be updated the entire parent
            # and so local_changed_widgets is not merged
            changed_widgets[self] = self._backup_repr
            self._set_updated()
        else:
            changed_widgets.update(local_changed_widgets)
        return self._backup_repr

    def _need_update(self, emitter=None):
        #if there is an emitter, it means self is the actual changed widget
        if emitter:
            tmp = dict(self.attributes)
            tmp['style'] = jsonize(self.style)
            self._repr_attributes = ' '.join('%s="%s"' % (k, v) if v is not None else k for k, v in
                                                tmp.items())
        if not self.ignore_update:
            if self.get_parent():
                self.get_parent()._need_update()

    def _ischanged(self):
        return self.children.ischanged() or self.attributes.ischanged() or self.style.ischanged()

    def _set_updated(self):
        self.children.align_version()
        self.attributes.align_version()
        self.style.align_version()

    def disable_refresh(self):
        self.ignore_update = True

    def enable_refresh(self):
        self.ignore_update = False

    def add_class(self, cls):
        self._classes.append(cls)
        self.attributes['class'] = ' '.join(self._classes) if self._classes else ''

    def remove_class(self, cls):
        try:
            self._classes.remove(cls)
        except ValueError:
            pass
        self.attributes['class'] = ' '.join(self._classes) if self._classes else ''

    def add_child(self, key, value):
        """Adds a child to the Tag

        To retrieve the child call get_child or access to the Tag.children[key] dictionary.

        Args:
            key (str):  Unique child's identifier, or iterable of keys
            value (Tag, str): can be a Tag, an iterable of Tag or a str. In case of iterable
                of Tag is a dict, each item's key is set as 'key' param
        """
        if type(value) in (list, tuple, dict):
            if type(value)==dict:
                for k in value.keys():
                    self.add_child(k, value[k])
                return
            i = 0
            for child in value:
                self.add_child(key[i], child)
                i = i + 1
            return

        if hasattr(value, 'attributes'):
            value.attributes['data-parent-widget'] = self.identifier
            value._parent = self

        if key in self.children:
            self._render_children_list.remove(key)
        self._render_children_list.append(key)

        self.children[key] = value

    def get_child(self, key):
        """Returns the child identified by 'key'

        Args:
            key (str): Unique identifier of the child.
        """
        return self.children[key]

    def get_parent(self):
        """Returns the parent tag instance or None where not applicable
        """

        return self._parent

    def empty(self):
        """remove all children from the widget"""
        for k in list(self.children.keys()):
            self.remove_child(self.children[k])

    def remove_child(self, child):
        """Removes a child instance from the Tag's children.

        Args:
            child (Tag): The child to be removed.
        """
        if child in self.children.values() and hasattr(child, 'identifier'):
            for k in self.children.keys():
                if hasattr(self.children[k], 'identifier'):
                    if self.children[k].identifier == child.identifier:
                        if k in self._render_children_list:
                            self._render_children_list.remove(k)
                        self.children.pop(k)
                        # when the child is removed we stop the iteration
                        # this implies that a child replication should not be allowed
                        break


class Widget(Tag, EventSource):
    """Base class for gui widgets.

    Widget can be used as generic container. You can add children by the append(value, key) function.
    Widget can be arranged in absolute positioning (assigning style['top'] and style['left'] attributes to the children
    or in a simple auto-alignment.
    You can decide the horizontal or vertical arrangement by the function set_layout_orientation(layout_orientation)
    passing as parameter Widget.LAYOUT_HORIZONTAL or Widget.LAYOUT_VERTICAL.

    Tips:
    In html, it is a DIV tag
    The self.type attribute specifies the HTML tag representation
    The self.attributes[] attribute specifies the HTML attributes like 'style' 'class' 'id'
    The self.style[] attribute specifies the CSS style content like 'font' 'color'. It will be packed together with
    'self.attributes'.
    """

    # constants
    LAYOUT_HORIZONTAL = True
    LAYOUT_VERTICAL = False

    # some constants for the events
    EVENT_ONCLICK = 'onclick'
    EVENT_ONDBLCLICK = 'ondblclick'
    EVENT_ONMOUSEDOWN = 'onmousedown'
    EVENT_ONMOUSEMOVE = 'onmousemove'
    EVENT_ONMOUSEOVER = 'onmouseover'
    EVENT_ONMOUSEOUT = 'onmouseout'
    EVENT_ONMOUSELEAVE = 'onmouseleave'
    EVENT_ONMOUSEUP = 'onmouseup'
    EVENT_ONTOUCHMOVE = 'ontouchmove'
    EVENT_ONTOUCHSTART = 'ontouchstart'
    EVENT_ONTOUCHEND = 'ontouchend'
    EVENT_ONTOUCHENTER = 'ontouchenter'
    EVENT_ONTOUCHLEAVE = 'ontouchleave'
    EVENT_ONTOUCHCANCEL = 'ontouchcancel'
    EVENT_ONKEYDOWN = 'onkeydown'
    EVENT_ONKEYPRESS = 'onkeypress'
    EVENT_ONKEYUP = 'onkeyup'
    EVENT_ONCHANGE = 'onchange'
    EVENT_ONFOCUS = 'onfocus'
    EVENT_ONBLUR = 'onblur'
    EVENT_ONCONTEXTMENU = "oncontextmenu"
    EVENT_ONUPDATE = 'onupdate'

    @decorate_constructor_parameter_types([])
    def __init__(self, children = None, style = {}, *args, **kwargs):

        """
        Args:
            children (Widget, or iterable of Widgets): The child to be appended. In case of a dictionary,
                each item's key is used as 'key' param for the single append.
            style (dict, or json str): The style properties to be applied. 
            width (int, str): An optional width for the widget (es. width=10 or width='10px' or width='10%').
            height (int, str): An optional height for the widget (es. height=10 or height='10px' or height='10%').
            margin (str): CSS margin specifier
            layout_orientation (Widget.LAYOUT_VERTICAL, Widget.LAYOUT_HORIZONTAL): Widget layout, only honoured for
                some widget types
        """
        if '_type' not in kwargs:
            kwargs['_type'] = 'div'

        super(Widget, self).__init__(**kwargs)
        EventSource.__init__(self, *args, **kwargs)

        self.oldRootWidget = None  # used when hiding the widget

        self.style['margin'] = kwargs.get('margin', '0px')
        self.set_layout_orientation(kwargs.get('layout_orientation', Widget.LAYOUT_VERTICAL))
        self.set_size(kwargs.get('width'), kwargs.get('height'))
        self.set_style(style)

        if children:
            self.append(children)

    def set_style(self, style):
        """ Allows to set style properties for the widget.
            Args:
                style (str or dict): The style property dictionary or json string.
        """
        if style is not None:
            try:
                self.style.update(style)
            except ValueError:
                for s in style.split(';'):
                    k, v = s.split(':', 1)
                    self.style[k.strip()] = v.strip()

    def set_enabled(self, enabled):
        if enabled:
            try:
                del self.attributes['disabled']
            except KeyError:
                pass
        else:
            self.attributes['disabled'] = None

    def set_size(self, width, height):
        """Set the widget size.

        Args:
            width (int or str): An optional width for the widget (es. width=10 or width='10px' or width='10%').
            height (int or str): An optional height for the widget (es. height=10 or height='10px' or height='10%').
        """
        if width is not None:
            try:
                width = to_pix(int(width))
            except ValueError:
                # now we know w has 'px or % in it'
                pass
            self.style['width'] = width

        if height is not None:
            try:
                height = to_pix(int(height))
            except ValueError:
                # now we know w has 'px or % in it'
                pass
            self.style['height'] = height

    def set_layout_orientation(self, layout_orientation):
        """For the generic Widget, this function allows to setup the children arrangement.

        Args:
            layout_orientation (Widget.LAYOUT_HORIZONTAL or Widget.LAYOUT_VERTICAL):
        """
        self.layout_orientation = layout_orientation

    def redraw(self):
        """Forces a graphic update of the widget"""
        self._need_update()

    def repr(self, changed_widgets={}):
        """Represents the widget as HTML format, packs all the attributes, children and so on.

        Args:
            client (App): Client instance.
            changed_widgets (dict): A dictionary containing a collection of widgets that have to be updated.
                The Widget that have to be updated is the key, and the value is its textual repr.
        """
        return super(Widget, self).repr(changed_widgets)

    def append(self, value, key=''):
        """Adds a child widget, generating and returning a key if not provided

        In order to access to the specific child in this way widget.children[key].

        Args:
            value (Widget, or iterable of Widgets): The child to be appended. In case of a dictionary,
                each item's key is used as 'key' param for the single append.
            key (str): The unique string identifier for the child. Ignored in case of iterable 'value'
                param.

        Returns:
            str: a key used to refer to the child for all future interaction, or a list of keys in case
                of an iterable 'value' param
        """
        if type(value) in (list, tuple, dict):
            if type(value)==dict:
                for k in value.keys():
                    self.append(value[k], k)
                return value.keys()
            keys = []
            for child in value:
                keys.append( self.append(child) )
            return keys

        if not isinstance(value, Widget):
            raise ValueError('value should be a Widget (otherwise use add_child(key,other)')

        key = value.identifier if key == '' else key
        self.add_child(key, value)

        if self.layout_orientation == Widget.LAYOUT_HORIZONTAL:
            if 'float' in self.children[key].style.keys():
                if not (self.children[key].style['float'] == 'none'):
                    self.children[key].style['float'] = 'left'
            else:
                self.children[key].style['float'] = 'left'

        return key

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def onfocus(self):
        """Called when the Widget gets focus."""
        return ()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def onblur(self):
        """Called when the Widget loses focus"""
        return ()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();")
    def onclick(self):
        """Called when the Widget gets clicked by the user with the left mouse button."""
        return ()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();")
    def ondblclick(self):
        """Called when the Widget gets double clicked by the user with the left mouse button."""
        return ()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();" \
                       "return false;")
    def oncontextmenu(self):
        """Called when the Widget gets clicked by the user with the right mouse button.
        """
        return ()

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=event.clientX-boundingBox.left;" \
            "params['y']=event.clientY-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def onmousedown(self, x, y):
        """Called when the user presses left or right mouse button over a Widget.

        Args:
            x (float): position of the mouse inside the widget
            y (float): position of the mouse inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=event.clientX-boundingBox.left;" \
            "params['y']=event.clientY-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def onmouseup(self, x, y):
        """Called when the user releases left or right mouse button over a Widget.

        Args:
            x (float): position of the mouse inside the widget
            y (float): position of the mouse inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();" \
                       "return false;")
    def onmouseout(self):
        """Called when the mouse cursor moves outside a Widget.

        Note: This event is often used together with the Widget.onmouseover event, which occurs when the pointer is
            moved onto a Widget, or onto one of its children.
        """
        return ()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();" \
                       "return false;")
    def onmouseleave(self):
        """Called when the mouse cursor moves outside a Widget.

        Note: This event is often used together with the Widget.onmouseenter event, which occurs when the mouse pointer
            is moved onto a Widget.

        Note: The Widget.onmouseleave event is similar to the Widget.onmouseout event. The only difference is that the
            onmouseleave event does not bubble (does not propagate up the Widgets tree).
        """
        return ()

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=event.clientX-boundingBox.left;" \
            "params['y']=event.clientY-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def onmousemove(self, x, y):
        """Called when the mouse cursor moves inside the Widget.

        Args:
            x (float): position of the mouse inside the widget
            y (float): position of the mouse inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=parseInt(event.changedTouches[0].clientX)-boundingBox.left;" \
            "params['y']=parseInt(event.changedTouches[0].clientY)-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def ontouchmove(self, x, y):
        """Called continuously while a finger is dragged across the screen, over a Widget.

        Args:
            x (float): position of the finger inside the widget
            y (float): position of the finger inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=parseInt(event.changedTouches[0].clientX)-boundingBox.left;" \
            "params['y']=parseInt(event.changedTouches[0].clientY)-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def ontouchstart(self, x, y):
        """Called when a finger touches the widget.

        Args:
            x (float): position of the finger inside the widget
            y (float): position of the finger inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=parseInt(event.changedTouches[0].clientX)-boundingBox.left;" \
            "params['y']=parseInt(event.changedTouches[0].clientY)-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def ontouchend(self, x, y):
        """Called when a finger is released from the widget.

        Args:
            x (float): position of the finger inside the widget
            y (float): position of the finger inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter, x, y)")
    @decorate_event_js("var params={};" \
            "var boundingBox = this.getBoundingClientRect();" \
            "params['x']=parseInt(event.changedTouches[0].clientX)-boundingBox.left;" \
            "params['y']=parseInt(event.changedTouches[0].clientY)-boundingBox.top;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);" \
            "event.stopPropagation();event.preventDefault();" \
            "return false;")
    def ontouchenter(self, x, y):
        """Called when a finger touches from outside to inside the widget.

        Args:
            x (float): position of the finger inside the widget
            y (float): position of the finger inside the widget
        """
        return (x, y)

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();" \
                       "return false;")
    def ontouchleave(self):
        """Called when a finger touches from inside to outside the widget.
        """
        return ()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
                       "event.stopPropagation();event.preventDefault();" \
                       "return false;")
    def ontouchcancel(self):
        """Called when a touch point has been disrupted in an implementation-specific manner
        (for example, too many touch points are created).
        """
        return ()

    @decorate_set_on_listener("(self, emitter, key, ctrl, shift, alt)")
    @decorate_event_js("""var params={};params['key']=event.key;
            params['ctrl']=event.ctrlKey;
            params['shift']=event.shiftKey;
            params['alt']=event.altKey;
            sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);
            event.stopPropagation();event.preventDefault();return false;""")
    def onkeyup(self, key, ctrl, shift, alt):
        """Called when user types and releases a key. 
        The widget should be able to receive the focus in order to emit the event.
        Assign a 'tabindex' attribute to make it focusable.
        
        Args:
            key (str): the character value
        """
        return (key, ctrl, shift, alt)

    @decorate_set_on_listener("(self, emitter, key, ctrl, shift, alt)")
    @decorate_event_js("""var params={};params['key']=event.key;
            params['ctrl']=event.ctrlKey;
            params['shift']=event.shiftKey;
            params['alt']=event.altKey;
            sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);
            event.stopPropagation();event.preventDefault();return false;""")
    def onkeydown(self, key, ctrl, shift, alt):
        """Called when user types and releases a key.
        The widget should be able to receive the focus in order to emit the event.
        Assign a 'tabindex' attribute to make it focusable.
        
        Args:
            key (str): the character value
        """
        return (key, ctrl, shift, alt)

    @decorate_explicit_alias_for_listener_registration
    def set_on_focus_listener(self, callback, *userdata):
        self.onfocus.connect(callback, *userdata)
        
    @decorate_explicit_alias_for_listener_registration
    def set_on_blur_listener(self, callback, *userdata):
        self.onblur.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_click_listener(self, callback, *userdata):
        self.onclick.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_dblclick_listener(self, callback, *userdata):
        self.ondblclick.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_contextmenu_listener(self, callback, *userdata):
        self.oncontextmenu.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_mousedown_listener(self, callback, *userdata):
        self.onmousedown.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_mouseup_listener(self, callback, *userdata):
        self.onmouseup.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_mouseout_listener(self, callback, *userdata):
        self.onmouseout.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_mouseleave_listener(self, callback, *userdata):
        self.onmouseleave.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_mousemove_listener(self, callback, *userdata):
        self.onmousemove.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_touchmove_listener(self, callback, *userdata):
        self.ontouchmove.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_touchstart_listener(self, callback, *userdata):
        self.ontouchstart.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_touchend_listener(self, callback, *userdata):
        self.ontouchend.connect(callback, *userdata)
        
    @decorate_explicit_alias_for_listener_registration
    def set_on_touchenter_listener(self, callback, *userdata):
        self.ontouchenter.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_touchleave_listener(self, callback, *userdata):
        self.ontouchleave.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_touchcancel_listener(self, callback, *userdata):
        self.ontouchcancel.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_key_up_listener(self, callback, *userdata):
        self.onkeyup.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_key_down_listener(self, callback, *userdata):
        self.onkeydown.connect(callback, *userdata)


class GridBox(Widget):
    """It contains widgets automatically aligning them to the grid.
    Does not permit children absolute positioning.

    In order to add children to this container, use the append(child, key) function.
    The key have to be string and determines the children positioning in the layout.

    Note: If you would absolute positioning, use the Widget container instead.
    """
    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        super(GridBox, self).__init__(*args, **kwargs)
        self.style.update({'display':'grid'})

    def define_grid(self, matrix):
        """Populates the Table with a list of tuples of strings.

        Args:
            matrix (list): list of iterables of strings (lists or someting else). 
                Items in the matrix have to correspond to a key for the children.
        """
        self.style['grid-template-areas'] = ''.join("'%s'"%(' '.join(x)) for x in matrix) 

    def append(self, value, key=''):
        """Adds a child widget, generating and returning a key if not provided

        In order to access to the specific child in this way widget.children[key].

        Args:
            value (Widget, or iterable of Widgets): The child to be appended. In case of a dictionary,
                each item's key is used as 'key' param for the single append.
            key (str): The unique string identifier for the child. Ignored in case of iterable 'value'
                param. The key have to correspond to a an element provided in the 'define_grid' method param.

        Returns:
            str: a key used to refer to the child for all future interaction, or a list of keys in case
                of an iterable 'value' param
        """
        if type(value) in (list, tuple, dict):
            if type(value)==dict:
                for k in value.keys():
                    self.append(value[k], k)
                return value.keys()
            keys = []
            for child in value:
                keys.append( self.append(child) )
            return keys

        if not isinstance(value, Widget):
            raise ValueError('value should be a Widget (otherwise use add_child(key,other)')

        key = value.identifier if key == '' else key
        self.add_child(key, value)
        value.style['grid-area'] = key
        value.style['position'] = 'static'

        return key
    
    def remove_child(self, child):
        if 'grid-area' in child.style.keys():
            del child.style['grid-area']
        super(GridBox,self).remove_child(child)

    def set_column_sizes(self, values):
        """Sets the size value for each column

        Args:
            values (iterable of int or str): values are treated as percentage.
        """
        self.style['grid-template-columns'] = ' '.join(map(lambda value: (str(value) if str(value).endswith('%') else str(value) + '%') , values))

    def set_row_sizes(self, values):
        """Sets the size value for each row

        Args:
            values (iterable of int or str): values are treated as percentage.
        """
        self.style['grid-template-rows'] = ' '.join(map(lambda value: (str(value) if str(value).endswith('%') else str(value) + '%') , values))
    
    def set_column_gap(self, value):
        """Sets the gap value between columns

        Args:
            value (int or str): gap value (i.e. 10 or "10px")
        """
        value = str(value) + 'px'
        value = value.replace('pxpx', 'px')
        self.style['grid-column-gap'] = value

    def set_row_gap(self, value):
        """Sets the gap value between rows

        Args:
            value (int or str): gap value (i.e. 10 or "10px")
        """
        value = str(value) + 'px'
        value = value.replace('pxpx', 'px')
        self.style['grid-row-gap'] = value


class HBox(Widget):
    """The purpose of this widget is to automatically horizontally aligning 
        the widgets that are appended to it.
    Does not permit children absolute positioning.

    In order to add children to this container, use the append(child, key) function.
    The key have to be numeric and determines the children order in the layout.

    Note: If you would absolute positioning, use the Widget container instead.
    """

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        super(HBox, self).__init__(*args, **kwargs)

        # fixme: support old browsers
        # http://stackoverflow.com/a/19031640
        self.style.update({'display':'flex', 'justify-content':'space-around', 
            'align-items':'center', 'flex-direction':'row'})

    def append(self, value, key=''):
        """It allows to add child widgets to this.
        The key allows to access the specific child in this way widget.children[key].
        The key have to be numeric and determines the children order in the layout.

        Args:
            value (Widget): Child instance to be appended.
            key (str): Unique identifier for the child. If key.isdigit()==True '0' '1'.. the value determines the order
            in the layout
        """
        if type(value) in (list, tuple, dict):
            if type(value)==dict:
                for k in value.keys():
                    self.append(value[k], k)
                return value.keys()
            keys = []
            for child in value:
                keys.append( self.append(child) )
            return keys
        
        key = str(key)
        if not isinstance(value, Widget):
            raise ValueError('value should be a Widget (otherwise use add_child(key,other)')

        if 'left' in value.style.keys():
            del value.style['left']
        if 'right' in value.style.keys():
            del value.style['right']

        if not 'order' in value.style.keys():
            value.style.update({'position':'static', 'order':'-1'})

        if key.isdigit():
            value.style['order'] = key

        key = value.identifier if key == '' else key
        self.add_child(key, value)

        return key


class VBox(HBox):
    """The purpose of this widget is to automatically vertically aligning 
        the widgets that are appended to it.
    Does not permit children absolute positioning.

    In order to add children to this container, use the append(child, key) function.
    The key have to be numeric and determines the children order in the layout.

    Note: If you would absolute positioning, use the Widget container instead.
    """

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        super(VBox, self).__init__(*args, **kwargs)
        self.style['flex-direction'] = 'column'


class TabBox(Widget):

    # create a structure like the following
    #
    # <div class="wrapper">
    # <ul class="tabs clearfix">
    #   <li><a href="#tab1" class="active">Tab 1</a></li>
    #   <li><a href="#tab2">Tab 2</a></li>
    #   <li><a href="#tab3">Tab 3</a></li>
    #   <li><a href="#tab4">Tab 4</a></li>
    #   <li><a href="#tab5">Tab 5</a></li>
    # </ul>
    # <section id="first-tab-group">
    #   <div id="tab1">

    def __init__(self, *args, **kwargs):
        super(TabBox, self).__init__(*args, **kwargs)

        self._section = Tag(_type='section')

        self._tab_cbs = {}

        self._ul = Tag(_type='ul', _class='tabs clearfix')
        self.add_child('ul', self._ul)

        self.add_child('section', self._section)

        # maps tabs to their corresponding tab header
        self._tabs = {}

        self._tablist = list()

    def _fix_tab_widths(self):
        tab_w = 100.0 / len(self._tabs)
        for a, li, holder in self._tabs.values():
            li.style['float'] = "left"
            li.style['width'] = "%.1f%%" % tab_w

    def _on_tab_pressed(self, _a, _li, _holder):
        # remove active on all tabs, and hide their contents
        for a, li, holder in self._tabs.values():
            a.remove_class('active')
            holder.style['display'] = 'none'

        _a.add_class('active')
        _holder.style['display'] = 'block'

        # call other callbacks
        cb = self._tab_cbs[_holder.identifier]
        if cb is not None:
            cb()

    def select_by_widget(self, widget):
        """ shows a tab identified by the contained widget """
        for a, li, holder in self._tabs.values():
            if holder.children['content'] == widget:
                self._on_tab_pressed(a, li, holder)
                return

    def select_by_name(self, name):
        """ shows a tab identified by the name """
        for a, li, holder in self._tabs.values():
            if a.children['text'] == name:
                self._on_tab_pressed(a, li, holder)
                return

    def select_by_index(self, index):
        """ shows a tab identified by its index """
        self._on_tab_pressed(*self._tablist[index])

    def add_tab(self, widget, name, tab_cb):

        holder = Tag(_type='div', _class='')
        holder.add_child('content', widget)

        li = Tag(_type='li', _class='')

        a = Widget(_type='a', _class='')
        if len(self._tabs) == 0:
            a.add_class('active')
            holder.style['display'] = 'block'
        else:
            holder.style['display'] = 'none'

        # we need a href attribute for hover effects to work, and while empty
        # href attributes are valid html5, this didn't seem reliable in testing.
        # finally, '#' moves to the top of the page, and '#abcd' leaves browser history.
        # so no-op JS is the least of all evils
        a.attributes['href'] = 'javascript:void(0);'

        self._tab_cbs[holder.identifier] = tab_cb
        a.onclick.connect(self._on_tab_pressed, li, holder)

        a.add_child('text', name)
        li.add_child('a', a)
        self._ul.add_child(li.identifier, li)

        self._section.add_child(holder.identifier, holder)

        self._tabs[holder.identifier] = (a, li, holder)
        self._fix_tab_widths()
        self._tablist.append((a, li, holder))
        return holder.identifier


# noinspection PyUnresolvedReferences
class _MixinTextualWidget(object):
    def set_text(self, text):
        """
        Sets the text label for the Widget.

        Args:
            text (str): The string label of the Widget.
        """
        self.add_child('text', text)

    def get_text(self):
        """
        Returns:
            str: The text content of the Widget. You can set the text content with set_text(text).
        """
        if 'text' not in self.children.keys():
            return ''
        return self.get_child('text')


class Button(Widget, _MixinTextualWidget):
    """The Button widget. Have to be used in conjunction with its event onclick.
        Use Widget.onclick.connect in order to register the listener.
    """
    @decorate_constructor_parameter_types([str])
    def __init__(self, text='', *args, **kwargs):
        """
        Args:
            text (str): The text that will be displayed on the button.
            kwargs: See Widget.__init__()
        """
        super(Button, self).__init__(*args, **kwargs)
        self.type = 'button'
        self.set_text(text)


class TextInput(Widget, _MixinTextualWidget):
    """Editable multiline/single_line text area widget. You can set the content by means of the function set_text or
     retrieve its content with get_text.
    """

    @decorate_constructor_parameter_types([bool, str])
    def __init__(self, single_line=True, hint='', *args, **kwargs):
        """
        Args:
            single_line (bool): Determines if the TextInput have to be single_line. A multiline TextInput have a gripper
                                that allows the resize.
            hint (str): Sets a hint using the html placeholder attribute.
            kwargs: See Widget.__init__()
        """
        super(TextInput, self).__init__(*args, **kwargs)
        self.type = 'textarea'

        self.single_line = single_line
        if single_line:
            self.style['resize'] = 'none'
            self.attributes['rows'] = '1'
            self.attributes[self.EVENT_ONKEYDOWN] = "if((event.charCode||event.keyCode)==13){" \
                "event.keyCode = 0;event.charCode = 0; document.getElementById('%(id)s').blur();" \
                "return false;}" % {'id': self.identifier}

        self.set_value('')

        if hint:
            self.attributes['placeholder'] = hint

        self.attributes['autocomplete'] = 'off'

        self.attributes[Widget.EVENT_ONCHANGE] = \
            "var params={};params['new_value']=document.getElementById('%(emitter_identifier)s').value;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);"% \
            {'emitter_identifier': str(self.identifier), 'event_name': Widget.EVENT_ONCHANGE}

    def set_value(self, text):
        """Sets the text content.

        Args:
            text (str): The string content that have to be appended as standard child identified by the key 'text'
        """
        if self.single_line:
            text = text.replace('\n', '')
        self.set_text(text)

    def get_value(self):
        """
        Returns:
            str: The text content of the TextInput. You can set the text content with set_text(text).
        """
        return self.get_text()

    @decorate_set_on_listener("(self, emitter, new_value)")
    @decorate_event
    def onchange(self, new_value):
        """Called when the user finishes to edit the TextInput content.

        Args:
            new_value (str): the new string content of the TextInput.
        """
        self.set_value(new_value)
        return (new_value, )

    @decorate_set_on_listener("(self, emitter, new_value)")
    @decorate_event_js("""var elem=document.getElementById('%(emitter_identifier)s');elem.value = elem.value.split('\\n').join('');
            var params={};params['new_value']=elem.value;
            sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);""")
    def onkeyup(self, new_value):
        """Called when user types and releases a key into the TextInput
        
        Args:
            new_value (str): the new string content of the TextInput
        """
        self.disable_refresh()
        self.set_value(new_value)
        self.enable_refresh()
        self._set_updated()
        return (new_value, )

    @decorate_set_on_listener("(self, emitter, new_value)")
    @decorate_event_js("var params={};params['new_value']=document.getElementById('%(emitter_identifier)s').value;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);if((event.charCode||event.keyCode)==13){" \
            "event.keyCode = 0;event.charCode = 0; document.getElementById('%(emitter_identifier)s').blur(); return false;}")
    def onkeydown(self, new_value):
        """Called when the user types a key into the TextInput.

        Note: This event can't be registered together with Widget.onenter.

        Args:
            new_value (str): the new string content of the TextInput.
        """
        self.disable_refresh()
        self.set_value(new_value)
        self.enable_refresh()
        self._set_updated()
        return (new_value, )

    @decorate_set_on_listener("(self, emitter, new_value)")
    @decorate_event_js("""
            if (event.keyCode == 13) {
                var params={};
                params['new_value']=document.getElementById('%(emitter_identifier)s').value;
                document.getElementById('%(emitter_identifier)s').value = '';
                document.getElementById('%(emitter_identifier)s').onchange = '';
                sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);
                return false;
            }""")
    def onenter(self, new_value):
        """Called when the user types an ENTER into the TextInput.
        Note: This event can't be registered together with Widget.onkeydown.

        Args:
            new_value (str): the new string content of the TextInput.
        """
        self.disable_refresh()
        self.set_value(new_value)
        self.enable_refresh()
        self._set_updated()
        return (new_value, )

    @decorate_explicit_alias_for_listener_registration
    def set_on_change_listener(self, callback, *userdata):
        self.onchange.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_key_up_listener(self, callback, *userdata):
        self.onkeyup.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_key_down_listener(self, callback, *userdata):
        self.onkeydown.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_enter_listener(self, callback, *userdata):
        self.onenter.connect(callback, *userdata)


class Label(Widget, _MixinTextualWidget):
    """ Non editable text label widget. Set its content by means of set_text function, and retrieve its content with the
        function get_text.
    """

    @decorate_constructor_parameter_types([str])
    def __init__(self, text, *args, **kwargs):
        """
        Args:
            text (str): The string content that have to be displayed in the Label.
            kwargs: See Widget.__init__()
        """
        super(Label, self).__init__(*args, **kwargs)
        self.type = 'p'
        self.set_text(text)


class GenericDialog(Widget):
    """ Generic Dialog widget. It can be customized to create personalized dialog windows.
        You can setup the content adding content widgets with the functions add_field or add_field_with_label.
        The user can confirm or dismiss the dialog with the common buttons Cancel/Ok.
        Each field added to the dialog can be retrieved by its key, in order to get back the edited value. Use the function
         get_field(key) to retrieve the field.
        The Ok button emits the 'confirm_dialog' event. Register the listener to it with set_on_confirm_dialog_listener.
        The Cancel button emits the 'cancel_dialog' event. Register the listener to it with set_on_cancel_dialog_listener.
    """

    @decorate_constructor_parameter_types([str, str])
    def __init__(self, title='', message='', *args, **kwargs):
        """
        Args:
            title (str): The title of the dialog.
            message (str): The message description.
            kwargs: See Widget.__init__()
        """
        super(GenericDialog, self).__init__(*args, **kwargs)
        self.set_layout_orientation(Widget.LAYOUT_VERTICAL)
        self.style.update({'display':'block', 'overflow':'auto', 'margin':'0px auto'})

        if len(title) > 0:
            t = Label(title)
            t.add_class('DialogTitle')
            self.append(t)

        if len(message) > 0:
            m = Label(message)
            m.style['margin'] = '5px'
            self.append(m)

        self.container = Widget()
        self.container.style.update({'display':'block', 'overflow':'auto', 'margin':'5px'})
        self.container.set_layout_orientation(Widget.LAYOUT_VERTICAL)
        self.conf = Button('Ok')
        self.conf.set_size(100, 30)
        self.conf.style['margin'] = '3px'
        self.cancel = Button('Cancel')
        self.cancel.set_size(100, 30)
        self.cancel.style['margin'] = '3px'
        hlay = Widget(height=35)
        hlay.style['display'] = 'block'
        hlay.style['overflow'] = 'visible'
        hlay.append(self.conf)
        hlay.append(self.cancel)
        self.conf.style['float'] = 'right'
        self.cancel.style['float'] = 'right'

        self.append(self.container)
        self.append(hlay)

        self.conf.onclick.connect(self.confirm_dialog)
        self.cancel.onclick.connect(self.cancel_dialog)

        self.inputs = {}

        self._base_app_instance = None
        self._old_root_widget = None

    def add_field_with_label(self, key, label_description, field):
        """
        Adds a field to the dialog together with a descriptive label and a unique identifier.

        Note: You can access to the fields content calling the function GenericDialog.get_field(key).

        Args:
            key (str): The unique identifier for the field.
            label_description (str): The string content of the description label.
            field (Widget): The instance of the field Widget. It can be for example a TextInput or maybe
            a custom widget.
        """
        self.inputs[key] = field
        label = Label(label_description)
        label.style['margin'] = '0px 5px'
        label.style['min-width'] = '30%'
        container = HBox()
        container.style.update({'justify-content':'space-between', 'overflow':'auto', 'padding':'3px'})
        container.append(label, key='lbl' + key)
        container.append(self.inputs[key], key=key)
        self.container.append(container, key=key)

    def add_field(self, key, field):
        """
        Adds a field to the dialog with a unique identifier.

        Note: You can access to the fields content calling the function GenericDialog.get_field(key).

        Args:
            key (str): The unique identifier for the field.
            field (Widget): The widget to be added to the dialog, TextInput or any Widget for example.
        """
        self.inputs[key] = field
        container = HBox()
        container.style.update({'justify-content':'space-between', 'overflow':'auto', 'padding':'3px'})
        container.append(self.inputs[key], key=key)
        self.container.append(container, key=key)

    def get_field(self, key):
        """
        Args:
            key (str): The unique string identifier of the required field.

        Returns:
            Widget field instance added previously with methods GenericDialog.add_field or
            GenericDialog.add_field_with_label.
        """
        return self.inputs[key]

    @decorate_set_on_listener("(self,emitter)")
    @decorate_event
    def confirm_dialog(self, emitter):
        """Event generated by the OK button click.
        """
        self.hide()
        return ()

    @decorate_set_on_listener("(self,emitter)")
    @decorate_event
    def cancel_dialog(self, emitter):
        """Event generated by the Cancel button click."""
        self.hide()
        return ()

    def show(self, base_app_instance):
        self._base_app_instance = base_app_instance
        self._old_root_widget = self._base_app_instance.root
        self._base_app_instance.set_root_widget(self)

    def hide(self):
        self._base_app_instance.set_root_widget(self._old_root_widget)

    @decorate_explicit_alias_for_listener_registration
    def set_on_confirm_dialog_listener(self, callback, *userdata):
        self.confirm_dialog.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_cancel_dialog_listener(self, callback, *userdata):
        self.cancel_dialog.connect(callback, *userdata)


class InputDialog(GenericDialog):
    """Input Dialog widget. It can be used to query simple and short textual input to the user.
    The user can confirm or dismiss the dialog with the common buttons Cancel/Ok.
    The Ok button click or the ENTER key pression emits the 'confirm_dialog' event. Register the listener to it
    with set_on_confirm_dialog_listener.
    The Cancel button emits the 'cancel_dialog' event. Register the listener to it with set_on_cancel_dialog_listener.
    """

    @decorate_constructor_parameter_types([str, str, str])
    def __init__(self, title='Title', message='Message', initial_value='', *args, **kwargs):
        """
        Args:
            title (str): The title of the dialog.
            message (str): The message description.
            initial_value (str): The default content for the TextInput field.
            kwargs: See Widget.__init__()
        """
        super(InputDialog, self).__init__(title, message, *args, **kwargs)

        self.inputText = TextInput()
        self.inputText.onenter.connect(self.on_text_enter_listener)
        self.add_field('textinput', self.inputText)
        self.inputText.set_text(initial_value)

        self.confirm_dialog.connect(self.confirm_value)

    @decorate_set_on_listener("(self, emitter, value)")
    @decorate_event
    def on_text_enter_listener(self, widget, value):
        """event called pressing on ENTER key.

        propagates the string content of the input field
        """
        self.hide()
        return (value, )

    @decorate_set_on_listener("(self, emitter, value)")
    @decorate_event
    def confirm_value(self, widget):
        """Event called pressing on OK button."""
        return (self.inputText.get_text(),)

    @decorate_explicit_alias_for_listener_registration
    def set_on_confirm_value_listener(self, callback, *userdata):
        self.confirm_value.connect(callback, *userdata)


class ListView(Widget):
    """List widget it can contain ListItems. Add items to it by using the standard append(item, key) function or
    generate a filled list from a string list by means of the function new_from_list. Use the list in conjunction of
    its onselection event. Register a listener with ListView.onselection.connect.
    """

    @decorate_constructor_parameter_types([bool])
    def __init__(self, selectable = True, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(ListView, self).__init__(*args, **kwargs)
        self.type = 'ul'
        self._selected_item = None
        self._selected_key = None
        self._selectable = selectable

    @classmethod
    def new_from_list(cls, items, **kwargs):
        """Populates the ListView with a string list.

        Args:
            items (list): list of strings to fill the widget with.
        """
        obj = cls(**kwargs)
        for item in items:
            obj.append(ListItem(item))
        return obj

    def append(self, value, key=''):
        """Appends child items to the ListView. The items are accessible by list.children[key].

        Args:
            value (ListItem, or iterable of ListItems): The child to be appended. In case of a dictionary,
                each item's key is used as 'key' param for the single append.
            key (str): The unique string identifier for the child. Ignored in case of iterable 'value'
                param.
        """
        if isinstance(value, type('')) or isinstance(value, type(u'')):
            value = ListItem(value)

        keys = super(ListView, self).append(value, key=key)
        if type(value) in (list, tuple, dict):
            for k in keys:
                if not self.EVENT_ONCLICK in self.children[k].attributes:
                    self.children[k].onclick.connect(self.onselection)
                self.children[k].attributes['selected'] = False
        else:
            # if an event listener is already set for the added item, it will not generate a selection event
            if not self.EVENT_ONCLICK in value.attributes:
                value.onclick.connect(self.onselection)
            value.attributes['selected'] = False
        return keys

    def empty(self):
        """Removes all children from the list"""
        self._selected_item = None
        self._selected_key = None
        super(ListView, self).empty()

    @decorate_set_on_listener("(self,emitter,selectedKey)")
    @decorate_event
    def onselection(self, widget):
        """Called when a new item gets selected in the list."""
        self._selected_key = None
        for k in self.children:
            if self.children[k] == widget:  # widget is the selected ListItem
                self._selected_key = k
                if (self._selected_item is not None) and self._selectable:
                    self._selected_item.attributes['selected'] = False
                self._selected_item = self.children[self._selected_key]
                if self._selectable:
                    self._selected_item.attributes['selected'] = True
                break
        return (self._selected_key,)

    def get_item(self):
        """
        Returns:
            ListItem: The selected item or None
        """
        return self._selected_item

    def get_value(self):
        """
        Returns:
            str: The value of the selected item or None
        """
        if self._selected_item is None:
            return None
        return self._selected_item.get_value()

    def get_key(self):
        """
        Returns:
            str: The key of the selected item or None if no item is selected.
        """
        return self._selected_key

    def select_by_key(self, key):
        """Selects an item by its key.

        Args:
            key (str): The unique string identifier of the item that have to be selected.
        """
        self._selected_key = None
        self._selected_item = None
        for item in self.children.values():
            item.attributes['selected'] = False

        if key in self.children:
            self.children[key].attributes['selected'] = True
            self._selected_key = key
            self._selected_item = self.children[key]

    def set_value(self, value):
        self.select_by_value(value)

    def select_by_value(self, value):
        """Selects an item by the text content of the child.

        Args:
            value (str): Text content of the item that have to be selected.
        """
        self._selected_key = None
        self._selected_item = None
        for k in self.children:
            item = self.children[k]
            item.attributes['selected'] = False
            if value == item.get_value():
                self._selected_key = k
                self._selected_item = item
                self._selected_item.attributes['selected'] = True

    @decorate_explicit_alias_for_listener_registration
    def set_on_selection_listener(self, callback, *userdata):
        self.onselection.connect(callback, *userdata)


class ListItem(Widget, _MixinTextualWidget):
    """List item widget for the ListView.

    ListItems are characterized by a textual content. They can be selected from
    the ListView. Do NOT manage directly its selection by registering set_on_click_listener, use instead the events of
    the ListView.
    """

    @decorate_constructor_parameter_types([str])
    def __init__(self, text, *args, **kwargs):
        """
        Args:
            text (str, unicode): The textual content of the ListItem.
            kwargs: See Widget.__init__()
        """
        super(ListItem, self).__init__(*args, **kwargs)
        self.type = 'li'
        self.set_text(text)

    def get_value(self):
        """
        Returns:
            str: The text content of the ListItem
        """
        return self.get_text()


class DropDown(Widget):
    """Drop down selection widget. Implements the onchange(value) event. Register a listener for its selection change
    by means of the function DropDown.onchange.connect.
    """

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(DropDown, self).__init__(*args, **kwargs)
        self.type = 'select'
        self.attributes[self.EVENT_ONCHANGE] = \
            "var params={};params['value']=document.getElementById('%(id)s').value;" \
            "sendCallbackParam('%(id)s','%(evt)s',params);" % {'id': self.identifier,
                                                               'evt': self.EVENT_ONCHANGE}
        self._selected_item = None
        self._selected_key = None

    @classmethod
    def new_from_list(cls, items, **kwargs):
        item = None
        obj = cls(**kwargs)
        for item in items:
            obj.append(DropDownItem(item))
        if item is not None:
            try:
                obj.select_by_value(item)  # ensure one is selected
            except UnboundLocalError:
                pass
        return obj

    def append(self, value, key=''):
        if isinstance(value, type('')) or isinstance(value, type(u'')):
            value = DropDownItem(value)
        keys = super(DropDown, self).append(value, key=key)
        if len(self.children) == 1:
            self.select_by_value(value.get_value())
        return keys

    def empty(self):
        self._selected_item = None
        self._selected_key = None
        super(DropDown, self).empty()

    def select_by_key(self, key):
        """Selects an item by its unique string identifier.

        Args:
            key (str): Unique string identifier of the DropDownItem that have to be selected.
        """
        for item in self.children.values():
            if 'selected' in item.attributes:
                del item.attributes['selected']
        self.children[key].attributes['selected'] = 'selected'
        self._selected_key = key
        self._selected_item = self.children[key]

    def set_value(self, value):
        self.select_by_value(value)

    def select_by_value(self, value):
        """Selects a DropDownItem by means of the contained text-

        Args:
            value (str): Textual content of the DropDownItem that have to be selected.
        """
        self._selected_key = None
        self._selected_item = None
        for k in self.children:
            item = self.children[k]
            if item.get_text() == value:
                item.attributes['selected'] = 'selected'
                self._selected_key = k
                self._selected_item = item
            else:
                if 'selected' in item.attributes:
                    del item.attributes['selected']

    def get_item(self):
        """
        Returns:
            DropDownItem: The selected item or None.
        """
        return self._selected_item

    def get_value(self):
        """
        Returns:
            str: The value of the selected item or None.
        """
        if self._selected_item is None:
            return None
        return self._selected_item.get_value()

    def get_key(self):
        """
        Returns:
            str: The unique string identifier of the selected item or None.
        """
        return self._selected_key

    @decorate_set_on_listener("(self,emitter,new_value)")
    @decorate_event
    def onchange(self, value):
        """Called when a new DropDownItem gets selected.
        """
        log.debug('combo box. selected %s' % value)
        self.select_by_value(value)
        return (value, )

    @decorate_explicit_alias_for_listener_registration
    def set_on_change_listener(self, callback, *userdata):
        self.onchange.connect(callback, *userdata)


class DropDownItem(Widget, _MixinTextualWidget):
    """item widget for the DropDown"""

    @decorate_constructor_parameter_types([str])
    def __init__(self, text, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(DropDownItem, self).__init__(*args, **kwargs)
        self.type = 'option'
        self.set_text(text)

    def set_value(self, text):
        return self.set_text(text)

    def get_value(self):
        return self.get_text()


class Image(Widget):
    """image widget."""

    @decorate_constructor_parameter_types([str])
    def __init__(self, filename, *args, **kwargs):
        """
        Args:
            filename (str): an url to an image
            kwargs: See Widget.__init__()
        """
        super(Image, self).__init__(*args, **kwargs)
        self.type = 'img'
        self.attributes['src'] = filename

    def set_image(self, filename):
        """
        Args:
            filename (str): an url to an image
        """
        self.attributes['src'] = filename


class Table(Widget):
    """
    table widget - it will contains TableRow
    """

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(Table, self).__init__(*args, **kwargs)

        self.type = 'table'
        self.style['float'] = 'none'

    @classmethod
    def new_from_list(cls, content, fill_title=True, **kwargs):
        """Populates the Table with a list of tuples of strings.

        Args:
            content (list): list of tuples of strings. Each tuple is a row.
            fill_title (bool): if true, the first tuple in the list will
                be set as title
        """
        obj = cls(**kwargs)
        obj.append_from_list(content, fill_title)
        return obj

    def append_from_list(self, content, fill_title=False):
        """
        Appends rows created from the data contained in the provided
        list of tuples of strings. The first tuple of the list can be
        set as table title.

        Args:
            content (list): list of tuples of strings. Each tuple is a row.
            fill_title (bool): if true, the first tuple in the list will
                be set as title.
        """
        row_index = 0
        for row in content:
            tr = TableRow()
            column_index = 0
            for item in row:
                if row_index == 0 and fill_title:
                    ti = TableTitle(item)
                else:
                    ti = TableItem(item)
                tr.append(ti, str(column_index))
                column_index = column_index + 1
            self.append(tr, str(row_index))
            row_index = row_index + 1

    def append(self, value, key=''):
        keys = super(Table, self).append(value, key)
        if type(value) in (list, tuple, dict):
            for k in keys:
                self.children[k].on_row_item_click.connect(self.on_table_row_click)
        else:
            value.on_row_item_click.connect(self.on_table_row_click)
        return keys

    @decorate_set_on_listener("(self, emitter, row, item)")
    @decorate_event
    def on_table_row_click(self, row, item):
        return (row, item)

    @decorate_explicit_alias_for_listener_registration
    def set_on_table_row_click_listener(self, callback, *userdata):
        self.on_table_row_click.connect(callback, *userdata)


class TableWidget(Table):
    """
    Basic table model widget.
    Each item is addressed by stringified integer key in the children dictionary.
    """

    @decorate_constructor_parameter_types([int, int, bool, bool])
    def __init__(self, n_rows, n_columns, use_title=True, editable=False, *args, **kwargs):
        """
        Args:
            use_title (bool): enable title bar. Note that the title bar is
                treated as a row (it is comprised in n_rows count)
            n_rows (int): number of rows to create
            n_columns (int): number of columns to create
            kwargs: See Widget.__init__()
        """
        super(TableWidget, self).__init__(*args, **kwargs)
        self._editable = editable
        self.set_use_title(use_title)
        self._column_count = 0
        self.set_column_count(n_columns)
        self.set_row_count(n_rows)
        self.style['display'] = 'table'

    def set_use_title(self, use_title):
        """Returns the TableItem instance at row, column cordinates

        Args:
            use_title (bool): enable title bar.
        """
        self._use_title = use_title
        self._update_first_row()

    def _update_first_row(self):
        cl = TableEditableItem if self._editable else TableItem
        if self._use_title:
            cl = TableTitle

        if len(self.children) > 0:
            for c_key in self.children['0'].children.keys():
                instance = cl(self.children['0'].children[c_key].get_text())
                self.children['0'].children[c_key] = instance
                #here the cells of the first row are overwritten and aren't appended by the standard Table.append
                # method. We have to restore de standard on_click internal listener in order to make it working
                # the Table.on_table_row_click functionality
                self.children['0'].children[c_key].onclick.connect(self.children['0'].on_row_item_click)

    def item_at(self, row, column):
        """Returns the TableItem instance at row, column cordinates

        Args:
            row (int): zero based index
            column (int): zero based index
        """
        return self.children[str(row)].children[str(column)]

    def item_coords(self, table_item):
        """Returns table_item's (row, column) cordinates.
        Returns None in case of item not found.

        Args:
            table_item (TableItem): an item instance
        """
        for row_key in self.children.keys():
            for item_key in self.children[row_key].children.keys():
                if self.children[row_key].children[item_key] == table_item:
                    return (int(row_key), int(item_key))
        return None

    def column_count(self):
        """Returns table's columns count.
        """
        return self._column_count

    def row_count(self):
        """Returns table's rows count (the title is considered as a row).
        """
        return len(self.children)

    def set_row_count(self, count):
        """Sets the table row count.

        Args:
            count (int): number of rows
        """
        current_row_count = self.row_count()
        current_column_count = self.column_count()
        if count > current_row_count:
            cl = TableEditableItem if self._editable else TableItem
            for i in range(current_row_count, count):
                tr = TableRow()
                for c in range(0, current_column_count):
                    tr.append(cl(), str(c))
                    if self._editable:
                        tr.children[str(c)].onchange.connect(
                            self.on_item_changed, int(i), int(c))
                self.append(tr, str(i))
            self._update_first_row()
        elif count < current_row_count:
            for i in range(count, current_row_count):
                self.remove_child(self.children[str(i)])

    def set_column_count(self, count):
        """Sets the table column count.

        Args:
            count (int): column of rows
        """
        current_row_count = self.row_count()
        current_column_count = self.column_count()
        if count > current_column_count:
            cl = TableEditableItem if self._editable else TableItem
            for r_key in self.children.keys():
                row = self.children[r_key]
                for i in range(current_column_count, count):
                    row.append(cl(), str(i))
                    if self._editable:
                        row.children[str(i)].onchange.connect(
                            self.on_item_changed, int(r_key), int(i))
            self._update_first_row()
        elif count < current_column_count:
            for row in self.children.values():
                for i in range(count, current_column_count):
                    row.remove_child(row.children[str(i)])
        self._column_count = count

    @decorate_set_on_listener("(self, emitter, item, new_value, row, column)")
    @decorate_event
    def on_item_changed(self, item, new_value, row, column):
        """Event for the item change.

        Args:
            emitter (TableWidget): The emitter of the event.
            item (TableItem): The TableItem instance.
            new_value (str): New text content.
            row (int): row index.
            column (int): column index.
        """
        return (item, new_value, row, column)

    @decorate_explicit_alias_for_listener_registration
    def set_on_item_changed_listener(self, callback, *userdata):
        self.on_item_changed.connect(callback, *userdata)


class TableRow(Widget):
    """
    row widget for the Table - it will contains TableItem
    """

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(TableRow, self).__init__(*args, **kwargs)
        self.type = 'tr'
        self.style['float'] = 'none'

    def append(self, value, key=''):
        if isinstance(value, type('')) or isinstance(value, type(u'')):
            value = TableItem(value)
        keys = super(TableRow, self).append(value, key)
        if type(value) in (list, tuple, dict):
            for k in keys:
                self.children[k].onclick.connect(self.on_row_item_click)
        else:
            value.onclick.connect(self.on_row_item_click)
        return keys

    @decorate_set_on_listener("(self, emitter, item)")
    @decorate_event
    def on_row_item_click(self, item):
        """Event on item click.

        Note: This is internally used by the Table widget in order to generate the
            Table.on_table_row_click event.
            Use Table.on_table_row_click instead.
        Args:
            emitter (TableRow): The emitter of the event.
            item (TableItem): The clicked TableItem.
        """
        return (item, )

    @decorate_explicit_alias_for_listener_registration
    def set_on_row_item_click_listener(self, callback, *userdata):
        self.on_row_item_click.connect(callback, *userdata)


class TableEditableItem(Widget, _MixinTextualWidget):
    """item widget for the TableRow."""

    @decorate_constructor_parameter_types([str])
    def __init__(self, text='', *args, **kwargs):
        """
        Args:
            text (str):
            kwargs: See Widget.__init__()
        """
        super(TableEditableItem, self).__init__(*args, **kwargs)
        self.type = 'td'
        self.editInput = TextInput()
        self.append(self.editInput)
        self.editInput.onchange.connect(self.onchange)
        self.get_text = self.editInput.get_text
        self.set_text = self.editInput.set_text
        self.set_text(text)

    @decorate_set_on_listener("(self, emitter, new_value)")
    @decorate_event
    def onchange(self, emitter, new_value):
        return (new_value, )

    @decorate_explicit_alias_for_listener_registration
    def set_on_change_listener(self, callback, *userdata):
        self.onchange.connect(callback, *userdata)


class TableItem(Widget, _MixinTextualWidget):
    """item widget for the TableRow."""

    @decorate_constructor_parameter_types([str])
    def __init__(self, text='', *args, **kwargs):
        """
        Args:
            text (str):
            kwargs: See Widget.__init__()
        """
        super(TableItem, self).__init__(*args, **kwargs)
        self.type = 'td'
        self.set_text(text)


class TableTitle(TableItem, _MixinTextualWidget):
    """title widget for the table."""

    @decorate_constructor_parameter_types([str])
    def __init__(self, text='', *args, **kwargs):
        """
        Args:
            text (str):
            kwargs: See Widget.__init__()
        """
        super(TableTitle, self).__init__(text, *args, **kwargs)
        self.type = 'th'


class Input(Widget):

    @decorate_constructor_parameter_types([str, str])
    def __init__(self, input_type='', default_value='', *args, **kwargs):
        """
        Args:
            input_type (str): HTML5 input type
            default_value (str):
            kwargs: See Widget.__init__()
        """
        kwargs['_class'] = input_type
        super(Input, self).__init__(*args, **kwargs)
        self.type = 'input'

        self.attributes['value'] = str(default_value)
        self.attributes['type'] = input_type
        self.attributes['autocomplete'] = 'off'
        self.attributes[Widget.EVENT_ONCHANGE] = \
            "var params={};params['value']=document.getElementById('%(emitter_identifier)s').value;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);"% \
        {'emitter_identifier':str(self.identifier), 'event_name':Widget.EVENT_ONCHANGE}

    def set_value(self, value):
        self.attributes['value'] = str(value)

    def get_value(self):
        """returns the new text value."""
        return self.attributes['value']

    @decorate_set_on_listener("(self, emitter, value)")
    @decorate_event
    def onchange(self, value):
        self.attributes['value'] = value
        return (value, )

    def set_read_only(self, readonly):
        if readonly:
            self.attributes['readonly'] = None
        else:
            try:
                del self.attributes['readonly']
            except KeyError:
                pass

    @decorate_explicit_alias_for_listener_registration
    def set_on_change_listener(self, callback, *userdata):
        self.onchange.connect(callback, *userdata)


class CheckBoxLabel(Widget):

    @decorate_constructor_parameter_types([str, bool, str])
    def __init__(self, label='', checked=False, user_data='', **kwargs):
        """
        Args:
            label (str):
            checked (bool):
            user_data (str):
            kwargs: See Widget.__init__()
        """
        super(CheckBoxLabel, self).__init__(**kwargs)
        self.set_layout_orientation(Widget.LAYOUT_HORIZONTAL)
        self._checkbox = CheckBox(checked, user_data)
        self._label = Label(label)
        self.append(self._checkbox, key='checkbox')
        self.append(self._label, key='label')

        self.set_value = self._checkbox.set_value
        self.get_value = self._checkbox.get_value

        self._checkbox.onchange.connect(self.onchange)

    @decorate_set_on_listener("(self, emitter, value)")
    @decorate_event
    def onchange(self, widget, value):
        return (value, )

    @decorate_explicit_alias_for_listener_registration
    def set_on_change_listener(self, callback, *userdata):
        self.onchange.connect(callback, *userdata)


class CheckBox(Input):
    """check box widget useful as numeric input field implements the onchange event."""

    @decorate_constructor_parameter_types([bool, str])
    def __init__(self, checked=False, user_data='', **kwargs):
        """
        Args:
            checked (bool):
            user_data (str):
            kwargs: See Widget.__init__()
        """
        super(CheckBox, self).__init__('checkbox', user_data, **kwargs)
        self.set_value(checked)
        self.attributes[Widget.EVENT_ONCHANGE] = \
            "var params={};params['value']=document.getElementById('%(emitter_identifier)s').checked;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);"% \
            {'emitter_identifier':str(self.identifier), 'event_name':Widget.EVENT_ONCHANGE}

    @decorate_set_on_listener("(self, emitter, value)")
    @decorate_event
    def onchange(self, value):
        value = value in ('True', 'true')
        self.set_value(value)
        return (value, )

    def set_value(self, checked, update_ui=1):
        if checked:
            self.attributes['checked'] = 'checked'
        else:
            if 'checked' in self.attributes:
                del self.attributes['checked']

    def get_value(self):
        """
        Returns:
            bool:
        """
        return 'checked' in self.attributes


class SpinBox(Input):
    """spin box widget useful as numeric input field implements the onchange event.
    """

    # noinspection PyShadowingBuiltins
    @decorate_constructor_parameter_types([int, int, int, int])
    def __init__(self, default_value=100, min_value=100, max_value=5000, step=1, allow_editing=True, **kwargs):
        """
        Args:
            default_value (int, float, str):
            min (int, float, str):
            max (int, float, str):
            step (int, float, str):
            allow_editing (bool): If true allow editing the value using backpspace/delete/enter (othewise
            only allow entering numbers)
            kwargs: See Widget.__init__()
        """
        super(SpinBox, self).__init__('number', str(default_value), **kwargs)
        self.attributes['min'] = str(min_value)
        self.attributes['max'] = str(max_value)
        self.attributes['step'] = str(step)
        # eat non-numeric input (return false to stop propogation of event to onchange listener)
        js = 'var key = event.keyCode || event.charCode;'
        js += 'return (event.charCode >= 48 && event.charCode <= 57)'
        if allow_editing:
            js += ' || (key == 8 || key == 46 || key == 45|| key == 44 )'  # allow backspace and delete and minus and coma
            js += ' || (key == 13)'  # allow enter
        self.attributes[self.EVENT_ONKEYPRESS] = '%s;' % js
        #FIXES Edge behaviour where onchange event not fires in case of key arrow Up or Down
        self.attributes[self.EVENT_ONKEYUP] = \
            "var key = event.keyCode || event.charCode;" \
            "if(key==13){var params={};params['value']=document.getElementById('%(id)s').value;" \
            "sendCallbackParam('%(id)s','%(evt)s',params); return true;}" \
            "return false;" % {'id': self.identifier, 'evt': self.EVENT_ONCHANGE}


class Slider(Input):

    # noinspection PyShadowingBuiltins
    @decorate_constructor_parameter_types([str, int, int, int])
    def __init__(self, default_value='', min=0, max=10000, step=1, **kwargs):
        """
        Args:
            default_value (str):
            min (int):
            max (int):
            step (int):
            kwargs: See Widget.__init__()
        """
        super(Slider, self).__init__('range', default_value, **kwargs)
        self.attributes['min'] = str(min)
        self.attributes['max'] = str(max)
        self.attributes['step'] = str(step)
        self.attributes[Widget.EVENT_ONCHANGE] = \
            "var params={};params['value']=document.getElementById('%(emitter_identifier)s').value;" \
            "sendCallbackParam('%(emitter_identifier)s','%(event_name)s',params);"% \
            {'emitter_identifier':str(self.identifier), 'event_name':Widget.EVENT_ONCHANGE}

    @decorate_set_on_listener("(self, emitter, value)")
    @decorate_event
    def oninput(self, value):
        return (value, )

    @decorate_explicit_alias_for_listener_registration
    def set_oninput_listener(self, callback, *userdata):
        self.oninput.connect(callback, *userdata)


class ColorPicker(Input):

    @decorate_constructor_parameter_types([str])
    def __init__(self, default_value='#995500', **kwargs):
        """
        Args:
            default_value (str): hex rgb color string (#rrggbb)
            kwargs: See Widget.__init__()
        """
        super(ColorPicker, self).__init__('color', default_value, **kwargs)


class Date(Input):

    @decorate_constructor_parameter_types([str])
    def __init__(self, default_value='2015-04-13', **kwargs):
        """
        Args:
            default_value (str): date string (yyyy-mm-dd)
            kwargs: See Widget.__init__()
        """
        super(Date, self).__init__('date', default_value, **kwargs)


class GenericObject(Widget):
    """
    GenericObject widget - allows to show embedded object like pdf,swf..
    """

    @decorate_constructor_parameter_types([str])
    def __init__(self, filename, **kwargs):
        """
        Args:
            filename (str): URL
            kwargs: See Widget.__init__()
        """
        super(GenericObject, self).__init__(**kwargs)
        self.type = 'object'
        self.attributes['data'] = filename


class FileFolderNavigator(Widget):
    """FileFolderNavigator widget."""

    @decorate_constructor_parameter_types([bool, str, bool, bool])
    def __init__(self, multiple_selection, selection_folder, allow_file_selection, allow_folder_selection, **kwargs):
        super(FileFolderNavigator, self).__init__(**kwargs)
        self.set_layout_orientation(Widget.LAYOUT_VERTICAL)
        self.style['width'] = '100%'

        self.multiple_selection = multiple_selection
        self.allow_file_selection = allow_file_selection
        self.allow_folder_selection = allow_folder_selection
        self.selectionlist = []
        self.controlsContainer = Widget()
        self.controlsContainer.set_size('100%', '30px')
        self.controlsContainer.style['display'] = 'flex'
        self.controlsContainer.set_layout_orientation(Widget.LAYOUT_HORIZONTAL)
        self.controlBack = Button('Up')
        self.controlBack.set_size('10%', '100%')
        self.controlBack.onclick.connect(self.dir_go_back)
        self.controlGo = Button('Go >>')
        self.controlGo.set_size('10%', '100%')
        self.controlGo.onclick.connect(self.dir_go)
        self.pathEditor = TextInput()
        self.pathEditor.set_size('80%', '100%')
        self.pathEditor.style['resize'] = 'none'
        self.pathEditor.attributes['rows'] = '1'
        self.controlsContainer.append(self.controlBack)
        self.controlsContainer.append(self.pathEditor)
        self.controlsContainer.append(self.controlGo)

        self.itemContainer = Widget(width='100%',height=300)

        self.append(self.controlsContainer)
        self.append(self.itemContainer, key='items')  # defined key as this is replaced later

        self.folderItems = list()

        # fixme: we should use full paths and not all this chdir stuff
        self.chdir(selection_folder)  # move to actual working directory
        self._last_valid_path = selection_folder

    def get_selection_list(self):
        return self.selectionlist

    def populate_folder_items(self, directory):
        def _sort_files(a, b):
            if os.path.isfile(a) and os.path.isdir(b):
                return 1
            elif os.path.isfile(b) and os.path.isdir(a):
                return -1
            else:
                try:
                    if a[0] == '.':
                        a = a[1:]
                    if b[0] == '.':
                        b = b[1:]
                    return (1 if a.lower() > b.lower() else -1)
                except (IndexError, ValueError):
                    return (1 if a > b else -1)

        log.debug("FileFolderNavigator - populate_folder_items")

        if pyLessThan3:
            directory = directory.decode('utf-8')

        l = os.listdir(directory)
        l.sort(key=functools.cmp_to_key(_sort_files))

        # used to restore a valid path after a wrong edit in the path editor
        self._last_valid_path = directory
        # we remove the container avoiding graphic update adding items
        # this speeds up the navigation
        self.remove_child(self.itemContainer)
        # creation of a new instance of a itemContainer
        self.itemContainer = Widget(width='100%', height=300)
        self.itemContainer.set_layout_orientation(Widget.LAYOUT_VERTICAL)
        self.itemContainer.style.update({'overflow-y':'scroll', 'overflow-x':'hidden', 'display':'block'})

        for i in l:
            full_path = os.path.join(directory, i)
            is_folder = not os.path.isfile(full_path)
            if (not is_folder) and (not self.allow_file_selection):
                continue
            fi = FileFolderItem(i, is_folder)
            fi.style['display'] = 'block'
            fi.onclick.connect(self.on_folder_item_click)  # navigation purpose
            fi.onselection.connect(self.on_folder_item_selected)  # selection purpose
            self.folderItems.append(fi)
            self.itemContainer.append(fi)
        self.append(self.itemContainer, key='items')  # replace the old widget

    def dir_go_back(self, widget):
        curpath = os.getcwd()  # backup the path
        try:
            os.chdir(self.pathEditor.get_text())
            os.chdir('..')
            self.chdir(os.getcwd())
        except Exception as e:
            self.pathEditor.set_text(self._last_valid_path)
            log.error('error changing directory', exc_info=True)
        os.chdir(curpath)  # restore the path

    def dir_go(self, widget):
        # when the GO button is pressed, it is supposed that the pathEditor is changed
        curpath = os.getcwd()  # backup the path
        try:
            os.chdir(self.pathEditor.get_text())
            self.chdir(os.getcwd())
        except Exception as e:
            log.error('error going to directory', exc_info=True)
            self.pathEditor.set_text(self._last_valid_path)
        os.chdir(curpath)  # restore the path

    def chdir(self, directory):
        curpath = os.getcwd()  # backup the path
        log.debug("FileFolderNavigator - chdir: %s" % directory)
        for c in self.folderItems:
            self.itemContainer.remove_child(c)  # remove the file and folders from the view
        self.folderItems = []
        self.selectionlist = []  # reset selected file list
        os.chdir(directory)
        directory = os.getcwd()
        self.disable_refresh()
        self.populate_folder_items(directory)
        self.enable_refresh()
        self.pathEditor.set_text(directory)
        os.chdir(curpath)  # restore the path

    def on_folder_item_selected(self, folderitem):
        if folderitem.isFolder and (not self.allow_folder_selection):
            folderitem.set_selected(False)
            return

        if not self.multiple_selection:
            self.selectionlist = []
            for c in self.folderItems:
                c.set_selected(False)
            folderitem.set_selected(True)
        log.debug("FileFolderNavigator - on_folder_item_click")
        # when an item is clicked it is added to the file selection list
        f = os.path.join(self.pathEditor.get_text(), folderitem.get_text())
        if f in self.selectionlist:
            self.selectionlist.remove(f)
        else:
            self.selectionlist.append(f)

    def on_folder_item_click(self, folderitem):
        log.debug("FileFolderNavigator - on_folder_item_dblclick")
        # when an item is clicked two time
        f = os.path.join(self.pathEditor.get_text(), folderitem.get_text())
        if not os.path.isfile(f):
            self.chdir(f)

    def get_selected_filefolders(self):
        return self.selectionlist


class FileFolderItem(Widget):
    """FileFolderItem widget for the FileFolderNavigator"""

    @decorate_constructor_parameter_types([str, bool])
    def __init__(self, text, is_folder=False, **kwargs):
        super(FileFolderItem, self).__init__(**kwargs)
        super(FileFolderItem, self).set_layout_orientation(Widget.LAYOUT_HORIZONTAL)
        self.style['margin'] = '3px'
        self.isFolder = is_folder
        self.icon = Widget(_class='FileFolderItemIcon')
        self.icon.set_size(30, 30)
        # the icon click activates the onselection event, that is propagates to registered listener
        if is_folder:
            self.icon.onclick.connect(self.onclick)
        icon_file = '/res/folder.png' if is_folder else '/res/file.png'
        self.icon.style['background-image'] = "url('%s')" % icon_file
        self.label = Label(text)
        self.label.set_size(400, 30)
        self.label.onclick.connect(self.onselection)
        self.append(self.icon, key='icon')
        self.append(self.label, key='text')
        self.selected = False

    def set_selected(self, selected):
        self.selected = selected
        self.label.style['font-weight'] = 'bold' if self.selected else 'normal'

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event
    def onclick(self, widget):
        return super(FileFolderItem, self).onclick()

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event
    def onselection(self, widget):
        self.set_selected(not self.selected)
        return ()

    def set_text(self, t):
        self.children['text'].set_text(t)

    def get_text(self):
        return self.children['text'].get_text()

    @decorate_explicit_alias_for_listener_registration
    def set_on_click_listener(self, callback, *userdata):
        self.onclick.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_selection_listener(self, callback, *userdata):
        self.onselection.connect(callback, *userdata)


class FileSelectionDialog(GenericDialog):
    """file selection dialog, it opens a new webpage allows the OK/CANCEL functionality
    implementing the "confirm_value" and "cancel_dialog" events."""

    @decorate_constructor_parameter_types([str, str, bool, str, bool, bool])
    def __init__(self, title='File dialog', message='Select files and folders',
                 multiple_selection=True, selection_folder='.',
                 allow_file_selection=True, allow_folder_selection=True, **kwargs):
        super(FileSelectionDialog, self).__init__(title, message, **kwargs)

        self.style['width'] = '475px'
        self.fileFolderNavigator = FileFolderNavigator(multiple_selection, selection_folder,
                                                       allow_file_selection,
                                                       allow_folder_selection)
        self.add_field('fileFolderNavigator', self.fileFolderNavigator)
        self.confirm_dialog.connect(self.confirm_value)

    @decorate_set_on_listener("(self, emitter, fileList)")
    @decorate_event
    def confirm_value(self, widget):
        """event called pressing on OK button.
           propagates the string content of the input field
        """
        self.hide()
        params = (self.fileFolderNavigator.get_selection_list(),)
        return params

    @decorate_explicit_alias_for_listener_registration
    def set_on_confirm_value_listener(self, callback, *userdata):
        self.confirm_value.connect(callback, *userdata)


class MenuBar(Widget):

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(MenuBar, self).__init__(*args, **kwargs)
        self.type = 'nav'
        self.set_layout_orientation(Widget.LAYOUT_HORIZONTAL)


class Menu(Widget):
    """Menu widget can contain MenuItem."""

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(Menu, self).__init__(layout_orientation = Widget.LAYOUT_HORIZONTAL, *args, **kwargs)
        self.type = 'ul'


class MenuItem(Widget, _MixinTextualWidget):
    """MenuItem widget can contain other MenuItem."""

    @decorate_constructor_parameter_types([str])
    def __init__(self, text, *args, **kwargs):
        """
        Args:
            text (str):
            kwargs: See Widget.__init__()
        """
        self.sub_container = Menu()
        super(MenuItem, self).__init__(*args, **kwargs)
        super(MenuItem, self).append(self.sub_container, key='subcontainer')
        self.type = 'li'
        self.set_text(text)

    def append(self, value, key=''):
        
        return self.sub_container.append(value, key=key)


class TreeView(Widget):
    """TreeView widget can contain TreeItem."""

    @decorate_constructor_parameter_types([])
    def __init__(self, *args, **kwargs):
        """
        Args:
            kwargs: See Widget.__init__()
        """
        super(TreeView, self).__init__(*args, **kwargs)
        self.type = 'ul'


class TreeItem(Widget, _MixinTextualWidget):
    """TreeItem widget can contain other TreeItem."""

    @decorate_constructor_parameter_types([str])
    def __init__(self, text, *args, **kwargs):
        """
        Args:
            text (str):
            kwargs: See Widget.__init__()
        """
        super(TreeItem, self).__init__(*args, **kwargs)
        self.sub_container = None
        self.type = 'li'
        self.set_text(text)
        self.treeopen = False
        self.attributes['treeopen'] = 'false'
        self.attributes['has-subtree'] = 'false'
        self.attributes[Widget.EVENT_ONCLICK] = \
            "sendCallback('%(emitter_identifier)s','%(event_name)s');" \
            "event.stopPropagation();event.preventDefault();"% \
            {'emitter_identifier': str(self.identifier), 'event_name': Widget.EVENT_ONCLICK}

    def append(self, value, key=''):
        if self.sub_container is None:
            self.attributes['has-subtree'] = 'true'
            self.sub_container = TreeView()
            super(TreeItem, self).append(self.sub_container, key='subcontainer')
        return self.sub_container.append(value, key=key)

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event
    def onclick(self):
        self.treeopen = not self.treeopen
        if self.treeopen:
            self.attributes['treeopen'] = 'true'
        else:
            self.attributes['treeopen'] = 'false'
        return super(TreeItem, self).onclick()


class FileUploader(Widget):
    """
    FileUploader widget:
        allows to upload multiple files to a specified folder.
        implements the onsuccess and onfailed events.
    """

    @decorate_constructor_parameter_types([str, bool])
    def __init__(self, savepath='./', multiple_selection_allowed=False, *args, **kwargs):
        super(FileUploader, self).__init__(*args, **kwargs)
        self._savepath = savepath
        self._multiple_selection_allowed = multiple_selection_allowed
        self.type = 'input'
        self.attributes['type'] = 'file'
        if multiple_selection_allowed:
            self.attributes['multiple'] = 'multiple'
        self.attributes['accept'] = '*.*'
        self.EVENT_ON_SUCCESS = 'onsuccess'
        self.EVENT_ON_FAILED = 'onfailed'
        self.EVENT_ON_DATA = 'ondata'

        self.attributes[self.EVENT_ONCHANGE] = \
            "var files = this.files;" \
            "for(var i=0; i<files.length; i++){" \
            "uploadFile('%(id)s','%(evt_success)s','%(evt_failed)s','%(evt_data)s',files[i]);}" % {
                'id': self.identifier, 'evt_success': self.EVENT_ON_SUCCESS, 'evt_failed': self.EVENT_ON_FAILED,
                'evt_data': self.EVENT_ON_DATA}

    @decorate_set_on_listener("(self, emitter, filename)")
    @decorate_event
    def onsuccess(self, filename):
        return (filename, )

    @decorate_set_on_listener("(self, emitter, filename)")
    @decorate_event
    def onfailed(self, filename):
        return (filename, )

    @decorate_set_on_listener("(self, emitter, filedata, filename)")
    @decorate_event
    def ondata(self, filedata, filename):
        with open(os.path.join(self._savepath, filename), 'wb') as f:
            f.write(filedata)
        return (filedata, filename)

    @decorate_explicit_alias_for_listener_registration
    def set_on_success_listener(self, callback, *userdata):
        self.onsuccess.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_failed_listener(self, callback, *userdata):
        self.onfailed.connect(callback, *userdata)

    @decorate_explicit_alias_for_listener_registration
    def set_on_data_listener(self, callback, *userdata):
        self.ondata.connect(callback, *userdata)


class FileDownloader(Widget, _MixinTextualWidget):
    """FileDownloader widget. Allows to start a file download."""

    @decorate_constructor_parameter_types([str, str, str])
    def __init__(self, text, filename, path_separator='/', *args, **kwargs):
        super(FileDownloader, self).__init__(*args, **kwargs)
        self.type = 'a'
        self.attributes['download'] = os.path.basename(filename)
        self.attributes['href'] = "/%s/download" % self.identifier
        self.set_text(text)
        self._filename = filename
        self._path_separator = path_separator

    def download(self):
        with open(self._filename, 'r+b') as f:
            content = f.read()
        headers = {'Content-type': 'application/octet-stream',
                   'Content-Disposition': 'attachment; filename="%s"' % os.path.basename(self._filename)}
        return [content, headers]


class Link(Widget, _MixinTextualWidget):

    @decorate_constructor_parameter_types([str, str, bool])
    def __init__(self, url, text, open_new_window=True, *args, **kwargs):
        super(Link, self).__init__(*args, **kwargs)
        self.type = 'a'
        self.attributes['href'] = url
        if open_new_window:
            self.attributes['target'] = "_blank"
        self.set_text(text)

    def get_url(self):
        return self.attributes['href']


class VideoPlayer(Widget):
    # some constants for the events

    @decorate_constructor_parameter_types([str, str, bool, bool])
    def __init__(self, video, poster=None, autoplay=False, loop=False, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)
        self.type = 'video'
        self.attributes['src'] = video
        self.attributes['preload'] = 'auto'
        self.attributes['controls'] = None
        self.attributes['poster'] = poster
        self.set_autoplay(autoplay)
        self.set_loop(loop)

    def set_autoplay(self, autoplay):
        if autoplay:
            self.attributes['autoplay'] = 'true'
        else:
            self.attributes.pop('autoplay', None)

    def set_loop(self, loop):
        """Sets the VideoPlayer to restart video when finished.

        Note: If set as True the event onended will not fire."""

        if loop:
            self.attributes['loop'] = 'true'
        else:
            self.attributes.pop('loop', None)

    @decorate_set_on_listener("(self, emitter)")
    @decorate_event_js("sendCallback('%(emitter_identifier)s','%(event_name)s');" \
            "event.stopPropagation();event.preventDefault();")
    def onended(self):
        """Called when the media has been played and reached the end."""
        return ()

    @decorate_explicit_alias_for_listener_registration
    def set_on_ended_listener(self, callback, *userdata):
        self.onended.connect(callback, *userdata)


class Svg(Widget):
    """svg widget - is a container for graphic widgets such as SvgCircle, SvgLine and so on."""

    @decorate_constructor_parameter_types([int, int])
    def __init__(self, width, height, *args, **kwargs):
        """
        Args:
            width (int): the viewport width in pixel
            height (int): the viewport height in pixel
            kwargs: See Widget.__init__()
        """
        super(Svg, self).__init__(*args, **kwargs)
        self.set_size(width, height)
        self.attributes['width'] = width
        self.attributes['height'] = height
        self.type = 'svg'

    def set_viewbox(self, x, y, w, h):
        """Sets the origin and size of the viewbox, describing a virtual view area.

        Args:
            x (int): x coordinate of the viewbox origin
            y (int): y coordinate of the viewbox origin
            w (int): width of the viewbox
            h (int): height of the viewbox
        """
        self.attributes['viewBox'] = "%s %s %s %s" % (x, y, w, h)
        self.attributes['preserveAspectRatio'] = 'none'


class SvgShape(Widget):
    """svg shape generic widget. Consists of a position, a fill color and a stroke."""

    @decorate_constructor_parameter_types([int, int])
    def __init__(self, x, y, *args, **kwargs):
        """
        Args:
            x (int): the x coordinate
            y (int): the y coordinate
            kwargs: See Widget.__init__()
        """
        super(SvgShape, self).__init__(*args, **kwargs)
        self.set_position(x, y)

    def set_position(self, x, y):
        """Sets the shape position.

        Args:
            x (int): the x coordinate
            y (int): the y coordinate
        """
        self.attributes['x'] = str(x)
        self.attributes['y'] = str(y)

    def set_stroke(self, width=1, color='black'):
        """Sets the stroke properties.

        Args:
            width (int): stroke width
            color (str): stroke color
        """
        self.attributes['stroke'] = color
        self.attributes['stroke-width'] = str(width)

    def set_fill(self, color='black'):
        """Sets the fill color.

        Args:
            color (str): stroke color
        """
        self.attributes['fill'] = color


class SvgGroup(SvgShape):
    """svg group widget."""

    @decorate_constructor_parameter_types([int, int])
    def __init__(self, x, y, *args, **kwargs):
        super(SvgGroup, self).__init__(x, y, *args, **kwargs)
        self.type = 'g' 


class SvgRectangle(SvgShape):
    """svg rectangle - a rectangle represented filled and with a stroke."""

    @decorate_constructor_parameter_types([int, int, int, int])
    def __init__(self, x, y, w, h, *args, **kwargs):
        """
        Args:
            x (int): the x coordinate of the top left corner of the rectangle
            y (int): the y coordinate of the top left corner of the rectangle
            w (int): width of the rectangle
            h (int): height of the rectangle
            kwargs: See Widget.__init__()
        """
        super(SvgRectangle, self).__init__(x, y, *args, **kwargs)
        self.set_size(w, h)
        self.type = 'rect'

    def set_size(self, w, h):
        """ Sets the rectangle size.

        Args:
            w (int): width of the rectangle
            h (int): height of the rectangle
        """
        self.attributes['width'] = str(w)
        self.attributes['height'] = str(h)


class SvgCircle(SvgShape):
    """svg circle - a circle represented filled and with a stroke."""

    @decorate_constructor_parameter_types([int, int, int])
    def __init__(self, x, y, radius, *args, **kwargs):
        """
        Args:
            x (int): the x center point of the circle
            y (int): the y center point of the circle
            radius (int): the circle radius
            kwargs: See Widget.__init__()
        """
        super(SvgCircle, self).__init__(x, y, *args, **kwargs)
        self.set_radius(radius)
        self.type = 'circle'

    def set_radius(self, radius):
        """Sets the circle radius.

        Args:
            radius (int): the circle radius
        """
        self.attributes['r'] = radius

    def set_position(self, x, y):
        """Sets the circle position.

        Args:
            x (int): the x coordinate
            y (int): the y coordinate
        """
        self.attributes['cx'] = str(x)
        self.attributes['cy'] = str(y)


class SvgLine(Widget):

    @decorate_constructor_parameter_types([int, int, int, int])
    def __init__(self, x1, y1, x2, y2, *args, **kwargs):
        super(SvgLine, self).__init__(*args, **kwargs)
        self.set_coords(x1, y1, x2, y2)
        self.type = 'line'

    def set_coords(self, x1, y1, x2, y2):
        self.set_p1(x1, y1)
        self.set_p2(x2, y2)

    def set_p1(self, x1, y1):
        self.attributes['x1'] = x1
        self.attributes['y1'] = y1

    def set_p2(self, x2, y2):
        self.attributes['x2'] = x2
        self.attributes['y2'] = y2

    def set_stroke(self, width=1, color='black'):
        self.style['stroke'] = color
        self.style['stroke-width'] = str(width)


class SvgPolyline(Widget):

    @decorate_constructor_parameter_types([int])
    def __init__(self, _maxlen=None, *args, **kwargs):
        super(SvgPolyline, self).__init__(*args, **kwargs)
        self.style['fill'] = 'none'
        self.type = 'polyline'
        self.coordsX = collections.deque(maxlen=_maxlen)
        self.coordsY = collections.deque(maxlen=_maxlen)
        self.maxlen = _maxlen  # no limit
        self.attributes['points'] = ''
        self.attributes['vector-effect'] = 'non-scaling-stroke'

    def add_coord(self, x, y):
        if len(self.coordsX) == self.maxlen:
            spacepos = self.attributes['points'].find(' ')
            if spacepos > 0:
                self.attributes['points'] = self.attributes['points'][spacepos + 1:]
        self.coordsX.append(x)
        self.coordsY.append(y)
        self.attributes['points'] += "%s,%s " % (x, y)

    def set_stroke(self, width=1, color='black'):
        self.style['stroke'] = color
        self.style['stroke-width'] = str(width)


class SvgText(SvgShape, _MixinTextualWidget):

    @decorate_constructor_parameter_types([int, int, str])
    def __init__(self, x, y, text, *args, **kwargs):
        super(SvgText, self).__init__(x, y, *args, **kwargs)
        self.type = 'text'
        self.set_fill()
        self.set_text(text)


class SvgPath(Widget):

    @decorate_constructor_parameter_types([str])
    def __init__(self, path_value, *args, **kwargs):
        super(SvgPath, self).__init__(*args, **kwargs)
        self.type = 'path'
        self.set_fill()
        self.attributes['d'] = path_value

    def add_position(self, x, y):
        self.attributes['d'] = self.attributes['d'] + "M %s %s"%(x,y)

    def add_arc(self, x, y, rx, ry, x_axis_rotation, large_arc_flag, sweep_flag):
        #A rx ry x-axis-rotation large-arc-flag sweep-flag x y
        self.attributes['d'] = self.attributes['d'] + "A %(rx)s %(ry)s, %(x-axis-rotation)s, %(large-arc-flag)s, %(sweep-flag)s, %(x)s %(y)s"%{'x':x, 
            'y':y, 'rx':rx, 'ry':ry, 'x-axis-rotation':x_axis_rotation, 'large-arc-flag':large_arc_flag, 'sweep-flag':sweep_flag}

    def set_stroke(self, width=1, color='black'):
        """Sets the stroke properties.

        Args:
            width (int): stroke width
            color (str): stroke color
        """
        self.attributes['stroke'] = color
        self.attributes['stroke-width'] = str(width)

    def set_fill(self, color='black'):
        """Sets the fill color.

        Args:
            color (str): stroke color
        """
        self.attributes['fill'] = color