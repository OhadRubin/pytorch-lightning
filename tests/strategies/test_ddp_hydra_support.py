import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pytorch_lightning.utilities.imports import _HYDRA_AVAILABLE
from tests.helpers.runif import RunIf

if _HYDRA_AVAILABLE:
    from omegaconf import OmegaConf


# fixture to run hydra jobs in a clean temporary directory
# Hydra creates its own output directories and logs
@pytest.fixture
def cleandir(tmp_path):
    """Run function in a temporary directory."""
    old_dir = os.getcwd()  # get current working directory (cwd)
    os.chdir(tmp_path)  # change cwd to the temp-directory
    yield tmp_path  # yields control to the test to be run
    os.chdir(old_dir)
    logging.shutdown()


# function to run a command line argument
def run_process(cmd):
    try:
        process = subprocess.Popen(
            args=cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            sys.stderr.write(f"Subprocess error:\n{stderr}\n")
            sys.stderr.write(f"Subprocess stdout:\n{stdout}\n")
            raise subprocess.CalledProcessError(returncode=process.returncode, cmd=cmd)
        return stdout, stderr
    except Exception as e:
        cmd = " ".join(cmd)
        sys.stderr.write(f"Error executing:\n{cmd}\n")
        raise e


# Script to run from command line
script = """
import hydra
import os
import torch

from pytorch_lightning import Trainer
from tests.helpers.boring_model import BoringModel


class BoringModelGPU(BoringModel):
    def on_train_start(self) -> None:
        # make sure that the model is on GPU when training
        assert self.device == torch.device(f"cuda:{self.trainer.strategy.local_rank}")
        self.start_cuda_memory = torch.cuda.memory_allocated()


@hydra.main()
def task_fn(cfg):
    trainer = Trainer(accelerator="auto", devices=cfg.devices, strategy=cfg.strategy, fast_dev_run=True)
    model = BoringModelGPU()
    trainer.fit(model)
    # make sure teardown executed
    assert not torch.distributed.is_initialized()
    assert "LOCAL_RANK" not in os.environ


if __name__ == "__main__":
    task_fn()
"""


@RunIf(min_gpus=2)
@pytest.mark.skipif(not _HYDRA_AVAILABLE, reason="Hydra not Available")
@pytest.mark.usefixtures("cleandir")
@pytest.mark.parametrize("subdir", [None, "dksa", ".hello"])
def test_ddp_with_hydra_runjob(subdir):
    # Save script locally
    with open("temp.py", "w") as fn:
        fn.write(script)

    # Run CLI
    devices = 2
    cmd = [sys.executable, "temp.py", f"+devices={devices}", '+strategy="ddp"']
    if subdir is not None:
        cmd += [f"hydra.output_subdir={subdir}"]
    run_process(cmd)

    # Make sure config.yaml was created for additional
    # processes.
    logs = list(Path.cwd().glob("**/config.yaml"))
    assert len(logs) == devices

    # Make sure the parameter was set and used
    cfg = OmegaConf.load(logs[0])
    assert cfg.devices == devices

    # Make sure PL spawned a job that is logged by Hydra
    logs = list(Path.cwd().glob("**/train_ddp_process_*.log"))
    assert len(logs) == devices - 1


@RunIf(min_gpus=2)
@pytest.mark.skipif(not _HYDRA_AVAILABLE, reason="Hydra not Available")
@pytest.mark.usefixtures("cleandir")
@pytest.mark.parametrize("num_jobs", [1, 2])
def test_ddp_with_hydra_multirunjob(num_jobs):
    # Save script locally
    with open("temp.py", "w") as fn:
        fn.write(script)

    # create fake multirun params based on `num_jobs`
    fake_param = "+foo="
    devices = 2
    for i in range(num_jobs):
        fake_param += f"{i}"
        if i < num_jobs - 1:
            fake_param += ","

    # Run CLI
    run_process([sys.executable, "temp.py", f"+devices={devices}", '+strategy="ddp"', fake_param, "--multirun"])

    # Make sure config.yaml was created for each job
    configs = sorted(Path.cwd().glob("**/.pl_ddp_hydra_*/config.yaml"))
    assert len(configs) == num_jobs * (devices - 1)

    # Make sure the parameter was set and used for each job
    for i, config in enumerate(configs):
        cfg = OmegaConf.load(config)
        local_rank = int(config.parent.parent.parts[-1])
        assert cfg.devices == devices
        assert cfg.foo == local_rank

    logs = list(Path.cwd().glob("**/train_ddp_process_*.log"))
    assert len(logs) == num_jobs * (devices - 1)
