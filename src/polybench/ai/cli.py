"""`polybench mcp`, `polybench ai setup`, `polybench ai install-skills`. Nothing here edits an assistant's configuration:
setup prints text, and the installer copies the skill folders only when the user runs it."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .. import platform as _platform

SKILLS = Path(__file__).resolve().parent / "skills"     # package data (src/polybench/ai/skills): present after pip install too


def write_trial_config(url: str, path: Path | None = None) -> Path:
    """The trial form: an MCP config file for `claude --mcp-config <file>`. It is written to a Polybench folder, never to
    Claude Code's own configuration, and the assistant forgets it when the session ends."""
    import json
    import os
    from .routes import mcp_command
    path = Path(path).expanduser() if path else _platform.default_config_dir() / "claude-mcp-trial.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {} if url.rstrip("/") == "http://127.0.0.1:8750" else {"POLYBENCH_URL": url}
    cfg = {"mcpServers": {"polybench": {"command": mcp_command(), "args": ["--client", "claude-code"], **({"env": env} if env else {})}}}
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def setup_text(url: str) -> str:
    from .routes import setup_commands
    c = setup_commands(url)
    return (f"Polybench AI connector (app at {url})\n\n"
            f"Claude Code, try it without changing your configuration (this session only):\n  {c['claude_trial']}\n"
            f"  (interactive: Claude asks you to approve each Polybench tool. polybench ai setup --trial writes that file; nothing else changes)\n"
            f"One question, non-interactive (claude -p cannot show the permission prompt, so pre-approve the Polybench tools only):\n  {c['claude_trial_p']}\n\n"
            f"Claude Code, add the MCP server permanently (edits your Claude Code configuration; run it in your project folder):\n  {c['claude']}\n\n"
            f"Codex, add to ~/.codex/config.toml yourself:\n" + "\n".join("  " + x for x in c["codex"].splitlines()) + "\n\n"
            f"Skills for Claude Code (copies the polybench-* skills shipped with Polybench):\n  {c['skills']}            (into ~/.claude/skills)\n"
            f"  {c['skills']} --project .  (into ./.claude/skills)\n\n"
            "Codex reads AGENTS.md in the repo root.\n"
            "Permissions: set POLYBENCH_AI_PERMISSIONS=propose (default) | read | off in the app's environment.\n"
            "Privacy: what the assistant reads leaves this Mac through the assistant's cloud service; Polybench itself sends nothing.\n")


def install_skills(target: Path, link: bool, force: bool, out=sys.stdout) -> int:
    if not SKILLS.is_dir():
        print(f"skills not found at {SKILLS} (the Polybench install is incomplete; reinstall it)", file=sys.stderr)
        return 2
    target = target.expanduser()
    target.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()):
        dst = target / src.name
        if dst.exists() or dst.is_symlink():
            if not force:
                print(f"skip {dst} (exists; --force replaces it)", file=out)
                continue
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        if link:
            dst.symlink_to(src, target_is_directory=True)
        else:
            shutil.copytree(src, dst)
        print(f"{'linked' if link else 'copied'} {src.name} -> {dst}", file=out)
        n += 1
    print(f"{n} skill(s) installed in {target}", file=out)
    return 0


def main(argv) -> int:
    if argv[0] == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main(argv[1:])
    ap = argparse.ArgumentParser(prog="polybench ai", description="AI connector helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="print the setup commands for Claude Code and Codex (changes nothing)")
    s.add_argument("--url", default="http://127.0.0.1:8750")
    s.add_argument("--trial", action="store_true", help="write the trial MCP config file (~/.config/polybench/claude-mcp-trial.json) and print the command")
    s.add_argument("--trial-file", help="where to write the trial config (default ~/.config/polybench/claude-mcp-trial.json)")
    i = sub.add_parser("install-skills", help="copy (or link) the shipped skills into a Claude Code skills folder")
    i.add_argument("--target", help="skills folder (default ~/.claude/skills)")
    i.add_argument("--project", help="project folder: installs into <project>/.claude/skills")
    i.add_argument("--link", action="store_true", help="symlink instead of copying (follows the repo)")
    i.add_argument("--force", action="store_true", help="replace skills that are already there")
    a = ap.parse_args(argv[1:])
    if a.cmd == "setup":
        if a.trial or a.trial_file:
            f = write_trial_config(a.url, a.trial_file)
            print(f"wrote {f} (an MCP config for this run only; your Claude Code configuration is not touched)\nRun Claude Code with it (interactive; it asks you to approve each Polybench tool):\n  claude --mcp-config {f}\n"
                  f"or, for one question with claude -p, pre-approve the Polybench tools:\n  claude -p --mcp-config {f} --allowedTools mcp__polybench \"<your question>\"")
            return 0
        print(setup_text(a.url))
        return 0
    target = Path(a.target) if a.target else (Path(a.project) / ".claude" / "skills" if a.project else Path("~/.claude/skills"))
    return install_skills(target, a.link, a.force)
