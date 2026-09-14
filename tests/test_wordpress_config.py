import pytest

from app.server.wordpress_config import parse_wordpress_database_settings


def test_wp_config_database_literals_are_read_without_executing_php():
    source = b"""<?php
define('DB_NAME', 'shop_wp');
define('DB_USER', 'shop_user');
define('DB_PASSWORD', 'not-logged');
define('DB_HOST', 'db.example.test:3307');
"""
    settings = parse_wordpress_database_settings(source)
    assert (settings.host, settings.port) == ("db.example.test", 3307)
    assert (settings.database, settings.username) == ("shop_wp", "shop_user")
    assert "not-logged" not in repr(settings)


def test_incomplete_wp_config_is_rejected():
    with pytest.raises(ValueError, match="DB_PASSWORD"):
        parse_wordpress_database_settings(b"define('DB_NAME', 'wordpress');")
