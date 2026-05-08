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
        # Streamlit constraint: when a session_state value is pre-set
        # under the widget's key (e.g. by a URL deep-link prefill in
        # 1_📊_Backtest.py setting `bt_p_{base}__{field}`), passing
        # `value=` to the widget triggers the "default + session_state"
        # warning. To support both code paths cleanly:
        #   - if the key is already in session_state, OMIT value= and
        #     let session_state drive the initial render
        #   - otherwise, pass value=default as before
        prefilled = widget_key in st.session_state

        if resolved is bool:
            if prefilled:
                field_values[f.name] = st.checkbox(label, key=widget_key)
            else:
                field_values[f.name] = st.checkbox(
                    label, value=bool(default), key=widget_key)
        elif resolved is int:
            if prefilled:
                field_values[f.name] = int(st.number_input(
                    label, step=1, key=widget_key))
            else:
                field_values[f.name] = int(st.number_input(
                    label, value=int(default), step=1, key=widget_key))
        elif resolved is float:
            step = max(abs(float(default)) * 0.1, 0.01) if default else 0.1
            if prefilled:
                field_values[f.name] = float(st.number_input(
                    label, step=step, format="%.4f", key=widget_key))
            else:
                field_values[f.name] = float(st.number_input(
                    label, value=float(default), step=step,
                    format="%.4f", key=widget_key))
        else:
            if prefilled:
                field_values[f.name] = st.text_input(label, key=widget_key)
            else:
                field_values[f.name] = st.text_input(
                    label, value=str(default), key=widget_key)
    return params_cls(**field_values)
