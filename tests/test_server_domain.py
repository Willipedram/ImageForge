from app.server.domain import infer_website_domain


def test_domain_is_inferred_from_ftp_username_when_host_is_ip():
    assert infer_website_domain("192.0.2.10", "media@safirezaman.com") == "safirezaman.com"


def test_domain_is_inferred_from_directadmin_path_or_ftp_hostname():
    assert infer_website_domain(
        "192.0.2.10", "account", "/domains/example.org/public_html"
    ) == "example.org"
    assert infer_website_domain("ftp.example.net", "account") == "example.net"
