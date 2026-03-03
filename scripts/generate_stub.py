import inspect
import sys
import os
import re
from typing import Any, Callable, Coroutine, List, Optional, TypeVar, AsyncContextManager

# Ensure project root is in sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from pixivpy3 import AppPixivAPI
    # Import patch to ensure novel_ranking is present in AppPixivAPI
    import core.pixiv_patch 
except ImportError as e:
    print(f"Error importing modules: {e}")
    sys.exit(1)

def get_type_name(annotation) -> str:
    """Extract string representation of a type annotation."""
    if annotation == inspect.Parameter.empty:
        return "Any"
    if isinstance(annotation, str):
        return annotation
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    
    # Handle typing objects (e.g. List[int], Union[...])
    s = str(annotation).replace('typing.', '')
    # Remove <class '...'> wrapper if present
    if s.startswith("<class '"):
        s = s[8:-2]
    return s

def format_default_value(val) -> str:
    """Format default value for python code."""
    if isinstance(val, str):
        return f"'{val}'"
    if val is None:
        return "None"
    return str(val)

def generate_method_stub(name: str, func: Callable) -> str:
    """Generate async stub for a single method."""
    try:
        sig = inspect.signature(func)
    except ValueError:
        return ""

    params = []
    has_kwargs = False
    
    for p_name, param in sig.parameters.items():
        if p_name == "self":
            params.append("self")
            continue
            
        type_hint = f": {get_type_name(param.annotation)}" if param.annotation != inspect.Parameter.empty else ""
        default_val = f" = {format_default_value(param.default)}" if param.default != inspect.Parameter.empty else ""
        
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            params.append(f"*{p_name}{type_hint}")
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            # Inject fetch_all/fetch_til/fetch_minlike before **kwargs
            params.append("fetch_all: bool = False")
            params.append("fetch_til: datetime = None") 
            params.append("fetch_minlike: int = 0") 
            params.append(f"**{p_name}{type_hint}")
            has_kwargs = True
        else:
            params.append(f"{p_name}{type_hint}{default_val}")
    
    if not has_kwargs:
        params.append("fetch_all: bool = False")
        params.append("fetch_til: datetime = None") 
        params.append("fetch_minlike: int = 0") 
        
    params_str = ", ".join(params)
    
    return_type = get_type_name(sig.return_annotation)
    
    # Simple docstring extraction
    doc = inspect.getdoc(func)
    if doc:
        doc_lines = doc.split('\n')
        indented_doc = '\n        '.join(doc_lines)
        doc_str = f'        """\n        {indented_doc}\n        """'
    else:
        doc_str = '        ...'

    return f"    async def {name}({params_str}) -> {return_type}:\n{doc_str}"

def generate_stub():
    # Target methods starting with these prefixes
    target_prefixes = ('user_', 'novel_', 'illust_', 'search_', 'webview_')
    
    methods_code = []
    
    # Inspect AppPixivAPI members
    # We use dir() to include monkey-patched methods which might not show up with inspect.getmembers depending on how they were added
    for name in dir(AppPixivAPI):
        if name.startswith("_") or not name.startswith(target_prefixes):
            continue
            
        func = getattr(AppPixivAPI, name)
        if not (inspect.isfunction(func) or inspect.ismethod(func)):
            continue
            
        stub = generate_method_stub(name, func)
        if stub:
            methods_code.append(stub)

    # File Header
    content = [
        "import asyncio",
        "from typing import Any, Callable, Coroutine, List, Optional, TypeVar, AsyncIterator, Dict, Union, Literal",
        "from contextlib import asynccontextmanager",
        "from datetime import date, datetime",
        "",
        "from pixivpy3 import AppPixivAPI, models",
        "from pixivpy3.utils import ParamDict, ParsedJson, Response",
        # Attempt to import types if available, otherwise define fallbacks or use Any
        "try:",
        "    from pixivpy3.aapi import _BOOL, _RESTRICT, _CONTENT_TYPE, _FILTER, _MODE, _SEARCH_TARGET, _SORT, _DURATION, _TYPE, DateOrStr",
        "except ImportError:",
        "    _BOOL = Any",
        "    _RESTRICT = Any",
        "    _CONTENT_TYPE = Any",
        "    _FILTER = Any",
        "    _MODE = Any",
        "    _SEARCH_TARGET = Any",
        "    _SORT = Any",
        "    _DURATION = Any",
        "    _TYPE = Any",
        "    DateOrStr = Union[date, str]",
        "",
        "T = TypeVar('T')",
        "",
        "class PixivClient:",
        "    def __init__(self, config_override: dict = None) -> None: ...",
        "",
        "    @asynccontextmanager",
        "    def account_rule(self, need_premium: bool = False, allow_special: bool = False, force_account: str = None) -> AsyncIterator['PixivClient']: ...",
        "",
        "    # Proxy methods from AppPixivAPI",
    ]
    
    content.extend(methods_code)
    
    output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "pixiv_client.pyi")
    
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(content))
        print(f"Successfully generated {output_path} with {len(methods_code)} proxied methods.")
    except Exception as e:
        print(f"Error writing file: {e}")

if __name__ == "__main__":
    generate_stub()
