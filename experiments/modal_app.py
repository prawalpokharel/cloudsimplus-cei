"""Build, test and run the CloudSim Plus dependency-criticality experiments on Modal.

Java 25 and Maven are not installed on the Mac, so every build and run happens
in a Modal CPU container from the eclipse-temurin:25-jdk image. The Maven
repository is cached in a Modal volume so later builds start quickly.

Usage (from the repository root, with the CloudSim Plus fork cloned into repo/):
    modal run experiments/modal_app.py::build --tests full
    modal run experiments/modal_app.py::sweep --config experiments/sweep.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# The source tree to build. Defaults to the fork clone; point it at an export of the
# upstream commit to measure the baseline test results.
REPO_DIR = Path(os.environ.get("CEI_REPO_DIR", str(ROOT / "repo")))
# The Maven project with the experiment harness: this repository's root, or a cloudsimplus-cei/ subfolder.
HARNESS_DIR = Path(os.environ.get("CEI_HARNESS_DIR", str(ROOT / "cloudsimplus-cei" if (ROOT / "cloudsimplus-cei").is_dir() else ROOT)))

app = modal.App("cei-cloudsimplus")
m2 = modal.Volume.from_name("cei-cloudsimplus-m2", create_if_missing=True)
jars = modal.Volume.from_name("cei-cloudsimplus-jars", create_if_missing=True)

image = (
    modal.Image.from_registry("eclipse-temurin:25-jdk", add_python="3.12")
    .apt_install("git", "curl")
    .run_commands(
        "curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz"
        " | tar xz -C /opt && ln -s /opt/apache-maven-3.9.11/bin/mvn /usr/local/bin/mvn"
    )
    .add_local_dir(REPO_DIR, "/src/repo", ignore=[".git", "**/target"])
    .add_local_dir(HARNESS_DIR, "/src/cloudsimplus-cei",
                   ignore=["**/target", "repo", "repo/**", ".git", ".git/**", "results/**", "report/**", "experiments/**"])
)

M2_DIR = "/root/.m2"
JAR_DIR = "/jars"


def _run(cmd, cwd, log):
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=isinstance(cmd, str))
    log.append(f"$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}  ({time.time() - t0:.0f}s, exit {proc.returncode})")
    log.append(proc.stdout[-20000:])
    log.append(proc.stderr[-8000:])
    return proc.returncode


@app.function(image=image, cpu=8, memory=12288, timeout=3600, volumes={M2_DIR: m2, JAR_DIR: jars})
def build_remote(tests: str = "full", tag: str = "current") -> dict:
    """tests: 'full' = mvn verify (unit + IT), 'unit' = unit tests only,
    'some:<pattern>' = only matching unit tests, 'none' = skip tests."""
    work = Path("/work")
    shutil.copytree("/src", work, dirs_exist_ok=True)
    # Modal mounts files with mtime 0, and maven-resources-plugin only copies a file
    # when it is newer than the target, so give every file the current time.
    subprocess.run("find /work -type f -exec touch {} +", shell=True, check=True)
    log: list[str] = []
    java_v = subprocess.run(["mvn", "-v"], capture_output=True, text=True).stdout.strip()
    log.append(java_v)
    if tests == "full":
        goal = "mvn -B -ntp install"
    elif tests == "unit":
        goal = "mvn -B -ntp install -DskipITs"
    elif tests.startswith("some:"):
        pat = tests.split(":", 1)[1]
        goal = f"mvn -B -ntp install -DskipITs -Dtest='{pat}' -Dsurefire.failIfNoSpecifiedTests=false"
    else:
        goal = "mvn -B -ntp install -DskipTests"
    rc_core = _run(goal, work / "repo", log)
    rc_cei = None
    if rc_core == 0 and (work / "cloudsimplus-cei" / "pom.xml").exists():
        rc_cei = _run("mvn -B -ntp package", work / "cloudsimplus-cei", log)
        if rc_cei == 0:
            out = Path(JAR_DIR) / tag
            out.mkdir(parents=True, exist_ok=True)
            for jar in (work / "cloudsimplus-cei" / "target").glob("*-with-dependencies.jar"):
                shutil.copy(jar, out / "cei-experiments.jar")
            jars.commit()
    m2.commit()
    # surefire / failsafe summaries
    reports = []
    for d in ("surefire-reports", "failsafe-reports"):
        rd = work / "repo" / "target" / d
        if rd.exists():
            for f in sorted(rd.glob("*.txt")):
                reports.append(f.read_text().strip().splitlines()[-1] if f.read_text().strip() else f.name)
    return {"rc_core": rc_core, "rc_cei": rc_cei, "log": "\n".join(log), "reports": reports}


@app.function(image=image, cpu=1, memory=6144, timeout=3600, volumes={JAR_DIR: jars})
def run_cell(cell: dict, tag: str = "current") -> str:
    """Runs one experiment cell (one graph shape, load and seed, every policy) and returns CSV text."""
    jar = Path(JAR_DIR) / tag / "cei-experiments.jar"
    args = ["java", "-Xmx5g", "-cp", str(jar), "io.github.prawalpokharel.cei.BlastRadiusExperiment"]
    for k, v in cell.items():
        args += [f"--{k}", str(v)]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"cell {cell} failed: {proc.stderr[-3000:]}")
    return proc.stdout


@app.local_entrypoint()
def build(tests: str = "full", tag: str = "current", log_out: str = ""):
    res = build_remote.remote(tests, tag)
    print("rc_core", res["rc_core"], "rc_cei", res["rc_cei"])
    print("\n".join(res["reports"][-400:]))
    if log_out:
        Path(log_out).write_text(res["log"] + "\n\nREPORTS\n" + "\n".join(res["reports"]))
    else:
        print(res["log"][-6000:])


@app.local_entrypoint()
def smoke(tag: str = "current", args: str = "--shape mixed --load 0.75 --seed 1"):
    parts = args.split()
    cell = {parts[i][2:]: parts[i + 1] for i in range(0, len(parts), 2)}
    print(run_cell.remote(cell, tag))


@app.local_entrypoint()
def sweep(config: str, out: str, tag: str = "current"):
    import csv
    import io

    cfg = json.loads(Path(config).read_text())
    cells = cfg["cells"]
    print(f"{len(cells)} cells")
    rows, fields = [], []
    for text in run_cell.map(cells, kwargs={"tag": tag}, order_outputs=False):
        reader = csv.DictReader(io.StringIO(text))
        for name in reader.fieldnames or []:
            if name not in fields:
                fields.append(name)
        rows.extend(reader)
    with open(out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print("wrote", out, len(rows), "rows")


@app.function(image=image, cpu=1, memory=2048, timeout=6 * 3600, volumes={JAR_DIR: jars})
def sweep_all(names: list, configs: dict, tag: str) -> dict:
    """Runs several sweeps server-side and writes each CSV to the volume (results/<tag>/), so the
    local client can detach right after launching."""
    import csv
    import io

    out_dir = Path(JAR_DIR) / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name in names:
        rows, fields, errors = [], [], []
        for text in run_cell.map(configs[name], kwargs={"tag": tag}, order_outputs=False, return_exceptions=True):
            if isinstance(text, Exception):
                errors.append(str(text)[:500])
                continue
            reader = csv.DictReader(io.StringIO(text))
            for field in reader.fieldnames or []:
                if field not in fields:
                    fields.append(field)
            rows.extend(reader)
        with open(out_dir / f"sweep_{name}.csv", "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        (out_dir / f"sweep_{name}.errors.json").write_text(json.dumps(errors))
        jars.commit()
        summary[name] = {"rows": len(rows), "errors": len(errors)}
        print(name, summary[name], flush=True)
    (out_dir / "done.json").write_text(json.dumps(summary))
    jars.commit()
    return summary


@app.local_entrypoint()
def sweep_detached(names: str, tag: str = "current"):
    """Launch with `modal run --detach experiments/modal_app.py::sweep_detached --names a,b --tag v5`,
    then fetch results/<tag>/ from the cei-cloudsimplus-jars volume."""
    sel = names.split(",")
    configs = {n: json.loads((HERE / f"sweep_{n}.json").read_text())["cells"] for n in sel}
    call = sweep_all.spawn(sel, configs, tag)
    print("spawned", call.object_id)
