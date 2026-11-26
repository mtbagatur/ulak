"""install.sh run in a clean folder, with the programs it calls replaced by recorders."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def clean_folder(tmp_path, name="ulak"):
    repo = tmp_path / name
    (repo / "deploy").mkdir(parents=True)
    for file in ("install.sh", "requirements.txt", "deploy/ulak.service.example"):
        shutil.copy2(ROOT / file, repo / file)
    log, tools = tmp_path / "calls", tmp_path / "tools"
    script(tools / "systemctl", f'echo "systemctl $*" >> "{log}"\n')
    # `python3 -m venv .venv` makes the two programs the script goes on to use.
    script(tools / "python3", f'''echo "python3 $*" >> "{log}"
mkdir -p .venv/bin
for n in pip python; do printf '#!/bin/sh\\necho "%s $*" >> "{log}"\\n' "$n" > ".venv/bin/$n"; chmod +x ".venv/bin/$n"; done
''')
    return repo, tools, log


def install(tmp_path, repo, tools):
    env = {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path / "home"),
           "ULAK_ALLOWED_LOGIN": "someone@github", "ULAK_VAPID_SUBJECT": "mailto:someone@example.com"}
    done = subprocess.run(["sh", str(repo / "install.sh")], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return (tmp_path / "home/.config/systemd/user/ulak.service").read_text()


def test_a_first_install_writes_the_unit_for_this_folder_and_starts_it(tmp_path):
    repo, tools, log = clean_folder(tmp_path)
    unit = install(tmp_path, repo, tools)
    assert f"WorkingDirectory={repo}\n" in unit
    assert "Environment=ULAK_ALLOWED_LOGIN=someone@github\n" in unit
    assert "Environment=ULAK_VAPID_SUBJECT=mailto:someone@example.com\n" in unit
    calls = log.read_text()
    assert "pip install -q -r requirements.txt" in calls and "python tools/generate_vapid.py" in calls
    assert "systemctl --user enable --now ulak.service" in calls


def test_running_it_again_restarts_the_service_so_new_code_is_served(tmp_path):
    repo, tools, log = clean_folder(tmp_path)
    install(tmp_path, repo, tools)
    log.write_text("")
    install(tmp_path, repo, tools)
    assert "systemctl --user restart ulak.service" in log.read_text()


def test_a_half_made_environment_is_made_again(tmp_path):
    repo, tools, log = clean_folder(tmp_path)
    (repo / ".venv/bin").mkdir(parents=True)  # what `python3 -m venv` leaves behind when ensurepip is missing
    install(tmp_path, repo, tools)
    assert "python3 -m venv .venv" in log.read_text()


def test_a_folder_with_a_space_in_its_name_still_starts(tmp_path):
    repo, tools, _ = clean_folder(tmp_path, "my tools")
    unit = install(tmp_path, repo, tools)
    assert f'ExecStart="{repo}/.venv/bin/python" app.py\n' in unit
