"""Use case: list available task methods with argument signatures."""

import inspect

from copixiv.tasks.registry import list_tasks


class GetMethodsUseCase:
    """Enumerate registered background tasks with their parameter signatures."""

    def execute(self) -> list[dict]:
        """Return a list of task-method descriptors."""
        methods = []
        for name, func in list_tasks().items():
            sig = inspect.signature(func)
            arguments = []
            for param_name, param in sig.parameters.items():
                if param.kind not in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    continue
                param_type = "str"
                if param.annotation != inspect.Parameter.empty:
                    if param.annotation is int:
                        param_type = "int"
                    elif param.annotation is bool:
                        param_type = "bool"
                    elif param.annotation is float:
                        param_type = "float"

                default_val = None
                required = True
                if param.default != inspect.Parameter.empty:
                    default_val = param.default
                    required = False

                arguments.append({
                    "name": param_name,
                    "type": param_type,
                    "default": default_val,
                    "required": required,
                })
            methods.append({
                "name": name,
                "description": func.__doc__,
                "arguments": arguments,
            })
        return methods
