"""Тесты Шага 1 (настройка ниши и продукта, friday.local.yaml и skills/product_knowledge.md)."""

from __future__ import annotations

from pathlib import Path

from friday.config import FridayConfig, is_niche_configured, load_config
from friday.paths import PROJECT_ROOT
from friday.trainer import NICHE_TEMPLATES_RU, apply_niche_setup


def test_default_friday_yaml_starts_unconfigured():
    cfg = load_config(PROJECT_ROOT / "friday.yaml")
    assert is_niche_configured(cfg) is False
    assert cfg.brain.mode == "agent"
    assert cfg.compliance.regulated_niche == "none"


def test_apply_niche_setup_writes_local_yaml_and_skills(tmp_path: Path):
    base_cfg = FridayConfig()
    local_yaml = tmp_path / "friday.local.yaml"
    skills_dir = tmp_path / "skills"

    preset = NICHE_TEMPLATES_RU[0]  # B2B CRM integrator
    res = apply_niche_setup(
        {
            **preset,
            "competitive_intel_md": "# Конкурентная отстройка\n\nОтстройка от таблиц Excel.",
        },
        base_config=base_cfg,
        local_config_path=local_yaml,
        skills_dir=skills_dir,
    )

    assert local_yaml.exists()
    assert (skills_dir / "product_knowledge.md").exists()
    assert (skills_dir / "competitive_intel.md").exists()
    assert res["company_name"] == preset["persona"]["company"]

    loaded = load_config(local_yaml)
    assert is_niche_configured(loaded) is True
    assert loaded.icp.niche == preset["icp"]["niche"]
    assert "менеджер по продажам" in loaded.icp.hiring_signals
