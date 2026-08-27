# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.16.0
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Example Notebook
#
# This is a paired notebook. The source of truth is this `.py` file.
# The `.ipynb` file is generated via `poe nb` and is not tracked in git.
#
# Edit this `.py` file, then run `poe sync` to update the notebook.

# %%
from juplit import test

# %%
def hello(name: str) -> str:
    """Return a greeting string."""
    return f"Hello, {name}!"

# %%
if test():
    assert hello("world") == "Hello, world!"
    assert hello("juplit") == "Hello, juplit!"
    print("hello() tests pass")
