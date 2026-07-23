from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_next_config_applies_browser_security_headers_to_all_routes() -> None:
    source = (ROOT / "next.config.ts").read_text(encoding="utf-8")
    assert 'source: "/(.*)"' in source
    for header in (
        "Strict-Transport-Security",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Cross-Origin-Opener-Policy",
        "Cross-Origin-Resource-Policy",
    ):
        assert header in source


def test_csp_uses_per_request_nonce_and_denies_unsafe_production_scripts() -> None:
    source = (ROOT / "proxy.ts").read_text(encoding="utf-8")
    layout = (ROOT / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "crypto.randomUUID()" in source
    assert "requestHeaders.set(\"x-nonce\", nonce)" in source
    assert 'response.headers.set("Content-Security-Policy", policy)' in source
    assert "'nonce-${nonce}' 'strict-dynamic'" in source
    assert 'process.env.NODE_ENV === "development"' in source
    assert "'unsafe-inline'" not in source.split("script-src", maxsplit=1)[1].split("style-src", maxsplit=1)[0]
    assert 'dynamic = "force-dynamic"' in layout
    assert "default-src 'self'" in source
    assert "object-src 'none'" in source
    assert "frame-ancestors 'none'" in source
    assert "base-uri 'self'" in source
    assert "form-action 'self'" in source
    assert "upgrade-insecure-requests" in source
