import logging
import pdb
import sys
import threading
import traceback
import warnings
from typing import Any, Dict, MutableMapping
from unittest import mock

import ipykernel.kernelbase
import IPython.display
import ipywidgets
import ipywidgets.widgets.widget_output
from IPython.core.interactiveshell import InteractiveShell

from . import app, kernel_context, reload, settings
from .utils import pdb_guard

logger = logging.getLogger("solara.server.patch")
try:
    from reacton.patch_display import patch as patch_display
except:  # noqa
    patch_display = None  # type: ignore

if patch_display is not None and sys.platform != "emscripten":
    patch_display()
ipywidget_version_major = int(ipywidgets.__version__.split(".")[0])


class FakeIPython:
    def __init__(self, context: kernel_context.VirtualKernelContext):
        self.context = context
        self.kernel = context.kernel
        self.display_pub = self.kernel.shell.display_pub
        # needed for the pyplot interface of matplotlib
        # (although we don't really support it)
        self.events = mock.MagicMock()

    def enable_gui(self, gui):
        logger.error("ignoring call to enable_gui(%s)", gui)

    def register_post_execute(self, callback):
        # mpl requires this
        pass

    def set_parent(self, *args):
        pass

    def showtraceback(self):
        if settings.main.use_pdb:
            logger.exception("Exception, will be handled by debugger")
            pdb.post_mortem()
        etype, value, tb = sys.exc_info()
        traceback_string = "".join(traceback.format_exception(etype, value, tb))
        logger.error("Uncaught exception: %s", traceback_string)
        msg = {
            "type": "exception",
            "traceback": traceback_string,
        }

        for socket in self.context.control_sockets:
            try:
                socket.send_json(msg)
            except:  # noqa
                # TODO: should we remove it from the list?
                pass

    def magic(self, *args):
        # proplot requires this
        pass

    def set_custom_exc(self, exc_tuple, handler):
        # make dask work
        pass


Kernel_instance_original = ipykernel.kernelbase.Kernel.instance.__func__  # type: ignore


def kernel_instance_dispatch(cls, *args, **kwargs):
    if kernel_context.has_current_context():
        context = kernel_context.get_current_context()
        return context.kernel
    else:
        return Kernel_instance_original(cls, *args, **kwargs)


Kernel_initialized_initial = ipykernel.kernelbase.Kernel.initialized.__func__  # type: ignore


def kernel_initialized_dispatch(cls):
    if app is None:  # python is shutting down, and the comm dtor wants to send a close message
        return False
    if kernel_context.has_current_context():
        return True
    else:
        return Kernel_initialized_initial(cls)


InteractiveShell_instance_initial = InteractiveShell.instance.__func__  # type: ignore


def interactive_shell_instance_dispatch(cls, *args, **kwargs):
    if kernel_context.has_current_context():
        context = kernel_context.get_current_context()
        return context.kernel.shell
    else:
        return InteractiveShell_instance_initial(cls, *args, **kwargs)


def display_solara(
    *objs,
    include=None,
    exclude=None,
    metadata=None,
    transient=None,
    display_id=None,
    raw=False,
    clear=False,
    **kwargs,
):
    print(*objs)  # noqa


# from IPython.core.interactiveshell import InteractiveShell

# if transient is None:
#     transient = {}
# if metadata is None:
#     metadata = {}
# from IPython.core.display_functions import _new_id

# if display_id:
#     if display_id is True:
#         display_id = _new_id()
#     transient["display_id"] = display_id
# if kwargs.get("update") and "display_id" not in transient:
#     raise TypeError("display_id required for update_display")
# if transient:
#     kwargs["transient"] = transient

# if not objs and display_id:
#     # if given no objects, but still a request for a display_id,
#     # we assume the user wants to insert an empty output that
#     # can be updated later
#     objs = [{}]
#     raw = True

# if not raw:
#     format = InteractiveShell.instance().display_formatter.format

# if clear:
#     clear_output(wait=True)

