"""End-to-end smoke test for Jarvis self-learning (Stages 1-3).

Does not require audio. Speaks directly to the Orchestrator so we can
verify that tool-calls happen and that the side effects (memory files,
user_skills/*.py, proposed_patches/*.json) land on disk.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator
from jarvis.patches import PatchManager
from jarvis.user_skills import UserSkillManager


def main() -> int:
    cfg = Config.load(ROOT / "config.yaml")
    cfg.load_state()

    # Use scratch sub-directories so the test doesn't pollute real data.
    mem_dir   = ROOT / ".scratch" / "memory"
    skill_dir = ROOT / ".scratch" / "user_skills"
    patch_repo = ROOT  # patches must touch real files; we'll only propose
    shutil.rmtree(ROOT / ".scratch", ignore_errors=True)

    memory = Memory(mem_dir)
    skills = UserSkillManager(skills_dir=skill_dir, repo_root=patch_repo)
    patches = PatchManager(repo_root=patch_repo)

    orch = Orchestrator(
        cfg.llm, cfg.permissions,
        memory=memory, user_skills=skills, patches=patches,
    )
    skills.bind(orch.register_user_skill, orch.unregister_user_skill)
    skills.load_all()

    print(f"LLM model: {cfg.llm.model}")
    print(f"Tools: core={len([*orch._user_tool_specs])} user-skills installed")
    print("-" * 60)

    prompts = [
        # Stage 1: memory
        "Please remember that my name is Bertie and I prefer short replies.",
        "Add a shortcut: when I say 'code' open cursor.exe",
        # Stage 2: user skill
        (
            "Create a user skill called greet_user that takes a 'name' string "
            "and returns a friendly greeting sentence."
        ),
        # Stage 3: propose a patch (non-destructive - just add a comment)
        (
            "Propose a patch to backend/jarvis/skills/info.py that adds the "
            "comment '# jarvis self-edited on test' near the top of the file. "
            "Keep every other line byte-identical."
        ),
    ]
    for p in prompts:
        print(f"\n>> {p}")
        r = orch.handle(p, user="guest")
        print(f"   intent={r.intent} success={r.success}")
        print(f"   reply: {r.reply[:200]}")

    # Show what landed on disk
    print("\n" + "=" * 60)
    print("facts.json:")
    print((mem_dir / "facts.json").read_text(encoding="utf-8")
          if (mem_dir / "facts.json").exists() else "(none)")
    print("\naliases.json:")
    print((mem_dir / "aliases.json").read_text(encoding="utf-8")
          if (mem_dir / "aliases.json").exists() else "(none)")
    print("\nuser_skills/:")
    for f in sorted(skill_dir.glob("*.py")):
        print(f"  {f.name}  ({f.stat().st_size} bytes)")
    print("\nproposed_patches/:")
    for f in sorted((ROOT / "proposed_patches").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {f.name}: {data.get('target')} - {data.get('description')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
