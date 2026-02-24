import inspect
import sys
import os
from typing import Any, Callable, Coroutine, List, Optional, TypeVar, ContextManager, AsyncIterator
# try to import pixivpy3
try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    print("Error: pixivpy3 not found. Please install it first.")
    sys.exit(1)

def get_type_hint(param):
    if param.annotation != inspect.Parameter.empty:
        # Handle simple type hints
        if hasattr(param.annotation, "__name__"):
            return f": {param.annotation.__name__}"
        # Handle typing types like List[int] which don't have __name__ directly in some python versions
        s = str(param.annotation).replace('typing.', '')
        if s.startswith("<class '"):
            s = s[8:-2]
        return f": {s}"
    return ""

def format_default_value(val):
    if isinstance(val, str):
        return f"'{val}'"
    if val is None:
        return "None"
    return str(val)

def get_params_str(sig):
    params = []
    for name, param in sig.parameters.items():
        if name == "self":
            params.append("self")
            continue
            
        type_hint = get_type_hint(param)
        default_val = ""
        
        if param.default != inspect.Parameter.empty:
            default_val = f" = {format_default_value(param.default)}"
            
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            part = f"*{name}{type_hint}"
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            part = f"**{name}{type_hint}"
        else:
            part = f"{name}{type_hint}{default_val}"
            
        params.append(part)
    return ", ".join(params)

def generate_stub():
    methods_code = []
    
    # Get all members from AppPixivAPI class directly to avoid instance bound methods if any
    # Using dir() and getattr on class is better than getmembers for control
    
    # But inspecting the class object directly works well with getmembers
    members = inspect.getmembers(AppPixivAPI, predicate=lambda x: inspect.isfunction(x) or inspect.ismethod(x))
    
    # Filter out private methods and non-target methods
    target_prefixes = ('user_', 'novel_', 'illust_', 'search_', 'webview_')
    public_methods = [
        (name, func) for name, func in members 
        if not name.startswith("_") and name.startswith(target_prefixes)
    ]
    
    # Add methods from AppPixivAPI
    # We iterate and redefine them as async
    
    for name, func in public_methods:
        try:
            sig = inspect.signature(func)
        except ValueError:
            continue
            
        # Manually inject fetch_all and fetch_til
        params = []
        has_kwargs = False
        
        for p_name, param in sig.parameters.items():
            if p_name == "self":
                params.append("self")
                continue
                
            type_hint = get_type_hint(param)
            default_val = ""
            
            if param.default != inspect.Parameter.empty:
                default_val = f" = {format_default_value(param.default)}"
                
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                part = f"*{p_name}{type_hint}"
                params.append(part)
            elif param.kind == inspect.Parameter.VAR_KEYWORD:
                # Inject our custom params before **kwargs
                params.append("fetch_all: bool = False")
                params.append("fetch_til: bool = False")
                
                part = f"**{p_name}{type_hint}"
                params.append(part)
                has_kwargs = True
            else:
                part = f"{p_name}{type_hint}{default_val}"
                params.append(part)
        
        if not has_kwargs:
            params.append("fetch_all: bool = False")
            params.append("fetch_til: bool = False")
            
        params_str = ", ".join(params)
        
        # Docstring handling
        doc = inspect.getdoc(func)
        if doc:
            # Indent docstring properly
            doc_lines = doc.split('\n')
            indented_doc = '\n        '.join(doc_lines)
            doc_str = f'        """\n        {indented_doc}\n        """'
        else:
            doc_str = '        ...'

        # Get return annotation
        return_annotation = "Any"
        if sig.return_annotation != inspect.Parameter.empty:
             if hasattr(sig.return_annotation, "__name__"):
                return_annotation = sig.return_annotation.__name__
             else:
                s = str(sig.return_annotation).replace('typing.', '')
                if s.startswith("<class '"):
                    s = s[8:-2]
                return_annotation = s
        
        # Define as async method
        method_code = f"    async def {name}({params_str}) -> {return_annotation}:\n{doc_str}"
        methods_code.append(method_code)

    # Prepare file content
    content = [
        "import asyncio",
        "from typing import Any, Callable, Coroutine, List, Optional, TypeVar, ContextManager, AsyncIterator, AsyncContextManager, Dict, Union",
        "from contextlib import asynccontextmanager",
        # Import necessary types and models from pixivpy3
        "from pixivpy3 import AppPixivAPI, models",
        "from pixivpy3.utils import ParamDict, ParsedJson, Response",
        "from pixivpy3.aapi import _BOOL, _RESTRICT, _CONTENT_TYPE, _FILTER, _MODE, _SEARCH_TARGET, _SORT, _DURATION, _TYPE, DateOrStr",
        "",
        "T = TypeVar('T')",
        "",
        "class PixivClient:",
        "    def __init__(self, config_override: dict = None) -> None: ...",
        "",
        "    @asynccontextmanager",
        "    def force_account(self, index: int) -> AsyncIterator['PixivClient']: ...",
        "",
        "    @asynccontextmanager",
        "    def account_rule(self, need_premium: bool = False, allow_special: bool = False) -> AsyncIterator['PixivClient']: ...",
        "",
        "    def pick(self, index: int) -> 'PixivClient': ...",
        "",
        "    # Proxy methods from AppPixivAPI",
    ]
    
    content.extend(methods_code)
    
    # Ensure directory exists
    os.makedirs("core", exist_ok=True)
    
    output_path = os.path.join("core", "pixiv_client.pyi")
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(content))
        print(f"Successfully generated {output_path} with {len(methods_code)} proxied methods.")
    except Exception as e:
        print(f"Error writing file: {e}")

if __name__ == "__main__":
    generate_stub()