# for obj in objs:
#     if raw:
#         publish_display_data(data=obj, metadata=metadata, **kwargs)
#     else:
#         format_dict, md_dict = format(obj, include=include, exclude=exclude)
#         if not format_dict:
#             # nothing to display (e.g. _ipython_display_ took over)
#             continue
#         if metadata:
#             # kwarg-specified metadata gets precedence
#             _merge(md_dict, metadata)
#         publish_display_data(data=format_dict, metadata=md_dict, **kwargs)
# if display_id:
#     return DisplayHandle(display_id)


def get_ipython():
    if kernel_context.has_current_context():
        context = kernel_context.get_current_context()
        our_fake_ipython = FakeIPython(context)
        return our_fake_ipython
    else:
        return None


class context_dict(MutableMapping):
    def _get_context_dict(self) -> dict:
        raise NotImplementedError

    def __delitem__(self, key) -> None:
        self._get_context_dict().__delitem__(key)

    def __getitem__(self, key):
        return self._get_context_dict().__getitem__(key)

    def __iter__(self):
        return self._get_context_dict().__iter__()

    def __len__(self):
        return self._get_context_dict().__len__()

    def __setitem__(self, key, value):
        self._get_context_dict().__setitem__(key, value)


class context_dict_widgets(context_dict):
    def _get_context_dict(self) -> dict:
        if kernel_context.has_current_context():
            context = kernel_context.get_current_context()
            return context.widgets
        else:
            return global_widgets_dict


class context_dict_templates(context_dict):
    def _get_context_dict(self) -> dict:
        if kernel_context.has_current_context():
            context = kernel_context.get_current_context()
            return context.templates
        else:
            return global_templates_dict


class context_dict_user(context_dict):
    def __init__(self, name, default_dict):
        self.name = name
        self.default_dict = default_dict

    def _get_context_dict(self) -> dict:
        if kernel_context.has_current_context():
            context = kernel_context.get_current_context()
            if self.name not in context.user_dicts:
                context.user_dicts[self.name] = {}
            return context.user_dicts[self.name]
        else:
            return self.default_dict


def auto_watch_get_template(get_template):
    """Wraps get_template and adds a file listener for automatic .vue file reloading"""

    def wrapper(abs_path):
        template = get_template(abs_path)
        reload.reloader.watcher.add_file(abs_path)
        return template

    return wrapper


Thread__init__ = threading.Thread.__init__
Thread__run = threading.Thread.run


def WidgetContextAwareThread__init__(self, *args, **kwargs):
    Thread__init__(self, *args, **kwargs)
    self.current_context = None
    try:
        self.current_context = kernel_context.get_current_context()
    except RuntimeError:
        logger.debug(f"No context for thread {self}")


def Thread_debug_run(self):
    if self.current_context:
        kernel_context.set_context_for_thread(self.current_context, self)
    with pdb_guard():
        Thread__run(self)


_patched = False
global_widgets_dict = {}
global_templates_dict: Dict[Any, Any] = {}


def Output_enter(self):
    self._flush()

    def hook(msg):
        if msg["msg_type"] == "display_data":
            self.outputs += ({"output_type": "display_data", "data": msg["content"]["data"], "metadata": msg["content"]["metadata"]},)
            return None
        if msg["msg_type"] == "clear_output":
            self.outputs = ()
            return None
        return msg

    ip = get_ipython()
    if ip:
        ip.display_pub.register_hook(hook)


def Output_exit(self, exc_type, exc_value, traceback):
    ip = get_ipython()
    if ip:
        ip.display_pub._hooks.pop()


