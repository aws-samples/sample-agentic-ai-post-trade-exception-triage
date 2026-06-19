from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _mock_aws(tmp_path: Path) -> Path:
    log_path = tmp_path / "aws.log"
    aws_path = tmp_path / "aws"
    aws_path.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "${AWS_MOCK_LOG}"
if [ "$1" = "sts" ] && [ "$2" = "get-caller-identity" ]; then
  echo "111122223333"
  exit 0
fi
if [ "$1" = "cloudformation" ] && [ "$2" = "describe-stacks" ]; then
  exit 254
fi
echo "Unexpected aws call: $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    aws_path.chmod(0o755)
    return log_path


def _run_destroy_with_mocked_aws(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    log_path = _mock_aws(tmp_path)
    env = os.environ.copy()
    env["AWS_MOCK_LOG"] = str(log_path)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env.pop("STACK_NAME", None)
    env.pop("AWS_REGION", None)
    result = subprocess.run(
        ["bash", "scripts/destroy.sh", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log_path.read_text(encoding="utf-8")


def test_destroy_accepts_positional_stack_name_and_region(tmp_path: Path) -> None:
    result, aws_log = _run_destroy_with_mocked_aws(tmp_path, "custom-stack", "us-east-2")

    assert result.returncode == 0
    assert "Destroying custom-stack" in result.stdout
    assert "region=us-east-2" in result.stdout
    assert "cloudformation describe-stacks --stack-name custom-stack --region us-east-2" in aws_log


def test_destroy_accepts_flag_stack_name_and_region(tmp_path: Path) -> None:
    result, aws_log = _run_destroy_with_mocked_aws(
        tmp_path,
        "--stack-name",
        "flag-stack",
        "--region",
        "us-west-2",
    )

    assert result.returncode == 0
    assert "Destroying flag-stack" in result.stdout
    assert "region=us-west-2" in result.stdout
    assert "cloudformation describe-stacks --stack-name flag-stack --region us-west-2" in aws_log
