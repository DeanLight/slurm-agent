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
# # Session {{SESSION}}
#
# The record of one orchestration session: what was allocated, what was launched, what each
# remote agent concluded, and what it cost. This notebook is committed **with its outputs**
# — it is what a human reads afterwards, and what makes a run auditable six weeks later.
#
# Chat scrollback is not the record. This is.

# %% [markdown]
# ## Manifest
#
# * **Session:** {{SESSION}}
# * **Question:** _what this session is for, in one line_
# * **Task(s):** _TASK-nnn_

# %% tags=["parameters"]
JOB = "remote_dev"
GPUS = 2
LEASE = "04:00:00"

# %%
from slurm_agent.cli import _cluster, _runner
from slurm_agent import jobs, watch

cluster, run = _cluster(), _runner()

# %% [markdown]
# ## Allocation

# %%
job = jobs.job_up(JOB, run, cluster, gpus=GPUS, time_limit=LEASE)
job.model_dump()

# %% [markdown]
# ## Launches
#
# One cell per agent, so a failure is isolated to one output.

# %%
# session = launch.launch(_agent("experiment-runner"), "TASK-nnn", JOB, run, cluster)

# %% [markdown]
# ## Supervision
#
# Each poll's decisions, appended as the session runs. A mid-run poll is as informative as
# one at the end, because the log is written round by round.

# %%
# watch.watch(run, cluster, rules, once=True)

# %% [markdown]
# ## Findings
#
# _What each agent concluded, what it cost, and what happens next._
