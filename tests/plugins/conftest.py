import json
import textwrap
import os
import subprocess
import sys
from pathlib import Path

import pytest

from polybench.testing import RecordingContext



@pytest.fixture
def make_ctx(tmp_path):
    def make(params=None, simulate=True, board="S01"):
        return RecordingContext(board=board, params=params or {}, workdir=tmp_path / "run", simulate=simulate)
    return make


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Keep the n6705b 'last connection' file out of the real cache."""
    import polybench.plugins.n6705b as n
    monkeypatch.setattr(n, "STATE_FILE", tmp_path / "state" / "n6705b_last.json")


@pytest.fixture(autouse=True)
def _scratch_data_dir(tmp_path, monkeypatch):
    """Plugins that look at the data folder (openocd's surviving-process scan) never see the real one."""
    monkeypatch.setenv("POLYBENCH_DATA", str(tmp_path / "pb-data"))


DBC = textwrap.dedent('''\
    VERSION ""
    NS_ :
    BS_:
    BU_: N
    BO_ 1008 Meas: 8 N
     SG_ Bridge : 0|24@1- (0.01,0) [-1000|1000] "uV" Vector__XXX
     SG_ Temp : 24|16@1+ (0.1,-40) [-40|125] "degC" Vector__XXX
     SG_ Cnt : 40|8@1+ (1,0) [0|255] "" Vector__XXX
    BO_ 2147483905 Ext: 8 N
     SG_ Mot : 7|12@0- (0.5,0) [-1000|1000] "V" Vector__XXX
     SG_ Mot2 : 23|8@0+ (1,0) [0|255] "" Vector__XXX
    ''')


@pytest.fixture
def dbc(tmp_path):
    p = tmp_path / "t.dbc"
    p.write_text(DBC)
    return str(p)
