"""Tests for the RAM-based tuning formulas.

These encode the arithmetic that used to live as one-shot sed commands in the
original scripts. The floors matter most: they are what stops a tiny instance
(like the 448 MB test box this project was actually deployed to) from being
handed a buffer pool too small to start, or too large to fit.
"""

import pytest

from app.services import tuning


# --------------------------------------------------------------------------
# MariaDB
# --------------------------------------------------------------------------


def test_mariadb_settings_tiny_instance_stays_above_floors():
    """448 MB is smaller than any tier the original script anticipated."""
    settings = tuning.mariadb_settings(448)
    assert settings["innodb_buffer_pool_size_mb"] >= 64
    assert settings["tmp_table_size_mb"] >= 8
    assert settings["innodb_log_file_size_mb"] >= 32
    assert settings["max_connections"] == 20


def test_mariadb_settings_never_claims_more_than_half_of_ram():
    for ram in (512, 1024, 2048, 4096, 8192):
        settings = tuning.mariadb_settings(ram)
        # Buffer pool is 60% of at most half of RAM.
        assert settings["innodb_buffer_pool_size_mb"] <= ram * 0.6 // 2 + 1


def test_mariadb_max_connections_tier():
    assert tuning.mariadb_settings(512)["max_connections"] == 20
    assert tuning.mariadb_settings(1023)["max_connections"] == 20
    assert tuning.mariadb_settings(1024)["max_connections"] == 50
    assert tuning.mariadb_settings(4096)["max_connections"] == 50


def test_mariadb_settings_scale_up_with_ram():
    small = tuning.mariadb_settings(1024)
    large = tuning.mariadb_settings(8192)
    assert large["innodb_buffer_pool_size_mb"] > small["innodb_buffer_pool_size_mb"]


# --------------------------------------------------------------------------
# PHP
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ram,expected_limit",
    [(512, "128M"), (1024, "128M"), (2048, "256M"), (4096, "512M"), (16384, "512M")],
)
def test_php_memory_limit_tiers(ram, expected_limit):
    assert tuning.php_settings(ram)["memory_limit"] == expected_limit


def test_php_settings_always_have_opcache_enabled_via_template_values():
    settings = tuning.php_settings(1024)
    assert settings["opcache_memory_consumption"] > 0
    assert settings["opcache_max_accelerated_files"] > 0


# --------------------------------------------------------------------------
# PHP-FPM
# --------------------------------------------------------------------------


def test_fpm_uses_ondemand_on_small_instances():
    """ondemand keeps idle workers from costing memory MariaDB needs more,
    on exactly the kind of box this was tested against."""
    assert tuning.fpm_pool_settings(448)["pm"] == "ondemand"
    assert tuning.fpm_pool_settings(1024)["pm"] == "ondemand"


def test_fpm_switches_to_dynamic_on_larger_instances():
    assert tuning.fpm_pool_settings(8192)["pm"] == "dynamic"


def test_fpm_max_children_increases_with_ram():
    small = tuning.fpm_pool_settings(1024)["max_children"]
    large = tuning.fpm_pool_settings(4096)["max_children"]
    assert large > small


# --------------------------------------------------------------------------
# Swap
# --------------------------------------------------------------------------


def test_swap_recommendation_for_tiny_instance():
    """The 448 MB test box: this is the number that keeps MariaDB from being
    OOM-killed on it."""
    assert tuning.swap_recommendation_mb(448) == 1024


def test_swap_recommendation_is_capped():
    assert tuning.swap_recommendation_mb(512) == 1024
    assert tuning.swap_recommendation_mb(16384) == 2048


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------


def test_override_replaces_a_computed_value(db):
    from app.models import TuningOverride

    db.add(TuningOverride(key="mariadb.max_connections", value="99"))
    db.commit()

    settings = tuning.mariadb_settings(1024)
    assert settings["max_connections"] == 99
    # Everything else is still computed normally.
    assert settings["innodb_buffer_pool_size_mb"] > 0


def test_override_is_coerced_to_the_right_type(db):
    from app.models import TuningOverride

    db.add(TuningOverride(key="fpm.max_children", value="77"))
    db.commit()

    result = tuning.fpm_pool_settings(1024)["max_children"]
    assert result == 77
    assert isinstance(result, int)


def test_malformed_override_falls_back_to_the_computed_value(db):
    from app.models import TuningOverride

    db.add(TuningOverride(key="fpm.max_children", value="not-a-number"))
    db.commit()

    # Must not raise, and must not silently become the string.
    result = tuning.fpm_pool_settings(1024)["max_children"]
    assert isinstance(result, int)


def test_summary_includes_the_ram_it_was_derived_from():
    result = tuning.summary()
    assert result["total_ram_mb"] > 0
    assert "php" in result and "mariadb" in result and "fpm" in result
