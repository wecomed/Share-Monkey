"""Locate Tcl/Tk when running a relocatable Python build in a virtualenv."""
import os
from pathlib import Path
import sys


def configure_tk():
    for variable, folder, marker in (('TCL_LIBRARY', 'tcl8.6', 'init.tcl'),
                                     ('TK_LIBRARY', 'tk8.6', 'tk.tcl')):
        library = Path(sys.base_prefix) / 'lib' / folder
        if (library / marker).is_file():
            os.environ.setdefault(variable, str(library))
