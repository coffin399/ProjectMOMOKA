"""
Dynamically imports and initializes LLM plugins.

This module scans the 'plugins' directory for Python files (excluding __init__.py),
dynamically imports them, and provides a function to initialize all found plugin
classes.

Attributes:
    __all__: A list of discovered plugin class names for 'from . import *'.
"""

import importlib
import os
from typing import List, Type, Any

# --- Globals ---
# A list to hold the discovered plugin classes.
_plugin_classes: List[Type[Any]] = []
__all__: List[str] = []


def _discover_plugins():
    """
    Discovers plugins in the current directory, imports them, and populates
    _plugin_classes and __all__.
    """
    if _plugin_classes:  # Avoid re-discovering if already done
        return

    current_dir = os.path.dirname(__file__)
    
    for filename in os.listdir(current_dir):
        if filename.endswith('.py') and not filename.startswith('__'):
            module_name = filename[:-3]
            try:
                # Import the module relative to the 'plugins' package
                module = importlib.import_module(f'.{module_name}', package=__name__)

                # Find classes within the module that are likely plugins.
                # This example assumes plugin classes are named like 'MyPlugin'
                # and are not imported from other modules.
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and attr.__module__ == module.__name__:
                        _plugin_classes.append(attr)
                        __all__.append(attr_name)
                        # print(f"Discovered plugin: {attr_name}") # for debugging

            except ImportError as e:
                # Handle potential import errors gracefully
                print(f"Error importing plugin {module_name}: {e}")

# --- Initialization ---
# Discover plugins when this package is imported.
_discover_plugins()

def initialize_plugins(bot: Any) -> List[Any]:
    """
    Initializes all discovered plugin classes.

    Args:
        bot: The main bot instance, passed to each plugin's constructor.

    Returns:
        A list of initialized plugin instances.
    """
    initialized_plugins = []
    for plugin_class in _plugin_classes:
        try:
            initialized_plugins.append(plugin_class(bot))
        except Exception as e:
            print(f"Error initializing plugin {plugin_class.__name__}: {e}")
    return initialized_plugins