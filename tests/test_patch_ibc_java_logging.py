import subprocess
from pathlib import Path

PATCH_SCRIPT = Path(__file__).parents[1] / "docker" / "patch-ibc-java-logging.sh"

REQUIRED_OPTIONS = (
    "-Dlog4j.configurationFile=file:/opt/thetagang/ibgateway-log4j2.xml",
    "-Dlog4j2.statusLoggerLevel=OFF",
    "-Dlog4j2.StatusLogger.level=OFF",
    "-Dorg.apache.logging.log4j.simplelog.StatusLogger.level=OFF",
    "-DStatusLogger.level=OFF",
)


def _write_ibcstart(path: Path, extra_options: tuple[str, ...] = ()) -> None:
    lines = [
        "#!/bin/bash\n",
        'java_vm_options="$java_vm_options -Dinstall4jType=standalone"\n',
    ]
    lines.extend(
        f'java_vm_options="$java_vm_options {option}"\n' for option in extra_options
    )
    lines.append('"$java_path/java" $java_vm_options ibcalpha.ibc.IbcGateway\n')
    path.write_text("".join(lines))


def test_patch_ibc_java_logging_adds_status_logger_options(tmp_path):
    ibcstart = tmp_path / "ibcstart.sh"
    _write_ibcstart(ibcstart)

    subprocess.run(["sh", str(PATCH_SCRIPT), str(ibcstart)], check=True)

    patched = ibcstart.read_text()
    for option in REQUIRED_OPTIONS:
        assert f'java_vm_options="$java_vm_options {option}"' in patched


def test_patch_ibc_java_logging_is_idempotent(tmp_path):
    ibcstart = tmp_path / "ibcstart.sh"
    _write_ibcstart(ibcstart)

    subprocess.run(["sh", str(PATCH_SCRIPT), str(ibcstart)], check=True)
    subprocess.run(["sh", str(PATCH_SCRIPT), str(ibcstart)], check=True)

    patched = ibcstart.read_text()
    for option in REQUIRED_OPTIONS:
        assert patched.count(option) == 1


def test_patch_ibc_java_logging_repairs_partial_older_patch(tmp_path):
    ibcstart = tmp_path / "ibcstart.sh"
    _write_ibcstart(
        ibcstart,
        ("-Dlog4j.configurationFile=file:/opt/thetagang/ibgateway-log4j2.xml",),
    )

    subprocess.run(["sh", str(PATCH_SCRIPT), str(ibcstart)], check=True)

    patched = ibcstart.read_text()
    for option in REQUIRED_OPTIONS:
        assert option in patched
        assert patched.count(option) == 1
