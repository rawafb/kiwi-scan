# SPDX-FileCopyrightText: 2026 Helmholtz-Zentrum Berlin für Materialien und Energie GmbH
# SPDX-License-Identifier: MIT

"""Print the full ScanConfig YAML structure filled with default values.

The output is generated from the dataclass definitions in
``kiwi_scan.datamodels``, so it follows field changes automatically.

Conventions in the generated YAML:

* Fields without a default get a type-appropriate placeholder and a
  ``# required`` comment.
* Optional nested blocks (default ``null``) and collections of nested
  configs (default ``[]``/``{}``) are expanded with one example entry so
  their structure is visible; the comment states the real default.
"""

import argparse
import dataclasses
import sys
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import yaml

from kiwi_scan import datamodels

INDENT = "  "

# Dataclasses whose YAML form is a free-form mapping rather than their fields.
# MonitorConfig.from_dict takes the monitor-specific parameters directly
# under ``monitor:``.
FREE_FORM = {datamodels.MonitorConfig}

# Key used for example entries of Dict[str, <dataclass>] fields.
EXAMPLE_KEY = "example"


def _unwrap_optional(tp: Any) -> Tuple[Any, bool]:
    """Return (inner type, is_optional) for Optional[X]."""
    if get_origin(tp) is Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def _is_dataclass_type(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _placeholder(tp: Any) -> Any:
    """Type-appropriate value for a field that has no default."""
    tp, optional = _unwrap_optional(tp)
    if optional:
        return None
    origin = get_origin(tp) or tp
    for kind, value in ((bool, False), (int, 0), (float, 0.0), (str, "")):
        if origin is kind:
            return value
    if origin in (list, List):
        return []
    if origin in (dict, Dict):
        return {}
    return None


def _scalar(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, width=float("inf"))
    return text.split("\n", 1)[0]


class _Node:
    """One ``key: value`` line, optionally with a nested block below it."""

    def __init__(
        self,
        key: str,
        value: Any = None,
        children: Optional[Union[List["_Node"], List[List["_Node"]]]] = None,
        is_list: bool = False,
        comment: str = "",
    ):
        self.key = key
        self.value = value
        self.children = children
        self.is_list = is_list
        self.comment = comment


def _field_default(f: "dataclasses.Field[Any]") -> Tuple[bool, Any]:
    if f.default is not dataclasses.MISSING:
        return True, f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return True, f.default_factory()  # type: ignore[misc]
    return False, None


def describe(cls: Type[Any]) -> List[_Node]:
    """Build the node tree for all fields of a dataclass."""
    hints = get_type_hints(cls)
    nodes = []
    for f in dataclasses.fields(cls):
        has_default, default = _field_default(f)
        nodes.append(_describe_field(f.name, hints[f.name], has_default, default))
    return nodes


def _describe_field(name: str, tp: Any, has_default: bool, default: Any) -> _Node:
    inner, _ = _unwrap_optional(tp)
    origin = get_origin(inner)
    args = get_args(inner)
    required = "" if has_default else "required"

    def note(text: str) -> str:
        return "; ".join(t for t in (required, text) if t)

    # Nested dataclass block
    if _is_dataclass_type(inner):
        if inner in FREE_FORM:
            return _Node(name, {}, comment=note(f"free-form {inner.__name__} parameters"))
        if default is None and has_default:
            return _Node(name, children=describe(inner), comment="default: null (structure shown)")
        return _Node(name, children=describe(inner), comment=note(""))

    # List of dataclasses: one example entry
    if origin is list and args and _is_dataclass_type(args[0]):
        shown = "null" if default is None else _scalar(default)
        dflt = f"default: {shown}" if has_default else ""
        return _Node(
            name,
            children=[describe(args[0])],
            is_list=True,
            comment=note("; ".join(t for t in (dflt, "example entry") if t)),
        )

    # Mapping name -> dataclass: one example entry
    if origin is dict and len(args) == 2 and _is_dataclass_type(args[1]):
        shown = "null" if default is None else _scalar(default)
        dflt = f"default: {shown}" if has_default else ""
        return _Node(
            name,
            children=[_Node(EXAMPLE_KEY, children=describe(args[1]))],
            comment=note("; ".join(t for t in (dflt, "example entry") if t)),
        )

    value = default if has_default else _placeholder(tp)
    return _Node(name, value, comment=note(""))


def _emit(nodes: List[_Node], depth: int, out: List[str], first_prefix: str = "") -> None:
    for index, node in enumerate(nodes):
        if index == 0 and first_prefix:
            lead = INDENT * (depth - 1) + first_prefix
        else:
            lead = INDENT * depth
        comment = f"  # {node.comment}" if node.comment else ""
        if node.children is None:
            out.append(f"{lead}{node.key}: {_scalar(node.value)}{comment}")
        elif node.is_list:
            out.append(f"{lead}{node.key}:{comment}")
            for entry in node.children:
                _emit(entry, depth + 2, out, first_prefix="- ")  # type: ignore[arg-type]
        else:
            out.append(f"{lead}{node.key}:{comment}")
            _emit(node.children, depth + 1, out)  # type: ignore[arg-type]


def render(cls: Type[Any] = datamodels.ScanConfig) -> str:
    """Return the YAML text for ``cls`` filled with its defaults."""
    out: List[str] = [f"# {cls.__name__} defaults (generated from kiwi_scan.datamodels)"]
    _emit(describe(cls), 0, out)
    return "\n".join(out) + "\n"


def _config_classes() -> Dict[str, Type[Any]]:
    return {
        name: obj
        for name, obj in vars(datamodels).items()
        if _is_dataclass_type(obj) and obj.__module__ == datamodels.__name__
    }


def main(argv: Optional[List[str]] = None) -> int:
    classes = _config_classes()
    parser = argparse.ArgumentParser(
        prog="kiwi-config-defaults",
        description=(
            "Print the full YAML structure of a kiwi-scan config dataclass "
            "(default: ScanConfig) filled with its default values."
        ),
    )
    parser.add_argument(
        "config_class",
        nargs="?",
        default="ScanConfig",
        choices=sorted(classes),
        help="Config dataclass to print (default: ScanConfig).",
    )
    parser.add_argument("-o", "--output", help="Write to this file instead of stdout.")
    args = parser.parse_args(argv)

    text = render(classes[args.config_class])
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
