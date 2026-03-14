import yaml
import os

def type_mapping(val):
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    if isinstance(val, list):
        return "list"
    return "Any"

def generate_classes(d, class_name="ConfigRoot"):
    lines = []
    lines.append(f"class {class_name}:")
    
    nested_classes = []
    
    if not d:
        lines.append("    pass")
    
    for k, v in d.items():
        if isinstance(v, dict):
            nested_class_name = f"{class_name}_{k.capitalize()}"
            nested_classes.extend(generate_classes(v, nested_class_name))
            lines.append(f"    {k}: '{nested_class_name}'")
        else:
            lines.append(f"    {k}: {type_mapping(v)}")
            
    lines.append("")
    return nested_classes + lines

def generate_pyi(config_path="config.yaml", output_path="core/config.pyi"):
    if not os.path.exists(config_path):
        print(f"Warning: {config_path} not found.")
        return
        
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
        
    lines = [
        "# Auto-generated config stub",
        "from typing import Any, Dict",
        "",
        "class JsonDict(dict):",
        "    def __init__(self, *args, **kwargs) -> None: ...",
        "    def __getattr__(self, attr: Any) -> Any: ...",
        "    def __setattr__(self, attr: Any, value: Any) -> None: ...",
        ""
    ]
    
    # Generate nested config structures
    for k, v in config_data.items():
        if isinstance(v, dict):
            lines.extend(generate_classes(v, f"Config_{k.capitalize()}"))
            
    # Generate Config class
    lines.append("class Config:")
    lines.append("    config_path: str")
    lines.append("    def __init__(self, config_path: str = ...) -> None: ...")
    lines.append("    def load(self) -> None: ...")
    lines.append("    def get(self, key: str, default: Any = ...) -> Any: ...")
    lines.append("    def __getitem__(self, key: str) -> Any: ...")
    lines.append("    def __getattr__(self, name: str) -> Any: ...")
    
    # Add root properties
    for k, v in config_data.items():
        if isinstance(v, dict):
            lines.append(f"    {k}: 'Config_{k.capitalize()}'")
        else:
            lines.append(f"    {k}: {type_mapping(v)}")
            
    lines.append("")
    lines.append("config: Config")
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        
    print(f"Successfully generated {output_path} based on {config_path}")

if __name__ == "__main__":
    # Ensure working directory is the project root
    if not os.path.exists("config.yaml") and os.path.exists("../config.yaml"):
        os.chdir("..")
    generate_pyi()
