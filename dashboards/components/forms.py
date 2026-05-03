"""
forms.py — auto-generated parameter forms for any frozen dataclass.
"""
from __future__ import annotations

import dataclasses
import sys
from typing import Any

import streamlit as st


def render_params_form(params_cls: type, key_prefix: str) -> Any:
    """Render number_input/checkbox/text widgets for each field of a frozen
    dataclass. Returns an instance of that dataclass with the user's values."""
    field_values: dict[str, Any] = {}
    for f in dataclasses.fields(params_cls):
        widget_key = f"{key_prefix}__{f.name}"
        default = f.default
        ftype = f.type
        resolved = ftype
        if isinstance(ftype, str):
            type_ns = {**vars(sys.modules[params_cls.__module__]),
                        "int": int, "float": float, "bool": bool, "str": str}
            try:
                resolved = eval(ftype, type_ns)
            except Exception:
                resolved = type(default) if default is not dataclasses.MISSING else str

        label = f.name.replace("_", " ")
        if resolved is bool:
            field_values[f.name] = st.checkbox(label, value=bool(default),
                                                  key=widget_key)
        elif resolved is int:
            field_values[f.name] = int(st.number_input(label, value=int(default),
                                                          step=1, key=widget_key))
        elif resolved is float:
            step = max(abs(float(default)) * 0.1, 0.01) if default else 0.1
            field_values[f.name] = float(st.number_input(label, value=float(default),
                                                            step=step, format="%.4f",
                                                            key=widget_key))
        else:
            field_values[f.name] = st.text_input(label, value=str(default),
                                                    key=widget_key)
    return params_cls(**field_values)
