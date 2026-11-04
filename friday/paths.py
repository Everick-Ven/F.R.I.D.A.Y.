"""Канонические пути проекта F.R.I.D.A.Y."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG_PATH = PROJECT_ROOT / "friday.local.yaml"
DATA_DIR = PROJECT_ROOT / "data"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
SKILLS_DIR = PROJECT_ROOT / "skills"
AGENT_TASKS_DIR = DATA_DIR / "agent_tasks"