def patch():
    global _patched
    global global_widgets_dict
    if _patched:
        warnings.warn("patch() called twice")
        return
    _patched = True
    __builtins__["display"] = IPython.display.display

    # the ipyvue.Template module cannot be accessed like ipyvue.Template
    # because the import in ipvue overrides it
    template_mod = sys.modules["ipyvue.Template"]
    template_mod.template_registry = context_dict_templates()  # type: ignore
    template_mod.get_template = auto_watch_get_template(template_mod.get_template)  # type: ignore

    # this module also imports get_template
    template_mod_vue = sys.modules["ipyvue.VueTemplateWidget"]
    template_mod_vue.get_template = template_mod.get_template  # type: ignore

    component_mod_vue = sys.modules["ipyvue.VueComponentRegistry"]
    component_mod_vue.vue_component_registry = context_dict_user("vue_component_registry", component_mod_vue.vue_component_registry)  # type: ignore
    component_mod_vue.vue_component_files = context_dict_user("vue_component_files", component_mod_vue.vue_component_files)  # type: ignore

    if ipywidget_version_major < 8:
        global_widgets_dict = ipywidgets.widget.Widget.widgets
        ipywidgets.widget.Widget.widgets = context_dict_widgets()  # type: ignore
    else:
        if hasattr(ipywidgets.widgets.widget, "_instances"):  # since 8.0.3
            global_widgets_dict = ipywidgets.widgets.widget._instances
            ipywidgets.widgets.widget._instances = context_dict_widgets()  # type: ignore
        elif hasattr(ipywidgets.widget.Widget, "_instances"):
            global_widgets_dict = ipywidgets.widget.Widget._instances
            ipywidgets.widget.Widget._instances = context_dict_widgets()  # type: ignore
        else:
            raise RuntimeError("Could not find _instances on ipywidgets version %r" % ipywidgets.__version__)
    threading.Thread.__init__ = WidgetContextAwareThread__init__  # type: ignore
    threading.Thread.run = Thread_debug_run  # type: ignore
    # on CI we get a mypy error:
    # solara/server/patch.py:210: error: Cannot assign to a method
    #  solara/server/patch.py:210: error: Incompatible types in assignment (expression has type "classmethod[Any]",\
    #                                     variable has type "Callable[[VarArg(Any), KwArg(Any)], Any]")
    # not sure why we cannot reproduce that locally
    ipykernel.kernelbase.Kernel.instance = classmethod(kernel_instance_dispatch)  # type: ignore
    InteractiveShell.instance = classmethod(interactive_shell_instance_dispatch)  # type: ignore
    # on CI we get a mypy error:
    # solara/server/patch.py:211: error: Cannot assign to a method
    # solara/server/patch.py:211: error: Incompatible types in assignment (expression has type "classmethod[Any]", variable has type "Callable[[], Any]")
    # not sure why we cannot reproduce that locally
    ipykernel.kernelbase.Kernel.initialized = classmethod(kernel_initialized_dispatch)  # type: ignore
    ipywidgets.widgets.widget.get_ipython = get_ipython
    # TODO: find a way to actually monkeypatch get_ipython
    IPython.get_ipython = get_ipython

    ipywidgets.widgets.widget_output.Output.__enter__ = Output_enter
    ipywidgets.widgets.widget_output.Output.__exit__ = Output_exit

    original_close = ipywidgets.widget.Widget.close
    closed_ids = set()
    closed_stack: Dict[int, str] = {}

    def model_id_debug(self: ipywidgets.widgets.widget.Widget):
        from ipyvue.ForceLoad import force_load_instance

        import solara.comm

        if self.comm is None and id(self) in closed_ids and id(self) in closed_stack:
            raise RuntimeError(f"Widget has been closed, the stacktrace when the widget was closed is:\n{closed_stack[id(self)]}")

        if self.comm is None or isinstance(self.comm, solara.comm.DummyComm) and force_load_instance.comm is not self.comm:
            stack = solara.comm.orphan_comm_stacks.get(self.comm)
            if stack:
                raise RuntimeError(
                    "Widget has no comm, you are probably using a widget that was created at app startup, the stacktrace when the widget was created is:\n"
                    + stack
                )
            else:
                raise RuntimeError("Widget has no comm, you are probably using a widget that was closed. The widget is:\n" + repr(self))

        return self.comm.comm_id

    ipywidgets.widget.Widget.model_id = property(model_id_debug)

    def close_widget_debug(self: ipywidgets.widgets.widget.Widget):
        # only in development mode, since this leaks memory
        # can be called during shutdown/gc, so we need to check if the module is still there
        if settings and settings.main.mode == "development":
            stacktrace = "".join(traceback.format_stack())
            closed_stack[id(self)] = stacktrace
            closed_ids.add(id(self))
        original_close(self)

    ipywidgets.widget.Widget.close = close_widget_debug
